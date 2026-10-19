"""
De Albert Heijn-route: het recept bij de app-API ophalen, niet bij de pagina.

www.ah.nl staat achter een bot-muur die elke server een 403 geeft; dat is de
reden dat deze brug bestaat. api.ah.nl, waar de Appie-app mee praat, doet dat
niet: een anoniem token is genoeg om een recept op te vragen.

Wat hier binnenkomt gaat niet veld voor veld naar Tandoor. We bouwen er een
pagina van met hetzelfde soort JSON-LD dat AH zelf op de receptpagina zet, en
die pagina gaat daarna door de gewone route. Tandoor ontleedt dus nog steeds
alles, en Food Alias, Unit Alias, Never Unit en Food Replace vuren precies
zoals bij een pagina die we wel konden ophalen. Daarom vragen we ook
`ingredients { text }`: dat is de hele regel ("200 g spaghetti") en niet een
getal met een losse naam. De eenheid hoort door Tandoor gelezen te worden,
niet door ons.

Het schema achter api.ah.nl is niet gedocumenteerd en introspectie staat uit.
De veldnamen hieronder komen uit twee openbare clients die dezelfde API
gebruiken, en die zijn het niet over alles eens. Daarom is alles buiten de
kern optioneel: zegt AH "Cannot query field", dan gaat dat blok eruit of een
stap terug naar een kalere vorm, en vragen we het opnieuw. Verandert AH
morgen iets, dan levert de brug een kaler recept in plaats van een fout.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

# AH_API_URL staat er alleen voor de tests en de rooktest; in bedrijf is dit
# altijd het echte adres.
API_URL = os.environ.get("AH_API_URL", "https://api.ah.nl").strip().rstrip("/")

# De API wil de officiele app zien. Gaat AH ons weigeren, dan is dit het
# eerste om bij te werken naar de versie die nu in de App Store staat; het kan
# ook via de omgeving, zonder nieuwe image.
CLIENT_ID = os.environ.get("AH_CLIENT_ID", "appie-ios").strip() or "appie-ios"
CLIENT_VERSION = os.environ.get("AH_CLIENT_VERSION", "9.28").strip() or "9.28"
USER_AGENT = os.environ.get(
    "AH_USER_AGENT",
    f"Appie/{CLIENT_VERSION} (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)",
)

TIMEOUT = 20

# De AH-route staat aan, tenzij iemand hem uitzet. Dat laatste is er voor het
# geval AH de app-API dichtzet: dan vallen we terug op de browser-container en
# de bookmarklet zonder dat er een nieuwe image nodig is.
AH_API_AAN = os.environ.get("AH_API", "1").strip().lower() not in ("0", "false", "nee", "off")

# Een anoniem token leeft twee uur. We vernieuwen een minuut eerder, zodat een
# token dat net nog goed was onderweg niet alsnog verloopt.
TOKEN_MARGE = 60

_RECEPT_PAD_RE = re.compile(r"^/allerhande/recept/R-R(\d+)", re.IGNORECASE)
_KORT_PAD_RE = re.compile(r"^/r/(\d+)/?$")


class AhError(Exception):
    """Ophalen bij AH mislukt, met een reden die in de interface te lezen valt."""

    def __init__(self, reason: str, *, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def recipe_id_from_url(url: str) -> int | None:
    """Het receptnummer uit een AH-link, of None als het er geen is."""
    parts = urlsplit(url or "")
    host = parts.netloc.lower().rsplit(":", 1)[0]
    if host != "ah.nl" and not host.endswith(".ah.nl"):
        return None
    for regel in (_RECEPT_PAD_RE, _KORT_PAD_RE):
        if gevonden := regel.match(parts.path):
            return int(gevonden.group(1))
    return None


# -- token ----------------------------------------------------------------

@dataclass
class _Token:
    waarde: str
    geldig_tot: float


_token: _Token | None = None
_token_slot = threading.Lock()


def reset_token() -> None:
    """Vergeet het token. Voor de tests, en voor na een 401."""
    global _token
    with _token_slot:
        _token = None


def _headers(bearer: str | None = None) -> dict:
    kop = {
        "User-Agent": USER_AGENT,
        "x-client-name": CLIENT_ID,
        "x-client-version": CLIENT_VERSION,
        "x-application": "AHWEBSHOP",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if bearer:
        kop["Authorization"] = f"Bearer {bearer}"
    return kop


def _post(pad: str, body: dict, *, api_url: str, session, bearer: str | None = None,
          fouten_in_body=False) -> dict:
    """
    Een POST naar AH, met het antwoord als dict.

    fouten_in_body: bij /graphql hoort een afgewezen vraag bij het gesprek en
    niet bij de fouten. AH antwoordt daarop met status 400 en de uitleg in
    "errors" - niet met 200 zoals de meeste GraphQL-servers. Zonder deze
    uitzondering zouden we die uitleg weggooien en nooit zien welk veld hij
    niet kent.
    """
    try:
        antwoord = session.post(f"{api_url}{pad}", json=body,
                                headers=_headers(bearer), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise AhError(f"AH niet bereikbaar: {e.__class__.__name__}") from e
    inhoud = None
    try:
        inhoud = antwoord.json()
    except ValueError:
        inhoud = None
    if antwoord.status_code >= 400:
        if fouten_in_body and isinstance(inhoud, dict) and inhoud.get("errors"):
            return inhoud
        raise AhError(f"AH gaf status {antwoord.status_code}", status=antwoord.status_code)
    if not isinstance(inhoud, dict):
        raise AhError("AH stuurde geen JSON terug", status=antwoord.status_code)
    return inhoud


def token(*, api_url: str | None = None, session=None) -> str:
    """Een geldig anoniem token, uit de cache als dat kan."""
    global _token
    api_url = (api_url or API_URL).rstrip("/")
    with _token_slot:
        if _token and _token.geldig_tot > time.time():
            return _token.waarde
        eigen = session or requests.Session()
        try:
            antwoord = _post("/mobile-auth/v1/auth/token/anonymous",
                             {"clientId": CLIENT_ID}, api_url=api_url, session=eigen)
        finally:
            if session is None:
                eigen.close()
        waarde = antwoord.get("access_token")
        if not waarde:
            raise AhError("AH gaf geen token terug")
        leeft = int(antwoord.get("expires_in") or 3600)
        _token = _Token(str(waarde), time.time() + max(leeft - TOKEN_MARGE, 30))
        log.info("anoniem AH-token gehaald, geldig voor %d seconden", leeft)
        return _token.waarde


# -- de vraag -------------------------------------------------------------

@dataclass(frozen=True)
class _Selectie:
    """Een stuk van de vraag, van de rijkste vorm naar de kaalste."""
    naam: str
    vormen: tuple
    subvelden: frozenset = frozenset()


# id en title vragen we altijd: zonder die twee is er geen recept.
_KERN = "id title"

_SELECTIES = (
    # De ingredientregel is het enige dat we echt nodig hebben: daar leest
    # Tandoor de hoeveelheid en de eenheid uit. Lukt `text` niet, dan bouwen
    # we de regel zelf uit getal plus naam - minder goed, maar beter dan niets.
    _Selectie("ingredients",
              ("ingredients { text name { singular plural } }",
               "ingredients { text name { singular } }",
               "ingredients { text }",
               "ingredients { quantity name { singular plural } }",
               "ingredients { quantity name { singular } }"),
              frozenset({"text", "quantity", "name", "singular", "plural"})),
    _Selectie("preparation",
              ("preparation { steps summary }", "preparation { steps }"),
              frozenset({"steps", "summary"})),
    _Selectie("description", ("description",)),
    # cookTime is de bereidingstijd. AH splitst die elders wél in bereiden,
    # oven en wachten (time { cook oven wait }), maar alleen op het type dat
    # uit een zoekopdracht komt: op recipe(id:) bestaat dat veld niet, en dan
    # kost het vragen alleen een ronde. _tijden() leest het wel, voor als AH
    # het hier ooit toevoegt.
    _Selectie("cookTime", ("cookTime",)),
    _Selectie("servings", ("servings { number type }", "servings { number }"),
              frozenset({"number", "type"})),
    _Selectie("images", ("images { url width height rendition }",
                         "images { url width }", "images { url }"),
              frozenset({"url", "width", "height", "rendition"})),
    _Selectie("courses", ("courses",)),
    _Selectie("cuisines", ("cuisines",)),
    _Selectie("tips", ("tips { type value }",), frozenset({"type", "value"})),
)

_QUERY = """query Recept($id: Int!) {
  recipe(id: $id) {
    %s
  }
}"""

# graphql-java schrijft "Field 'text' in type 'RecipeIngredient' is undefined"
# en zet het pad in de code erachter: FieldUndefined@[recipe/ingredients/text].
# Andere servers schrijven: Cannot query field "text" on type "RecipeIngredient".
# We halen de namen tussen aanhalingstekens op en de laatste stap van dat pad.
# Alleen de laatste: de rest van het pad zijn de velden waar het onbekende veld
# in zat, en die bestaan juist wél. Zouden we die meenemen, dan gooiden we
# ingredients in z'n geheel weg omdat er één subveld niet bestaat.
_NAAM_RE = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]|@\[([A-Za-z0-9_/]+)\]""")


