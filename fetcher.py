"""
Receptpagina's ophalen zoals een browser dat doet.

Dit bestand is de reden dat de brug bestaat. Tandoor haalt pagina's zelf op
met alleen een User-Agent en een Accept-header (zie RecipeUrlImportView in
cookbook/views/api.py), en daar zegt de botbeveiliging van onder andere
Albert Heijn nee tegen. Hier doen we de aanvraag die er wél als een browser
uitziet, en geven we de HTML onbewerkt terug.

Voor Albert Heijn gaat er een poging vóór die allemaal: daar vragen we het
recept bij api.ah.nl op, de API van de Appie-app, en bouwen we er zelf een
pagina van. Zie ah_api.py. Dat is de enige site waar het ophalen van de
pagina structureel niet lukt, en de enige waarvoor we een eigen route hebben.

Daarna, voor elke site, in deze volgorde:

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
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

import ah_api

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


def _attempt_ah(url: str) -> _Attempt | None:
    """
    Het recept bij de app-API van AH ophalen in plaats van bij de pagina.

    None betekent: dit is geen AH-receptlink, of de route staat uit.
    """
    if not ah_api.AH_API_AAN or ah_api.recipe_id_from_url(url) is None:
        return None
    # Een mislukte AH-poging krijgt nooit een status mee. fetch_page() stopt
    # de hele rij bij een 404 of 410, en dat klopt voor een pagina die weg is,
    # maar een 404 van api.ah.nl zegt iets over de API en niets over het
    # recept: dan horen de andere pogingen nog gewoon te draaien.
    try:
        html, recept = ah_api.fetch_page_html(url)
    except ah_api.AhError as e:
        reden = f"{e.reason} (status {e.status})" if e.status else e.reason
        return _Attempt(False, f"AH-API: {reden}")
    except Exception as e:  # een antwoord waar we niet op rekenden
        # Liever een mislukte poging dan een uitzondering die de rest van de
        # rij en /api/diagnose meeneemt.
        log.exception("onverwachte fout in de AH-route voor %s", url)
        return _Attempt(False, f"AH-API: onverwachte fout ({e.__class__.__name__})")
    if not ah_api.to_jsonld(recept, url)["recipeIngredient"]:
        # Een recept zonder ingrediënten is geen recept, maar Tandoor maakt
        # het wel aan - en dan staat het er, en ziet de brug het bij een
        # nieuwe poging als "bestaat al". Liever mislukt, dan zie je het.
        return _Attempt(False, "AH-API: AH gaf het recept zonder ingrediënten")
    log.info("recept %s bij de AH-API opgehaald: %s",
             recept.get("id"), recept.get("title"))
    return _Attempt(True, status=200,
                    result=FetchResult(url, url, 200, html, "ah-api"))


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


# De browser-container, als die er is. Zie renderer/ en de README.
RENDERER_URL = os.environ.get("RENDERER_URL", "").strip().rstrip("/")


def _attempt_renderer(url: str, *, timeout: int, renderer: str) -> _Attempt:
    """
    Laat de browser-container de pagina ophalen.

    Nodig voor sites die elke server-side aanvraag weigeren: Albert Heijn geeft
    403 op requests én op curl_cffi met TLS-vingerafdruk, omdat Akamai z'n
    sensor-cookies pas zet nadat het JavaScript op de pagina gedraaid heeft.
    Een echte browser doet dat gewoon.
    """
    try:
        resp = requests.post(f"{renderer}/render", json={"url": url},
                             timeout=(5, timeout + 60))
    except requests.RequestException as e:
        # Dit is bijna altijd een deployment-oorzaak en niet een netwerkstoring.
        # renderer_diagnose() zegt welke van de drie het is, en dat scheelt een
        # ronde zoeken.
        uitleg = renderer_diagnose(renderer).get("reden") or e.__class__.__name__
        return _Attempt(False, f"browser-container niet bereikt op {renderer}: {uitleg}")
    try:
        body = resp.json()
    except ValueError:
        return _Attempt(False, f"browser-container gaf status {resp.status_code}")
    if resp.status_code != 200 or "html" not in body:
        return _Attempt(False, f"browser: {body.get('error') or resp.status_code}")
    return _judge(url, body.get("status") or 200, body["html"],
                  body.get("final_url") or url, via="browser")


def fetch_page(url: str, *, timeout: int = 20, impersonate: bool = True,
               renderer_url: str | None = None) -> FetchResult:
    """
    Haal een receptpagina op. Gooit FetchError met een leesbare reden.

    De HTML gaat hierna onbewerkt naar Tandoor: wij ontleden niets.
    """
    renderer = RENDERER_URL if renderer_url is None else (renderer_url or "").rstrip("/")
    redenen = []

    pogingen = [
        ("AH-API", lambda: _attempt_ah(url)),
        ("minimale headers",
         lambda: _attempt_requests(url, timeout=timeout,
                                   headers=HEADERS_MINIMAAL, naam="requests")),
        ("volledige headers",
         lambda: _attempt_requests(url, timeout=timeout,
                                   headers=HEADERS, naam="requests+headers")),
    ]
    if impersonate:
        pogingen.append(("curl_cffi", lambda: _attempt_curl(url, timeout=timeout)))

    gelukt = None
    eerste_status = None
    for naam, doe in pogingen:
        uitkomst = doe()
        if uitkomst is None:
            redenen.append(f"{naam}: niet beschikbaar")
            continue
        if uitkomst.ok:
            log.info("opgehaald via %s: %s (status %s)", naam, url, uitkomst.status)
            gelukt = uitkomst.result
            break
        redenen.append(f"{naam}: {uitkomst.reason}")
        eerste_status = eerste_status or uitkomst.status
        log.warning("poging mislukt (%s) voor %s: %s", naam, url, uitkomst.reason)
        if uitkomst.status in (404, 410):
            # De pagina is er niet. Andere headers veranderen daar niets aan,
            # en een browserronde al helemaal niet.
            raise FetchError(uitkomst.reason, status=uitkomst.status)

    # De browser is de laatste redding: of niets kwam door, of wat doorkwam
    # bevat geen recept-schema (een cookiemuur geeft ook een nette 200).
    # Zonder browser-container hebben extra pogingen geen zin: dezelfde server
    # stuurt dezelfde inhoud, en alleen een echte browser verandert dat.
    if renderer and (gelukt is None or not describe_page(gelukt.html)["heeft_schema"]):
        if gelukt is not None:
            log.info("pagina zonder recept-schema, nu via de browser: %s", url)
        via_browser = _attempt_renderer(url, timeout=timeout, renderer=renderer)
        if via_browser.ok and describe_page(via_browser.result.html)["heeft_schema"]:
            log.info("via de browser gelukt: %s", url)
            return via_browser.result
        if via_browser.ok and gelukt is None:
            return via_browser.result          # beter dan niets
        if not via_browser.ok:
            redenen.append(f"browser: {via_browser.reason}")

    if gelukt is not None:
        return gelukt
    raise FetchError("; ".join(redenen) or "geen enkele poging kwam door",
                     status=eerste_status)


def renderer_health(renderer: str, *, timeout: int = 3) -> bool | None:
    """
    Antwoordt de browser-container? None als er geen is ingesteld.

    Dat is iets anders dan "is hij ingesteld", en precies het verschil waar je
    naar zoekt als Albert Heijn niet lukt.
    """
    renderer = (renderer or "").rstrip("/")
    if not renderer:
        return None
    try:
        resp = requests.get(f"{renderer}/healthz", timeout=timeout)
        return resp.status_code == 200 and bool((resp.json() or {}).get("ok"))
    except (requests.RequestException, ValueError):
        return False


def renderer_diagnose(renderer: str, *, timeout: float = 3.0) -> dict:
    """
    Waarom antwoordt de browser-container niet?

    Een ConnectionError gooit drie heel verschillende oorzaken op één hoop, en
    ze hebben elk een andere oplossing:

    * de naam lost niet op  -> de tweede service staat niet in dezelfde YAML,
      of heet anders;
    * de naam lost op maar de poort is dicht -> de container is er wel maar
      luistert niet, of valt bij de start om;
    * poort open, /healthz stil -> hij draait maar is nog niet klaar, of de
      browserthread is gestopt.
    """
    renderer = (renderer or "").rstrip("/")
    if not renderer:
        return {"ingesteld": False,
                "reden": "RENDERER_URL is leeg; er is geen browser-container"}

    delen = urlsplit(renderer)
    host = delen.hostname or ""
    poort = delen.port or (443 if delen.scheme == "https" else 80)
    uit = {"ingesteld": True, "host": host, "poort": poort}

    try:
        uit["ip"] = socket.gethostbyname(host)
    except OSError as e:
        uit["naam_lost_op"] = False
        uit["reden"] = (
            f"de naam '{host}' bestaat niet in dit Docker-netwerk "
            f"({e.__class__.__name__}). Staat de tweede service in dezelfde "
            f"YAML, met exact die naam?")
        return uit
    uit["naam_lost_op"] = True

    try:
        with socket.create_connection((host, poort), timeout=timeout):
            pass
    except OSError as e:
        uit["poort_open"] = False
        uit["reden"] = (
            f"{host} bestaat ({uit['ip']}) maar luistert niet op poort {poort} "
            f"({e.__class__.__name__}). De container is er dus wel - kijk in "
            f"z'n logboek of hij bij de start omvalt.")
        return uit
    uit["poort_open"] = True

    uit["gezond"] = renderer_health(renderer, timeout=timeout)
    uit["reden"] = "" if uit["gezond"] else (
        f"poort {poort} staat open maar /healthz antwoordt niet; hij start "
        f"misschien nog op")
    return uit


def probe(url: str, *, timeout: int = 20) -> list:
    """
    Probeer alle manieren náást elkaar en zeg per poging wat eruit kwam.

    Alleen voor het stellen van een diagnose: dit haalt dezelfde pagina
    meerdere keren op, dus niet gebruiken in de wachtrij.
    """
    varianten = []
    if ah_api.recipe_id_from_url(url) is not None:
        # _attempt_ah() geeft None als de route uitstaat; hier weten we al dat
        # het een AH-link is, dus dat kan alleen die reden hebben.
        varianten.append(("AH-API", lambda: _attempt_ah(url)
                          or _Attempt(False, "de AH-route staat uit (AH_API=0)")))
    varianten += [
        ("requests, minimale headers",
         lambda: _attempt_requests(url, timeout=timeout,
                                   headers=HEADERS_MINIMAAL, naam="requests")),
        ("requests, volledige headers",
         lambda: _attempt_requests(url, timeout=timeout,
                                   headers=HEADERS, naam="requests+headers")),
        (f"curl_cffi, impersonate={IMPERSONATE}",
         lambda: _attempt_curl(url, timeout=timeout)),
    ]
    if RENDERER_URL:
        varianten.append(
            ("browser-container",
             lambda: _attempt_renderer(url, timeout=timeout, renderer=RENDERER_URL)))
    else:
        varianten.append(("browser-container", None))

    uitkomsten = []
    for naam, poging in varianten:
        if poging is None:
            uitkomsten.append({"poging": naam, "ok": False,
                               "reden": "niet ingesteld (RENDERER_URL is leeg)"})
            continue
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
