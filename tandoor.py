"""
Client voor de Tandoor-API.

De brug doet precies wat de importpagina van Tandoor zelf doet, maar met HTML
die wij al hebben opgehaald. Drie aanroepen per recept:

  1. POST /api/recipe-from-source/  met {"url": ..., "data": <html>}
  2. POST /api/recipe/              met het recipe-object uit stap 1
  3. PUT  /api/recipe/{id}/image/   multipart, met de foto die wij downloadden

Vier dingen zijn in de broncode van tag 2.6.9 nagekeken, omdat ze niet in de
documentatie staan en elk afzonderlijk stil kan mislukken:

* Het veld 'data' gaat door urllib.parse.unquote (RecipeUrlImportView,
  cookbook/views/api.py). Zie escape_for_data().
* Worden url én data meegestuurd, dan parseert Tandoor onze data en haalt hij
  de pagina nooit zelf op, maar vult hij 'duplicates' wél - die controle
  staat buiten de url/data-splitsing. Zo krijgen we gratis een tweede
  dubbelcontrole, naast onze eigen database.
* In die tak krijgt de scraper org_url='https://urlnotfound.none', dus de
  bron-URL komt uit de canonical van de pagina. Zie with_canonical().
* 'source_url' staat in RecipeSerializer.fields en niet in read_only_fields,
  dus die mag meteen mee bij het aanmaken; een losse PATCH is alleen een
  vangnet.
"""

import logging
import re
import time
from dataclasses import dataclass
from html import escape

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- HTML klaarzetten

def escape_for_data(html: str) -> str:
    """
    Bescherm de HTML tegen de unquote() die Tandoor erover haalt.

    Die sloopt stil elke %XX-reeks in de pagina: %2F wordt /, %20 wordt een
    spatie. Omdat unquote (en niet unquote_plus) alleen naar %XX kijkt, is het
    procentteken zelf het enige dat bescherming nodig heeft. De rest blijft
    byte voor byte staan en de pagina wordt niet groter - wat telt, want
    Tandoor zet geen eigen DATA_UPLOAD_MAX_MEMORY_SIZE en Django neemt
    standaard niet meer dan 2,5 MB aan formuliergegevens aan.
    """
    return html.replace("%", "%25")


_CANONICAL_TAG_RE = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']?canonical\b["']?[^>]*>""", re.I)
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']*)["']""", re.I)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)
_HTML_OPEN_RE = re.compile(r"<html\b[^>]*>", re.I)


def with_canonical(html: str, url: str) -> str:
    """
    Zorg dat de pagina een absolute canonical heeft die naar onze URL wijst.

    Tandoor geeft de scraper org_url='https://urlnotfound.none' zodra er data
    meekomt, en recipe_scrapers leidt de bron-URL dan af uit
    <link rel="canonical">. Staat die er niet, of staat er een relatieve, dan
    plakt urljoin() hem aan urlnotfound.none vast. Gevolg: een leeg veld
    source_url bij het parsen, geen domein-trefwoord, en Food
    Replace-automations met een bron-regex die niet vuren.

    Dit is de enige ingreep in de HTML, en hij voegt alleen toe wat we al
    weten: welke URL we net hebben opgehaald. Er wordt niet geparseerd en niet
    opnieuw weggeschreven, zodat de rest van de pagina byte voor byte intact
    blijft.
    """
    if bestaand := _CANONICAL_TAG_RE.search(html):
        href = _HREF_RE.search(bestaand.group(0))
        if href and href.group(1).strip().lower().startswith(("http://", "https://")):
            return html  # de pagina weet het zelf al

    tag = f'<link rel="canonical" href="{escape(url, quote=True)}">'
    # Onze tag gaat vooraan: BeautifulSoup.find() pakt de eerste, dus een
    # relatieve canonical die verderop staat wint het niet van deze.
    if kop := _HEAD_OPEN_RE.search(html):
        return html[:kop.end()] + tag + html[kop.end():]
    if begin := _HTML_OPEN_RE.search(html):
        return html[:begin.end()] + f"<head>{tag}</head>" + html[begin.end():]
    return tag + html


# ---------------------------------------------------------------- afbeeldingen

