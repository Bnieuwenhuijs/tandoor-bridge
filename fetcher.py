"""
Receptpagina's ophalen zoals een browser dat doet.

Dit bestand is de reden dat de brug bestaat. Tandoor haalt pagina's zelf op
met alleen een User-Agent en een Accept-header (zie RecipeUrlImportView in
cookbook/views/api.py), en daar zegt de botbeveiliging van onder andere
Albert Heijn nee tegen. Hier doen we de aanvraag die er wél als een browser
uitziet, en geven we de HTML onbewerkt terug.

Twee pogingen, in deze volgorde:

  1. requests met een volledige set browserheaders.
  2. curl_cffi met impersonate="chrome", voor sites die niet naar de headers
     maar naar de TLS-vingerafdruk van de client kijken.

De tweede poging is optioneel: ontbreekt curl_cffi, dan blijft het bij de
eerste en zegt het logboek waarom. De brug blijft dus werken op een machine
waar dat wheel niet te installeren is.

Opgemerkt tijdens het bouwen: AH weigert ook een aanvraag met de juiste
headers als die van een datacenter-IP komt (geprobeerd vanuit de cloud:
Akamai "Access Denied"). Vanaf een gewone thuisverbinding gaat het goed. Komt
er een blokkade voorbij, dan staat de statuscode en het herkende type in het
logboek - dat scheelt het verschil tussen "mijn headers zijn fout" en "dit IP
staat op een lijst".
"""

import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

# De set die zich bewezen heeft. Letterlijk die uit recipe_parser, waarmee
# ah.nl, voedingscentrum.nl en leukerecepten.nl maanden lang gewoon
# antwoordden. Dit is de eerste poging, en dat is geen willekeurige keuze: een
# uitgebreidere set hieronder bleek bij AH juist een 403 op te leveren.
# Akamai kijkt naar de combinatie, en Chrome-headers zonder bijpassende
# TLS-vingerafdruk zijn verdachter dan een kale User-Agent.
HEADERS_MINIMAAL = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# De volledige set, als tweede poging. Sommige sites willen juist wél de
# headers zien die een echte navigatie verraden.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="126", "Not;A=Brand";v="24", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

# Het profiel dat curl_cffi nabootst. "chrome" volgt de nieuwste versie die
# het wheel kent, zodat een update van curl_cffi ons meeneemt.
IMPERSONATE = "chrome"

# Statuscodes waarbij een tweede poging zin heeft. 404 staat er bewust niet
# tussen: die pagina is er simpelweg niet.
_BLOCKED_STATUSES = frozenset({401, 403, 406, 429, 451, 503})

# Blokkadepagina's die met status 200 terugkomen. Akamai (ah.nl) stuurt een
# "Access Denied" met een Reference #, Cloudflare een "Just a moment".
_BOT_WALL_MARKERS = (
    "access denied",
    "reference #",
    "just a moment",
    "attention required",
    "verify you are a human",
    "enable javascript and cookies",
    "px-captcha",
    "incapsula",
)

# Een echte receptpagina is altijd groter dan dit. De grens voorkomt dat een
# recept dat het woord "access denied" bevat voor een blokkade doorgaat.
_SOFT_WALL_MAX_BYTES = 20_000

# Een receptfoto van meer dan dit is geen receptfoto. Voorkomt dat een
# verkeerde link het geheugen van de NAS opeet.
MAX_IMAGE_BYTES = 15 * 1024 * 1024


@dataclass(frozen=True)
class FetchResult:
    url: str          # wat we vroegen
    final_url: str    # waar we na doorverwijzingen uitkwamen
    status: int
    html: str
    via: str          # "requests" of "curl_cffi"