def _onbekende_velden(errors: list) -> set:
    namen = set()
    for fout in errors or []:
        bericht = str((fout or {}).get("message", ""))
        laag = bericht.lower()
        if "undefined" not in laag and "cannot query" not in laag:
            continue
        for tussen, pad in _NAAM_RE.findall(bericht):
            if tussen:
                namen.add(tussen)
            if stappen := [deel for deel in pad.split("/") if deel]:
                namen.add(stappen[-1])
    return namen


def _bouw_query(stand: dict) -> str:
    stukken = [_KERN]
    for selectie in _SELECTIES:
        index = stand[selectie.naam]
        if index < len(selectie.vormen):
            stukken.append(selectie.vormen[index])
    return _QUERY % "\n    ".join(stukken)


def _pas_aan(stand: dict, namen: set) -> list:
    """Zet selecties een vorm terug of helemaal uit; geeft terug wat er veranderde."""
    veranderd = []
    for selectie in _SELECTIES:
        if stand[selectie.naam] >= len(selectie.vormen):
            continue
        if selectie.naam in namen:
            stand[selectie.naam] = len(selectie.vormen)      # het veld bestaat niet
            veranderd.append(selectie.naam)
        elif namen & selectie.subvelden:
            stand[selectie.naam] += 1                        # kalere vorm proberen
            veranderd.append(selectie.naam)
    return veranderd


