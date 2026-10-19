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
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

# De headers waarmee ah.nl, voedingscentrum.nl en leukerecepten.nl antwoorden.
# De User-Agent en Accept-Language komen uit recipe_parser, waar ze zich al
# bewezen hebben; de rest stuurt een echte Chrome er altijd bij, en een
# pagina-aanvraag zonder Sec-Fetch-* valt juist daardoor op.
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


def image_headers(referer: str | None = None) -> dict:
    """
    Headers voor het ophalen van een plaatje.

    De Referer is geen detail: beeld-CDN's weigeren een aanvraag die niet van
    de eigen pagina lijkt te komen.
    """
    headers = dict(HEADERS)
    headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
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


def _attempt_requests(url: str, *, timeout: int) -> _Attempt:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return _Attempt(False, f"netwerkfout ({e.__class__.__name__})")
    return _judge(url, resp.status_code, resp.text, resp.url, via="requests")


def _attempt_curl(url: str, *, timeout: int) -> _Attempt | None:
    """None betekent: curl_cffi is hier niet beschikbaar."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        log.warning("curl_cffi is niet geïnstalleerd, tweede poging overgeslagen")
        return None
    try:
        resp = curl_requests.get(url, headers=HEADERS, timeout=timeout,
                                 impersonate=IMPERSONATE, allow_redirects=True)
    except Exception as e:  # curl_cffi gooit z'n eigen fouttypes
        return _Attempt(False, f"netwerkfout met curl_cffi ({e.__class__.__name__})")
    return _judge(url, resp.status_code, resp.text, str(resp.url), via="curl_cffi")


def fetch_page(url: str, *, timeout: int = 20, impersonate: bool = True) -> FetchResult:
    """
    Haal een receptpagina op. Gooit FetchError met een leesbare reden.

    De HTML gaat hierna onbewerkt naar Tandoor: wij ontleden niets.
    """
    eerste = _attempt_requests(url, timeout=timeout)
    if eerste.ok:
        log.info("opgehaald: %s (status %s)", url, eerste.status)
        return eerste.result

    log.warning("eerste poging mislukt voor %s: %s", url, eerste.reason)
    if not impersonate:
        raise FetchError(eerste.reason, status=eerste.status)

    tweede = _attempt_curl(url, timeout=timeout)
    if tweede is None:
        raise FetchError(
            f"{eerste.reason}; curl_cffi is niet beschikbaar voor een tweede poging",
            status=eerste.status,
        )
    if tweede.ok:
        log.info("tweede poging met curl_cffi gelukt: %s", url)
        return tweede.result

    raise FetchError(
        f"{eerste.reason}; ook met curl_cffi: {tweede.reason}",
        status=tweede.status or eerste.status,
    )


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
        for brok in resp.iter_content(64 * 1024):
            inhoud += brok
            if len(inhoud) > MAX_IMAGE_BYTES:
                raise FetchError(
                    f"foto is groter dan {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
                )

    if not inhoud:
        raise FetchError("foto was leeg")
    log.info("foto opgehaald: %s (%s, %d KB)", url, content_type, len(inhoud) // 1024)
    return bytes(inhoud), content_type