class FetchError(Exception):
    """Ophalen mislukt, met een reden die in de interface te lezen valt."""

    def __init__(self, reason: str, *, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class _Attempt:
    """Uitkomst van één poging; intern, maar de tests prikken erin."""
    ok: bool
    reason: str = ""
    status: int | None = None
    result: FetchResult | None = None


def looks_like_bot_wall(status: int, body: str) -> str | None:
    """Een korte omschrijving van de blokkade, of None als het er geen is."""
    if status in _BLOCKED_STATUSES:
        return f"status {status}"
    if status == 200 and len(body) <= _SOFT_WALL_MAX_BYTES:
        laag = body.lower()
        for marker in _BOT_WALL_MARKERS:
            if marker in laag:
                return f"blokkadepagina met status 200 ({marker})"
    return None


# De vormen waarin een recept in een pagina kan staan. recipe_scrapers leest
# ze alle drie via extruct, dus het is niet genoeg om alleen naar ld+json te
# kijken.
_SCHEMA_MARKERS = (
    'application/ld+json',
    'itemtype="https://schema.org/recipe"',
    "itemtype='https://schema.org/recipe'",
    'itemtype="http://schema.org/recipe"',
    "itemtype='http://schema.org/recipe'",
)


def describe_page(html: str) -> dict:
    """
    Wat zit er in deze pagina? Voor het stellen van een diagnose.

    Dit ontleedt geen recept - het kijkt alleen of er iets te ontleden valt.
    Tandoor geeft bij een pagina zonder schema namelijk een kale 500 (zijn
    data-tak vangt NoSchemaFoundInWildMode niet af), en dan weet je nog niet
    of de pagina leeg was, of een cookiemuur, of met JavaScript opgebouwd.
    """
    laag = html.lower()
    titel = ""
    if match := re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S):
        titel = re.sub(r"\s+", " ", match.group(1)).strip()[:200]
    return {
        "bytes": len(html),
        "titel": titel,
        "ld_json_blokken": laag.count("application/ld+json"),
        "heeft_schema": any(m in laag for m in _SCHEMA_MARKERS),
        "noemt_recipe": '"@type": "recipe"' in laag.replace(" ", "")
                        or '"@type":"recipe"' in laag.replace(" ", ""),
        "lijkt_cookiemuur": any(w in laag for w in (
            "cookiemelding", "accepteer cookies", "cookie-instellingen",
            "jouw privacy", "we gebruiken cookies", "accept all cookies")),
        "lijkt_js_pagina": "__next_data__" in laag or "window.__nuxt__" in laag,
    }


def image_headers(referer: str | None = None) -> dict:
    """
    Headers voor het ophalen van een plaatje.

    De Referer is geen detail: beeld-CDN's weigeren een aanvraag die niet van
    de eigen pagina lijkt te komen.
    """
    headers = dict(HEADERS)
    # Bewust geen avif: Tandoor keurt een upload op de bestandsnaam en
    # is_file_type_allowed() laat alleen png, jpg, jpeg, gif en webp door
    # (cookbook/helper/image_processing.py). Vragen we om avif, dan geeft een
    # modern CDN dat ook, en loopt de upload daarna stuk op het formaat.
    headers["Accept"] = "image/webp,image/png,image/jpeg,image/*;q=0.8"
    headers["Sec-Fetch-Dest"] = "image"
    headers["Sec-Fetch-Mode"] = "no-cors"
    headers["Sec-Fetch-Site"] = "same-site"
    headers.pop("Sec-Fetch-User", None)
    headers.pop("Upgrade-Insecure-Requests", None)
    if referer:
        headers["Referer"] = referer
    return headers


def _judge(url: str, status: int, text: str, final_url: str, *, via: str) -> _Attempt:
    """Bepaal of deze poging bruikbare HTML opleverde."""
    if wall := looks_like_bot_wall(status, text):
        return _Attempt(False, f"geblokkeerd ({wall})", status)
    if status in (404, 410):
        return _Attempt(False, f"pagina bestaat niet meer (status {status})", status)
    if status >= 400:
        return _Attempt(False, f"pagina gaf status {status}", status)
    if not text.strip():
        return _Attempt(False, "pagina was leeg", status)
    return _Attempt(True, status=status,
                    result=FetchResult(url, final_url, status, text, via))


