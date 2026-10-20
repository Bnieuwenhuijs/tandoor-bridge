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

import json as _json
import logging
import re
import time
from dataclasses import dataclass
from html import escape

import requests

from fetcher import describe_page

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- HTML klaarzetten

# Django's DATA_UPLOAD_MAX_MEMORY_SIZE, die Tandoor niet overschrijft. Gaat de
# body hier voorbij, dan gooit Django een RequestDataTooBig nog voordat DRF
# hem ziet - en het antwoord is dan gewone HTML, geen JSON met een melding.
DJANGO_BODY_LIMIT = 2_621_440


def sanitise_html(html: str) -> str:
    """
    Haal eruit wat het serializerveld van Tandoor niet aanneemt.

    Het veld 'data' is een gewone DRF CharField, en die draait vóór de
    unquote(). Twee gevolgen, en geen van beide staat in de documentatie:

    * trim_whitespace staat standaard aan, dus Tandoor strijkt de witruimte
      aan het begin en eind er zelf af. Wij doen dat hier dus ook, anders
      verstuurt de brug iets anders dan Tandoor parseert en liegt de test die
      dat vergelijkt.
    * ProhibitNullCharactersValidator en ProhibitSurrogateCharactersValidator
      weigeren een NUL-byte of een losse surrogate met een 400. Eén kapot
      teken in een pagina zou dus het hele recept kosten.
    """
    html = html.strip()
    if "\x00" in html:
        html = html.replace("\x00", "")
    try:
        html.encode("utf-8")
    except UnicodeEncodeError:
        # Losse surrogates: komen voor in slecht gecodeerde pagina's.
        html = html.encode("utf-8", "ignore").decode("utf-8")
    return html


def escape_for_data(html: str) -> str:
    """
    Bescherm de HTML tegen de unquote() die Tandoor erover haalt.

    Die sloopt stil elke %XX-reeks in de pagina: %2F wordt /, %20 wordt een
    spatie. Omdat het unquote is en niet unquote_plus, is het procentteken
    zelf het enige dat bescherming nodig heeft; een + blijft gewoon staan. De
    pagina wordt er niet groter van, wat telt vanwege DJANGO_BODY_LIMIT.
    """
    return html.replace("%", "%25")


def prepare_data(html: str, url: str) -> str:
    """De HTML zoals hij het veld 'data' in gaat."""
    return escape_for_data(with_canonical(sanitise_html(html), url))


_CANONICAL_TAG_RE = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']?canonical\b["']?[^>]*>""", re.I)
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']*)["']""", re.I)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
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
    # Zoeken buiten commentaar om: <!-- <link rel="canonical" ...> --> is geen
    # uitspraak van de pagina, en BeautifulSoup leest hem ook niet.
    if bestaand := _CANONICAL_TAG_RE.search(_COMMENT_RE.sub("", html)):
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


# ---------------------------------------------------------------- recept klaarzetten

def recipe_payload(recipe: dict, source_url: str, *, send_properties: bool = False) -> dict:
    """
    Maak van het recipe-object uit stap 1 iets dat POST /api/recipe/ aanneemt.

    Het antwoord van recipe-from-source is niet hetzelfde model als het
    recept-model, en twee velden komen er in een vorm uit die het aanmaken
    met een 400 afwijst. Dat is geen theorie: beide staan in het antwoord van
    elk recept dat de brug ophaalt.

    * ingredients[].order is in het antwoord altijd null, omdat
      SourceImportIngredientSerializer het veld als allow_null declareert en
      get_from_scraper het nooit zet. Maar Ingredient.order is
      IntegerField(default=0) zonder null=True, dus bij het aanmaken geeft
      null "This field may not be null." Het veld eruit laten betekent dat
      Tandoor z'n eigen default gebruikt.
    * servings kan null zijn als de pagina geen porties noemt
      (parse_servings geeft dan None), terwijl Recipe.servings
      IntegerField(default=1) is - ook niet nullable.

    Verder weggelaten:

    * image_url hoort niet bij het recept-model en wordt genegeerd; de foto
      gaat via PUT /api/recipe/{id}/image/.
    * properties, tenzij uitdrukkelijk gevraagd. property_type is een directe
      FK, en drf-writable-nested voert die in UPDATE-stand uit: de naam uit
      de body wordt op het bestaande PropertyType geschreven. In de praktijk
      is dat dezelfde naam, maar het blijft een schrijfactie op de inrichting
      van de ruimte, en daar blijft de brug van af.
    * servings_text met de letterlijke tekst "None", wat parse_servings_text
      oplevert bij een pagina zonder porties.
    """
    payload = {sleutel: waarde for sleutel, waarde in recipe.items()
               if sleutel not in ("id", "image", "image_url")}
    payload["source_url"] = source_url

    if not send_properties:
        payload.pop("properties", None)

    if payload.get("servings") is None:
        payload.pop("servings", None)
    if payload.get("servings_text") in (None, "None"):
        payload.pop("servings_text", None)
    if payload.get("description") == "None":
        payload["description"] = ""

    stappen = []
    for stap in payload.get("steps") or []:
        stap = dict(stap)
        stap["ingredients"] = [
            {s: w for s, w in (ingredient or {}).items()
             if not (s == "order" and w is None)}
            for ingredient in (stap.get("ingredients") or [])
        ]
        stappen.append(stap)
    if stappen:
        payload["steps"] = stappen

    return payload


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