def _graphql(query: str, variables: dict, *, api_url: str, session, bearer: str) -> dict:
    return _post("/graphql", {"query": query, "variables": variables},
                 api_url=api_url, session=session, bearer=bearer,
                 fouten_in_body=True)


# Welke vraag bij deze AH werkt, hoeven we maar één keer uit te zoeken. Zonder
# deze cache kost elk recept opnieuw de rondes die nodig waren om erachter te
# komen dat een veld niet bestaat.
_stand: dict | None = None
_stand_slot = threading.Lock()


def reset_schema() -> None:
    """Vergeet welke vraag werkte. Voor de tests."""
    global _stand
    with _stand_slot:
        _stand = None


def fetch_recipe(recipe_id: int, *, api_url: str | None = None, session=None) -> dict:
    """Het recept zoals AH het geeft. Gooit AhError met een leesbare reden."""
    try:
        return _fetch_recipe(recipe_id, api_url=api_url, session=session)
    except AhError as e:
        if e.status != 401:
            raise
        # Het token was toch verlopen. De brug draait weken achter elkaar, dus
        # dit gebeurt een keer: nieuw token, en nog één poging.
        log.info("AH wees het token af; nieuw token halen")
        reset_token()
        return _fetch_recipe(recipe_id, api_url=api_url, session=session)