def _attempt_requests(url: str, *, timeout: int, headers=None, naam="requests") -> _Attempt:
    try:
        resp = requests.get(url, headers=headers or HEADERS_MINIMAAL,
                            timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return _Attempt(False, f"netwerkfout ({e.__class__.__name__})")
    return _judge(url, resp.status_code, resp.text, resp.url, via=naam)


def _attempt_curl(url: str, *, timeout: int) -> _Attempt | None:
    """None betekent: curl_cffi is hier niet beschikbaar."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        log.warning("curl_cffi is niet geïnstalleerd, tweede poging overgeslagen")
        return None
    try:
        resp = curl_requests.get(url, headers=HEADERS_MINIMAAL, timeout=timeout,
                                 impersonate=IMPERSONATE, allow_redirects=True)
    except Exception as e:  # curl_cffi gooit z'n eigen fouttypes
        return _Attempt(False, f"netwerkfout met curl_cffi ({e.__class__.__name__})")
    return _judge(url, resp.status_code, resp.text, str(resp.url), via="curl_cffi")


def fetch_page(url: str, *, timeout: int = 20, impersonate: bool = True) -> FetchResult:
    """
    Haal een receptpagina op. Gooit FetchError met een leesbare reden.

    De HTML gaat hierna onbewerkt naar Tandoor: wij ontleden niets.
    """
    eerste = _attempt_requests(url, timeout=timeout,
                               headers=HEADERS_MINIMAAL, naam="requests")
    if eerste.ok:
        log.info("opgehaald: %s (status %s)", url, eerste.status)
        return eerste.result
    log.warning("poging 1 (minimale headers) mislukt voor %s: %s", url, eerste.reason)

    tweede = _attempt_requests(url, timeout=timeout,
                               headers=HEADERS, naam="requests+headers")
    if tweede.ok:
        log.info("poging 2 (volledige headers) gelukt: %s", url)
        return tweede.result
    log.warning("poging 2 (volledige headers) mislukt voor %s: %s", url, tweede.reason)

    if not impersonate:
        raise FetchError(f"{eerste.reason}; met volledige headers: {tweede.reason}",
                         status=eerste.status)

    derde = _attempt_curl(url, timeout=timeout)
    if derde is None:
        raise FetchError(
            f"{eerste.reason}; curl_cffi is niet beschikbaar voor een derde poging",
            status=eerste.status,
        )
    if derde.ok:
        log.info("poging 3 met curl_cffi gelukt: %s", url)
        return derde.result

    raise FetchError(
        f"{eerste.reason}; ook met curl_cffi: {derde.reason}",
        status=derde.status or eerste.status,
    )


def probe(url: str, *, timeout: int = 20) -> list:
    """
    Probeer alle manieren náást elkaar en zeg per poging wat eruit kwam.

    Alleen voor het stellen van een diagnose: dit haalt dezelfde pagina tot
    drie keer op, dus niet gebruiken in de wachtrij.
    """
    varianten = (
        ("requests, minimale headers",
         lambda: _attempt_requests(url, timeout=timeout,
                                   headers=HEADERS_MINIMAAL, naam="requests")),
        ("requests, volledige headers",
         lambda: _attempt_requests(url, timeout=timeout,
                                   headers=HEADERS, naam="requests+headers")),
        (f"curl_cffi, impersonate={IMPERSONATE}",
         lambda: _attempt_curl(url, timeout=timeout)),
    )

    uitkomsten = []
    for naam, poging in varianten:
        uitkomst = poging()
        if uitkomst is None:
            uitkomsten.append({"poging": naam, "ok": False,
                               "reden": "curl_cffi is hier niet geïnstalleerd"})
            continue
        regel = {"poging": naam, "ok": uitkomst.ok, "status": uitkomst.status}
        if uitkomst.ok:
            regel["final_url"] = uitkomst.result.final_url
            regel["pagina"] = describe_page(uitkomst.result.html)
        else:
            regel["reden"] = uitkomst.reason
        uitkomsten.append(regel)
    return uitkomsten


def fetch_image(url: str, *, referer: str | None = None, timeout: int = 20) -> tuple[bytes, str]:
    """
    Download een receptfoto. Geeft (inhoud, content-type) terug.

    Dit doen we zelf in plaats van Tandoor er een image_url te geven, om twee
    redenen: Tandoor haalt dat op met safe_request - die een privé-IP weigert
    en bij AH hetzelfde 403 krijgt als bij de pagina - en wij hebben de juiste
    Referer al in handen.
    """
    try:
        resp = requests.get(url, headers=image_headers(referer), timeout=timeout,
                            allow_redirects=True, stream=True)
    except requests.RequestException as e:
        raise FetchError(f"foto ophalen mislukte ({e.__class__.__name__})") from e

    with resp:
        if resp.status_code != 200:
            raise FetchError(f"foto gaf status {resp.status_code}", status=resp.status_code)

        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise FetchError(f"dit is geen afbeelding maar {content_type or 'onbekend'}")

        inhoud = bytearray()
        try:
            for brok in resp.iter_content(64 * 1024):
                inhoud += brok
                if len(inhoud) > MAX_IMAGE_BYTES:
                    raise FetchError(
                        f"foto is groter dan {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
                    )
        except requests.RequestException as e:
            # Het eerste verzoek lukte, maar de verbinding viel tijdens het
            # binnenhalen weg. Zonder deze vangst ontsnapt een ruwe
            # requests-fout aan FetchError, en dan vangt de wachtrij hem niet.
            raise FetchError(f"foto half binnengekomen ({e.__class__.__name__})") from e

    if not inhoud:
        raise FetchError("foto was leeg")
    log.info("foto opgehaald: %s (%s, %d KB)", url, content_type, len(inhoud) // 1024)
    return bytes(inhoud), content_type