# Alleen statuscodes die bij een herstart of een proxy ertussen horen zijn
# het opnieuw proberen waard. Een 500 komt uit Tandoor zelf en is bijna altijd
# deterministisch: dezelfde pagina geeft dezelfde fout. Nog twee keer sturen
# levert dus dezelfde 500 op, maar kost wel drie plaatsen in de importlimiet
# van 60 per uur - een derde van je doorvoer, voor niets.
RETRY_STATUSSEN = frozenset({502, 503, 504})


class TandoorUnreachable(TandoorError):
    """Tandoor antwoordde niet. Meestal tijdelijk, dus geen eindoordeel."""


class NoRecipeSchema(TandoorNoRecipe):
    """
    Wij zagen al dat er geen recept in de pagina staat.

    Apart van TandoorNoRecipe omdat Tandoor hier niets van gezien heeft: de
    melding komt van ons, en de wachtrij hoort hem niet aan Tandoor toe te
    schrijven.
    """


class TandoorAiError(TandoorError):
    """De AI-aanroep zelf mislukte: een fout, een time-out, het model."""


class TandoorAiTimeout(TandoorAiError):
    """
    De nginx in de Tandoor-container gaf het op voordat de AI klaar was.

    Die wacht 60 seconden (geen proxy_read_timeout in http.d/Recipes.conf.
    template, dus de standaard), en is niet via de omgeving in te stellen.
    De AI-aanroep loopt in Tandoor daarna gewoon door - en kost tegoed - maar
    het antwoord komt nergens meer aan. Wie dan meteen de volgende vraag
    stuurt, stapelt aanroepen op in Tandoor, en dan antwoordt Tandoor ook
    niet meer op wie hem in de browser gebruikt.
    """


class TandoorAiCredits(TandoorAiError):
    """
    Het AI-tegoed van de space is op, of AI staat voor de space uit.

    Apart, omdat dit geen fout van dit ene recept is: elk volgend recept
    krijgt dezelfde 400. Een sorteerronde stopt hierop in plaats van door te
    gaan en honderd keer dezelfde melding te verzamelen.
    """


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


def _is_tegoed_op(payload) -> bool:
    """
    De 400 uit AiStepSortView als can_perform_ai_request() nee zegt.

    Op de tekst, want er is geen foutcode. Die tekst staat in Tandoor achter
    gettext; de Nederlandse vertaling is leeg, dus ook een Nederlandstalige
    Tandoor stuurt het Engels. De aanroeper controleert daarna nog via de
    space (sorteren.tegoed_op), zodat een andere taal hier niet tot een
    eindeloze rij mislukte recepten leidt.
    """
    melding = str((payload or {}).get("msg") or "").lower()
    return "credits remaining" in melding or "ai features are not enabled" in melding