def _fetch_recipe(recipe_id: int, *, api_url: str | None = None, session=None) -> dict:
    global _stand
    api_url = (api_url or API_URL).rstrip("/")
    eigen = session or requests.Session()
    try:
        bearer = token(api_url=api_url, session=eigen)
        with _stand_slot:
            stand = dict(_stand) if _stand else {s.naam: 0 for s in _SELECTIES}
        # Elke ronde valt er minstens een selectie terug of af, dus dit loopt
        # hoe dan ook af; de grens is er voor het geval AH iets zegt wat we
        # verkeerd begrijpen.
        for _ in range(sum(len(s.vormen) for s in _SELECTIES) + 2):
            antwoord = _graphql(_bouw_query(stand), {"id": int(recipe_id)},
                                api_url=api_url, session=eigen, bearer=bearer)
            recept = (antwoord.get("data") or {}).get("recipe")
            fouten = antwoord.get("errors") or []
            if recept and not fouten:
                with _stand_slot:
                    _stand = dict(stand)
                return recept
            if not fouten:
                raise AhError(f"AH kent recept {recipe_id} niet")
            namen = _onbekende_velden(fouten)
            if not namen or not _pas_aan(stand, namen):
                melding = "; ".join(str(f.get("message", "")) for f in fouten[:3])
                raise AhError(f"AH weigerde de vraag: {melding[:300]}")
            log.info("AH kent %s niet (meer); vraag aangepast", ", ".join(sorted(namen)))
        raise AhError("AH bleef de vraag weigeren")
    finally:
        if session is None:
            eigen.close()


# -- naar een pagina ------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")

# AH geeft de portiesoort als code terug. Alleen wat we kennen zetten we om;
# een onbekende code laten we weg, want een verkeerd woord achter het getal is
# erger dan geen woord.
_PORTIES = {
    "PERSON": "personen", "PERSONS": "personen", "PEOPLE": "personen",
    "PIECE": "stuks", "PIECES": "stuks",
    "PORTION": "porties", "PORTIONS": "porties", "SERVING": "porties",
}


def _tekst(waarde) -> str:
    """Een AH-veld naar platte tekst: er zitten links en entiteiten in."""
    if not waarde:
        return ""
    plat = _TAG_RE.sub(" ", str(waarde))
    return re.sub(r"\s+", " ", htmllib.unescape(plat)).strip()


def _getal(waarde) -> str:
    """4.0 wordt "4", 1.5 wordt "1,5" - zoals AH het op de pagina zet."""
    try:
        getal = float(waarde)
    except (TypeError, ValueError):
        return ""
    if getal == int(getal):
        return str(int(getal))
    return f"{getal:g}".replace(".", ",")


def _ingredient_regel(ingredient: dict) -> str:
    regel = _tekst(ingredient.get("text"))
    if regel:
        return regel
    # Zonder `text` zelf samenstellen. Een eenheid zit er niet bij: die geeft
    # de API niet, en wij gaan er geen verzinnen.
    naam = ingredient.get("name") or {}
    aantal = _getal(ingredient.get("quantity"))
    enkel, meervoud = _tekst(naam.get("singular")), _tekst(naam.get("plural"))
    woord = meervoud if (meervoud and aantal not in ("", "1")) else enkel
    return " ".join(deel for deel in (aantal, woord) if deel)


def _stappen(recept: dict) -> list:
    bereiding = recept.get("preparation") or {}
    for sleutel in ("steps", "summary"):
        regels = [_tekst(stap) for stap in (bereiding.get(sleutel) or [])]
        if regels := [regel for regel in regels if regel]:
            return regels
    return []


def _afbeeldingen(recept: dict) -> list:
    beelden = [b for b in (recept.get("images") or [])
               if isinstance(b, dict) and b.get("url")]
    beelden.sort(key=lambda b: b.get("width") or 0, reverse=True)
    urls = list(dict.fromkeys(str(beeld["url"]) for beeld in beelden))
    return urls[:4]


def _porties(recept: dict) -> str:
    porties = recept.get("servings") or {}
    aantal = _getal(porties.get("number"))
    if not aantal:
        return ""
    soort = str(porties.get("type") or "").strip()
    woord = _PORTIES.get(soort.upper(), "" if soort.isupper() else soort.lower())
    return f"{aantal} {woord}".strip()


def _minuten(waarde) -> int:
    try:
        return max(int(waarde), 0)
    except (TypeError, ValueError):
        return 0


def _tijden(recept: dict) -> tuple:
    """
    (bereiden, wachten) in minuten.

    Tandoor zet prepTime in working_time en cookTime in waiting_time (zie
    get_from_scraper in cookbook/helper/recipe_url_import.py). De
    bereidingstijd van AH hoort dus in prepTime, en de oven- en wachttijd in
    cookTime - niet andersom, want dan staat alle tijd als wachttijd in
    Tandoor en de bereidingstijd op nul.
    """
    tijd = recept.get("time") or {}
    bereiden = _minuten(tijd.get("cook")) or _minuten(recept.get("cookTime"))
    wachten = _minuten(tijd.get("oven")) + _minuten(tijd.get("wait"))
    return bereiden, wachten