# Tandoor keurt een upload op de naam en niet op de inhoud:
# is_file_type_allowed(..., image_only=True) in
# cookbook/helper/image_processing.py laat precies deze achtervoegsels door.
# Een upload zonder achtervoegsel wordt stil geweigerd - de serializer geeft
# dan None terug zonder foutmelding.
_SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# Voor het geval een CDN een onzinnig content-type meestuurt.
_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def image_suffix(content_type: str, content: bytes = b"") -> str:
    """Een achtervoegsel waar Tandoor de upload mee aanneemt."""
    if suffix := _SUFFIX_BY_TYPE.get((content_type or "").split(";")[0].strip().lower()):
        return suffix
    for magic, suffix in _MAGIC:
        if content.startswith(magic):
            return suffix
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    raise TandoorError(
        f"Tandoor accepteert dit beeldformaat niet ({content_type or 'onbekend'}); "
        "alleen jpg, png, gif en webp"
    )


# ---------------------------------------------------------------- fouten

class TandoorError(Exception):
    """Fout van Tandoor, met de melding die Tandoor zelf gaf."""

    def __init__(self, message: str, *, status: int | None = None, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


class TandoorNoRecipe(TandoorError):
    """Tandoor kon in deze pagina geen recept vinden. Niet onze fout."""


class TandoorThrottled(TandoorError):
    """De importlimiet is bereikt; na retry_after seconden mag het weer."""

    def __init__(self, message: str, *, retry_after: int, payload=None):
        super().__init__(message, status=429, payload=payload)
        self.retry_after = retry_after


# ---------------------------------------------------------------- antwoordvorm

@dataclass(frozen=True)
class SourceImport:
    """Wat POST /api/recipe-from-source/ teruggaf."""
    recipe: dict
    images: list
    duplicates: list
    msg: str = ""

    @property
    def duplicate_ids(self) -> list:
        return [d["id"] for d in self.duplicates if isinstance(d, dict) and "id" in d]


def _payload(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"detail": data}


def _message_from(payload: dict, status: int) -> str:
    """
    Haal de leesbaarste melding uit een foutantwoord.

    Tandoor antwoordt in drie vormen: {"error": true, "msg": ...} vanuit de
    importview, {"detail": ...} vanuit DRF, en {"veld": ["..."]} bij
    validatiefouten.
    """
    if not payload:
        return f"Tandoor gaf status {status}"
    for sleutel in ("msg", "detail"):
        waarde = payload.get(sleutel)
        if isinstance(waarde, str) and waarde.strip():
            return waarde.strip()
    velden = [
        f"{veld}: {meldingen[0] if isinstance(meldingen, list) and meldingen else meldingen}"
        for veld, meldingen in payload.items()
        if veld not in ("error", "msg", "detail", "recipe", "images", "duplicates")
    ]
    if velden:
        return "; ".join(velden[:3])
    return f"Tandoor gaf status {status}"


def _retry_after(resp) -> int:
    kop = (resp.headers.get("Retry-After") or "").strip()
    try:
        return max(1, int(float(kop)))
    except ValueError:
        # DRF zet de kop altijd, maar reken er niet op: zonder kop is een
        # kwartier een veilige gok bij een limiet van 60 per uur.
        return 900


# ---------------------------------------------------------------- client

class TandoorClient:
    """
    Alles wat de brug van Tandoor nodig heeft.

    public_url is er voor de links in de interface: op de NAS praat de brug
    met 192.168.1.130, maar onderweg wil ik een Tailscale-adres aanklikken.
    """

    def __init__(self, base_url: str, token: str, *, public_url: str | None = None,
                 timeout=(5, 30), max_retries: int = 3, max_throttle_wait: int = 120,
                 sleep=time.sleep):
        if not base_url:
            raise ValueError("TANDOOR_URL is leeg")
        if not token:
            raise ValueError("TANDOOR_TOKEN is leeg")
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or base_url).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_throttle_wait = max_throttle_wait
        self._sleep = sleep
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "tandoor-bridge",
        })

    # -- laag niveau ------------------------------------------------------

    def _request(self, method: str, path: str, *, json=None, files=None, ok=(200, 201)) -> dict:
        url = f"{self.base_url}{path}"
        laatste: TandoorError | None = None

        for poging in range(1, self._max_retries + 1):
            try:
                resp = self._session.request(method, url, json=json, files=files,
                                             timeout=self._timeout)
            except requests.RequestException as e:
                laatste = TandoorError(
                    f"Tandoor is niet te bereiken op {self.base_url} ({e.__class__.__name__})"
                )
                if poging < self._max_retries:
                    self._sleep(2 ** poging)
                    continue
                raise laatste from e

            if resp.status_code == 429:
                wacht = _retry_after(resp)
                payload = _payload(resp)
                # Korte wachttijden lossen we hier op; een uur wachten is een
                # beslissing voor de wachtrij, niet voor één aanroep.
                if wacht <= self._max_throttle_wait and poging < self._max_retries:
                    log.warning("importlimiet bereikt, %s seconden wachten", wacht)
                    self._sleep(wacht)
                    continue
                raise TandoorThrottled(
                    _message_from(payload, 429), retry_after=wacht, payload=payload
                )

            if resp.status_code in ok:
                return _payload(resp)

            if 500 <= resp.status_code < 600 and poging < self._max_retries:
                log.warning("Tandoor gaf %s, nog een poging", resp.status_code)
                self._sleep(2 ** poging)
                continue

            payload = _payload(resp)
            raise TandoorError(_message_from(payload, resp.status_code),
                               status=resp.status_code, payload=payload)

        raise laatste or TandoorError("onbekende fout bij Tandoor")

    # -- de importflow ----------------------------------------------------

    def recipe_from_source(self, url: str, html: str) -> SourceImport:
        """
        Laat Tandoor onze HTML ontleden. Wij ontleden zelf niets.

        De url gaat mee náást de data: Tandoor haalt de pagina dan nog steeds
        niet zelf op, maar vult wel 'duplicates' op basis van source_url.
        """
        body = {"url": url, "data": escape_for_data(with_canonical(html, url))}
        try:
            payload = self._request("POST", "/api/recipe-from-source/", json=body, ok=(200,))
        except TandoorError as e:
            if e.status == 400 and isinstance(e.payload, dict) and e.payload.get("error"):
                raise TandoorNoRecipe(e.message, status=400, payload=e.payload) from None
            raise

        if payload.get("error"):
            raise TandoorNoRecipe(_message_from(payload, 200), status=200, payload=payload)

        recept = payload.get("recipe")
        if not isinstance(recept, dict) or not recept.get("name"):
            raise TandoorNoRecipe(
                payload.get("msg") or "Tandoor vond geen recept op deze pagina",
                status=200, payload=payload,
            )

        return SourceImport(
            recipe=recept,
            images=payload.get("images") or [],
            duplicates=payload.get("duplicates") or [],
            msg=payload.get("msg") or "",
        )

    def create_recipe(self, recipe: dict, *, source_url: str) -> dict:
        """
        Maak het recept aan, met de bron er meteen bij.

        Het recipe-object gaat vrijwel onbewerkt terug naar Tandoor, net zoals
        de eigen importpagina dat doet; alleen source_url zetten we zelf, op
        exact de opgeschoonde URL die ook onze dubbel-sleutel is.
        """
        body = dict(recipe)
        body["source_url"] = source_url
        body.pop("id", None)
        aangemaakt = self._request("POST", "/api/recipe/", json=body, ok=(200, 201))
        if not aangemaakt.get("id"):
            raise TandoorError("Tandoor gaf geen id terug bij het aanmaken van het recept")
        return aangemaakt

    def ensure_source_url(self, recipe_id: int, source_url: str) -> bool:
        """
        Vangnet: zet source_url alsnog als het aanmaken hem niet overnam.

        In 2.6.9 is het veld schrijfbaar, dus normaal doet dit niets en kost
        het ook geen aanroep.
        """
        self._request("PATCH", f"/api/recipe/{recipe_id}/",
                      json={"source_url": source_url}, ok=(200,))
        log.info("source_url nagezet op recept %s", recipe_id)
        return True

    def set_image(self, recipe_id: int, content: bytes, *, content_type: str) -> dict:
        """
        Upload de foto als multipart.

        Het endpoint heeft parser_classes=[MultiPartParser], dus een JSON-body
        komt er niet in. De naam van het bestand moet een achtervoegsel hebben
        dat Tandoor kent; zie image_suffix().
        """
        bestandsnaam = f"recept{image_suffix(content_type, content)}"
        return self._request(
            "PUT", f"/api/recipe/{recipe_id}/image/",
            files={"image": (bestandsnaam, content, content_type)}, ok=(200, 201),
        )

    # -- hulpmiddelen -----------------------------------------------------

    def recipe_url(self, recipe_id: int) -> str:
        """De link die ik in de interface wil kunnen aanklikken."""
        return f"{self.public_url}/recipe/{recipe_id}"

    def check_connection(self) -> int:
        """
        Controleer URL, token en scope in één aanroep; geeft het aantal
        recepten terug. Gebruikt door de rooktest en de startcontrole.
        """
        payload = self._request("GET", "/api/recipe/?page_size=1", ok=(200,))
        return int(payload.get("count") or 0)