def _geen_schema_melding(beschrijving: dict) -> str:
    """Zeg wat er dan wél in de pagina stond; dat scheelt het zoeken."""
    delen = [f"geen recept-schema in de pagina ({beschrijving['bytes'] // 1024} KB)"]
    if beschrijving.get("titel"):
        delen.append(f'titel: "{beschrijving["titel"]}"')
    if beschrijving.get("lijkt_cookiemuur"):
        delen.append("dit lijkt een cookiemelding, niet het recept")
    elif beschrijving.get("lijkt_js_pagina"):
        delen.append("de pagina bouwt zichzelf met JavaScript op, dus het "
                     "schema staat niet in de ruwe HTML")
    return "; ".join(delen)


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
                 send_properties: bool = False, sleep=time.sleep):
        if not base_url:
            raise ValueError("TANDOOR_URL is leeg")
        if not token:
            raise ValueError("TANDOOR_TOKEN is leeg")
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or base_url).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_throttle_wait = max_throttle_wait
        self.send_properties = send_properties
        self._sleep = sleep
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "tandoor-bridge",
        })

    # -- laag niveau ------------------------------------------------------

    def _request(self, method: str, path: str, *, json=None, files=None,
                 ok=(200, 201), retry: bool = True, timeout=None) -> dict:
        """
        retry=False voor aanroepen die niet veilig te herhalen zijn.

        POST /api/recipe/ is er zo een: gaat het antwoord verloren nadat
        Tandoor het recept al heeft aangemaakt, dan maakt een tweede identieke
        body een tweede recept. Dat gebeurt echt - een 502 van de proxy
        terwijl Tandoor herstart, of een create met veel nieuwe foods die
        langer duurt dan de leestimeout. Eén poging, en de wachtrij lost de
        rest op: bij een handmatige nieuwe poging ziet Tandoors eigen
        duplicates-lijst het recept en wordt het item "bestaat al".

        Een GET en recipe-from-source hebben geen bijwerking en mogen wel
        herhaald worden; de afbeelding-PUT vervangt de foto, dus die ook.
        """
        url = f"{self.base_url}{path}"
        laatste: TandoorError | None = None
        max_pogingen = self._max_retries if retry else 1

        for poging in range(1, max_pogingen + 1):
            try:
                resp = self._session.request(method, url, json=json, files=files,
                                             timeout=timeout or self._timeout)
            except requests.RequestException as e:
                laatste = TandoorUnreachable(
                    f"Tandoor is niet te bereiken op {self.base_url} ({e.__class__.__name__})"
                )
                if poging < max_pogingen:
                    self._sleep(2 ** poging)
                    continue
                raise laatste from e

            if resp.status_code == 429:
                wacht = _retry_after(resp)
                payload = _payload(resp)
                # Korte wachttijden lossen we hier op; een uur wachten is een
                # beslissing voor de wachtrij, niet voor één aanroep.
                if wacht <= self._max_throttle_wait and poging < max_pogingen:
                    log.warning("importlimiet bereikt, %s seconden wachten", wacht)
                    self._sleep(wacht)
                    continue
                raise TandoorThrottled(
                    _message_from(payload, 429), retry_after=wacht, payload=payload
                )

            if resp.status_code in ok:
                return _payload(resp)

            if resp.status_code in RETRY_STATUSSEN and poging < max_pogingen:
                log.warning("Tandoor gaf %s (herstart of proxy?), nog een poging",
                            resp.status_code)
                self._sleep(2 ** poging)
                continue

            payload = _payload(resp)
            melding = _message_from(payload, resp.status_code)
            if resp.status_code in RETRY_STATUSSEN:
                # Geen fout van Tandoor zelf: de nginx in de Tandoor-container
                # (of een proxy ervoor) kreeg geen of geen tijdig antwoord.
                melding = (f"Tandoor antwoordde niet op tijd (status {resp.status_code} "
                           f"van de proxy in de Tandoor-container)")
            elif 500 <= resp.status_code < 600:
                # Een Django-fout met DEBUG=False stuurt gewone HTML terug
                # zonder enige uitleg, dus hier is niets zinnigs uit te halen.
                # De traceback staat in het logboek van Tandoor zelf, en daar
                # moet je dus kijken - dat is niet vanzelf duidelijk.
                melding = (f"Tandoor gaf een interne fout ({resp.status_code}). "
                           f"De oorzaak staat in het logboek van de "
                           f"Tandoor-container, niet in dit antwoord.")
            if resp.status_code == 403:
                # Tandoor geeft nooit een 401: SessionAuthentication staat
                # vooraan in DEFAULT_AUTHENTICATION_CLASSES en z'n
                # authenticate_header() geeft None, waarna DRF elke 401 tot
                # een 403 herschrijft. Een verkeerd token en een token zonder
                # de scope "read write" zien er dus identiek uit.
                melding = (f"{melding} (controleer TANDOOR_TOKEN en of het token "
                           f"de scope 'read write' heeft)")
            raise TandoorError(melding, status=resp.status_code, payload=payload)

        raise laatste or TandoorError("onbekende fout bij Tandoor")

    # -- de importflow ----------------------------------------------------

    def recipe_from_source(self, url: str, html: str) -> SourceImport:
        """
        Laat Tandoor onze HTML ontleden. Wij ontleden zelf niets.

        De url gaat mee náást de data: Tandoor haalt de pagina dan nog steeds
        niet zelf op, maar vult wel 'duplicates' op basis van source_url.
        """
        # Vooraf kijken of er iets te ontleden valt. Tandoor geeft bij een
        # pagina zonder schema geen nette melding maar een 500: zijn data-tak
        # vangt NoSchemaFoundInWildMode niet af, terwijl de url-tak dat wel
        # doet. Dat kost een plaats in de limiet van 60 per uur en levert een
        # foutmelding op die niets uitlegt. Dit is geen ingrediënten ontleden
        # - het kijkt alleen of er een schema in de pagina staat.
        beschrijving = describe_page(html)
        if not beschrijving["heeft_schema"]:
            raise NoRecipeSchema(_geen_schema_melding(beschrijving),
                                 payload={"pagina": beschrijving})

        body = {"url": url, "data": prepare_data(html, url)}

        # Meten op de werkelijke body: JSON ontsnapt aanhalingstekens en
        # regeleinden, dus die is merkbaar groter dan de HTML zelf. Django
        # weigert erboven met gewone HTML in plaats van een melding.
        body_bytes = len(_json.dumps(body).encode("utf-8"))
        if body_bytes > DJANGO_BODY_LIMIT - 4096:
            raise TandoorError(
                f"de pagina is te groot voor Tandoor ({body_bytes // 1024} KB aan "
                f"verzoek; de grens ligt bij {DJANGO_BODY_LIMIT // 1024} KB)"
            )
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
        body = recipe_payload(recipe, source_url, send_properties=self.send_properties)
        aangemaakt = self._request("POST", "/api/recipe/", json=body, ok=(200, 201),
                                   retry=False)
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

    # -- bestaande recepten -----------------------------------------------

    def get_recipe(self, recipe_id: int) -> dict:
        return self._request("GET", f"/api/recipe/{recipe_id}/", ok=(200,))

    def recipe_ids(self) -> list:
        """(id, naam) van alle recepten in de space, in pagina's van 100."""
        recepten, pagina = [], 1
        while True:
            antwoord = self._request("GET", f"/api/recipe/?page={pagina}&page_size=100",
                                     ok=(200,))
            for recept in antwoord.get("results") or []:
                if isinstance(recept, dict) and isinstance(recept.get("id"), int):
                    recepten.append((recept["id"], recept.get("name") or ""))
            if not antwoord.get("next"):
                return recepten
            pagina += 1

    def patch_steps(self, recipe_id: int, steps: list, *, retry: bool) -> dict:
        """
        PATCH alleen de stappen van een recept. Zie sorteren.py voor wat je
        daarin wel en niet kunt sturen.

        retry=False zodra er nieuwe stappen in zitten: die maakt Tandoor bij
        elke poging opnieuw aan.
        """
        return self._request("PATCH", f"/api/recipe/{recipe_id}/", json={"steps": steps},
                             ok=(200,), retry=retry)

    # -- AI ---------------------------------------------------------------

    def current_space(self) -> dict:
        return self._request("GET", "/api/space/current/", ok=(200,))

    def ai_providers(self) -> list:
        """De AI-providers die deze gebruiker mag gebruiken: van de space en globaal."""
        providers, pagina = [], 1
        while True:
            antwoord = self._request("GET", f"/api/ai-provider/?page={pagina}", ok=(200,))
            providers.extend(p for p in antwoord.get("results") or [] if isinstance(p, dict))
            if not antwoord.get("next"):
                return providers
            pagina += 1

    def ai_step_sort(self, recipe: dict, provider_id: int, *, timeout=(5, 180)) -> dict:
        """
        Laat Tandoor de stappen en de indeling van de ingrediënten maken.

        Tandoor slaat hier niets op: het gesorteerde recept komt terug en de
        aanroeper beslist wat ermee gebeurt. Eén poging - elke aanroep kost
        tegoed - en een ruime leestimeout, want hier zit een taalmodel achter.
        """
        try:
            antwoord = self._request("POST", f"/api/ai-step-sort/?provider={int(provider_id)}",
                                     json=recipe, ok=(200,), retry=False, timeout=timeout)
        except TandoorThrottled:
            raise
        except TandoorUnreachable as e:
            raise TandoorAiError(f"geen antwoord van de AI binnen {timeout[1]} seconden of "
                                 f"Tandoor niet bereikbaar ({e.message})") from e
        except TandoorError as e:
            if e.status in RETRY_STATUSSEN:
                raise TandoorAiTimeout(
                    "de AI deed er langer over dan de 60 seconden die de nginx in de "
                    "Tandoor-container wacht; de vraag loopt in Tandoor nog door en "
                    "kost wel tegoed", status=e.status, payload=e.payload) from e
            if e.status == 400 and _is_tegoed_op(e.payload):
                raise TandoorAiCredits(
                    "het AI-tegoed van de space is op, of AI staat voor de space uit",
                    status=400, payload=e.payload) from e
            raise TandoorAiError(f"AI: {e.message}", status=e.status, payload=e.payload) from e
        if not isinstance(antwoord, dict):
            raise TandoorAiError("de AI gaf geen recept terug")
        return antwoord

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