def _beschrijving(recept: dict) -> str:
    delen = [_tekst(recept.get("description"))]
    # De tips staan op de AH-pagina naast het recept. Tussen de stappen zetten
    # verandert de nummering, dus ze gaan onder de beschrijving.
    for tip in recept.get("tips") or []:
        if tekst := _tekst((tip or {}).get("value")):
            delen.append(f"Tip: {tekst}")
    return "\n\n".join(deel for deel in delen if deel)


def to_jsonld(recept: dict, url: str) -> dict:
    """Het recept als schema.org-Recipe, zoals AH het zelf op de pagina zet."""
    data = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": _tekst(recept.get("title")),
        "url": url,
    }
    if beschrijving := _beschrijving(recept):
        data["description"] = beschrijving
    if beelden := _afbeeldingen(recept):
        data["image"] = beelden
    if porties := _porties(recept):
        data["recipeYield"] = porties
    bereiden, wachten = _tijden(recept)
    if bereiden:
        data["prepTime"] = f"PT{bereiden}M"
    if wachten:
        data["cookTime"] = f"PT{wachten}M"
    if bereiden or wachten:
        # Tandoor gebruikt totalTime alleen als de andere twee nul zijn.
        data["totalTime"] = f"PT{bereiden + wachten}M"
    regels = [_ingredient_regel(i) for i in (recept.get("ingredients") or [])
              if isinstance(i, dict)]
    data["recipeIngredient"] = [regel for regel in regels if regel]
    data["recipeInstructions"] = [{"@type": "HowToStep", "text": stap}
                                  for stap in _stappen(recept)]
    trefwoorden = [_tekst(woord) for sleutel in ("courses", "cuisines")
                   for woord in (recept.get(sleutel) or [])]
    if trefwoorden := [woord for woord in trefwoorden if woord]:
        data["keywords"] = ", ".join(dict.fromkeys(trefwoorden))
    return data


def _veilig_json(data: dict) -> str:
    """JSON die niet uit z'n <script> kan breken, ook niet met een rare tekst."""
    ruw = json.dumps(data, ensure_ascii=False, indent=2)
    return (ruw.replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))


def to_html(recept: dict, url: str) -> str:
    """
    Een pagina met het recept erin, voor de gewone route naar Tandoor.

    De body is er voor een mens die hem per ongeluk opent; Tandoor leest het
    JSON-LD, net als bij de echte pagina.
    """
    data = to_jsonld(recept, url)
    titel = htmllib.escape(data.get("name") or f"AH-recept {recept.get('id', '')}")
    adres = htmllib.escape(url, quote=True)
    regels = "".join(f"<li>{htmllib.escape(r)}</li>"
                     for r in data.get("recipeIngredient", []))
    stappen = "".join(f"<li>{htmllib.escape(s['text'])}</li>"
                      for s in data.get("recipeInstructions", []))
    return (
        "<!doctype html>\n"
        '<html lang="nl">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{titel}</title>\n"
        f'<link rel="canonical" href="{adres}">\n'
        f'<script type="application/ld+json">\n{_veilig_json(data)}\n</script>\n'
        f"</head>\n<body>\n<h1>{titel}</h1>\n"
        f"<ul>{regels}</ul>\n<ol>{stappen}</ol>\n"
        f'<p>Opgehaald bij de Albert Heijn-API: <a href="{adres}">'
        f"{htmllib.escape(url)}</a></p>\n</body>\n</html>\n"
    )


def fetch_page_html(url: str, *, api_url: str | None = None, session=None) -> tuple:
    """
    De hele route: nummer uit de link, recept ophalen, pagina teruggeven.

    Geeft de HTML en het recept zoals AH het gaf, zodat /api/ah kan laten zien
    wat er binnenkwam.
    """
    recipe_id = recipe_id_from_url(url)
    if recipe_id is None:
        raise AhError("geen AH-receptlink")
    recept = fetch_recipe(recipe_id, api_url=api_url, session=session)
    return to_html(recept, url), recept
