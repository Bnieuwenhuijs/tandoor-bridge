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
# Een lege AH_USER_AGENT= in de .env (zoals example.env hem laat staan) is
# "niet ingesteld", geen lege kop.
USER_AGENT = (os.environ.get("AH_USER_AGENT", "").strip()
              or f"Appie/{CLIENT_VERSION} (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)")

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

    Alleen bij 400. Een 401 of 403 met "errors" erin (de gewone vorm van een
    GraphQL-gateway die een token weigert) gaat de vraag niet aan maar het
    token, en moet z'n status houden: daar hangt de nieuwe-tokenpoging in
    fetch_recipe() aan.
    """
    try:
        antwoord = session.post(f"{api_url}{pad}", json=body,
                                headers=_headers(bearer), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise AhError(f"AH niet bereikbaar: {e.__class__.__name__}") from e
    try:
        inhoud = antwoord.json()
    except (ValueError, requests.RequestException):
        # RequestException hier is een body die niet uit te pakken valt
        # (ContentDecodingError); dat leest pas bij .json(), niet bij post().
        inhoud = None
    if antwoord.status_code >= 400:
        if (fouten_in_body and antwoord.status_code == 400
                and isinstance(inhoud, dict) and inhoud.get("errors")):
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
        try:
            leeft = int(antwoord.get("expires_in") or 3600)
        except (TypeError, ValueError):
            leeft = 3600                    # liever te kort dan een vastloper
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
    # cookTime is de bereidingstijd. De oventijd en de wachttijd staan niet op
    # recipe(id:), alleen op het type dat uit een zoekopdracht komt
    # (time { cook oven wait }); die halen we er met _tijd_uit_zoeken() bij.
    _Selectie("cookTime", ("cookTime",)),
    _Selectie("servings", ("servings { number type }", "servings { number }"),
              frozenset({"number", "type"})),
    # Kent AH width niet, dan liever height dan alleen url: zonder maat is
    # niet te zien welke de grote is, en dan wordt het de eerste - bij AH het
    # kleinste plaatje.
    _Selectie("images", ("images { url width height rendition }", "images { url height }",
                         "images { url width }", "images { url }"),
              frozenset({"url", "width", "height", "rendition"})),
    _Selectie("courses", ("courses",)),
    _Selectie("cuisines", ("cuisines",)),
    _Selectie("tags", ("tags { key value }",), frozenset({"key", "value"})),
    _Selectie("tips", ("tips { type value }",), frozenset({"type", "value"})),
)

# Van de AH-tags alleen de groepen die iets zeggen over het gerecht zelf
# (vegetarisch, gezond, salade, soep). "momenten" ("wat eten we vandaag") en
# "kooktechniek" zouden alleen nieuwe, nietszeggende trefwoorden in Tandoor
# opleveren.
TAG_GROEPEN = frozenset({"speciale-wensen", "soort-gerecht"})

_QUERY = """query Recept($id: Int!) {
  recipe(id: $id) {
    %s
  }
}"""

# graphql-java schrijft "Field 'text' in type 'RecipeIngredient' is undefined"
# en zet het pad in de code erachter: FieldUndefined@[recipe/ingredients/text].
# Andere servers schrijven: Cannot query field "text" on type
# "RecipeIngredient". Did you mean "steps"?
#
# Het pad zegt welke selectie het is: de stap na recipe. Dat is nodig, want
# sommige subvelden staan in meer dan één selectie - `type` zit zowel in
# servings als in tips - en zonder pad zou een onbekende tips.type ook
# servings een vorm terugzetten, en dat voor de rest van de dag. Zonder pad
# nemen we alleen het veld direct na "field": wat er verder tussen
# aanhalingstekens staat is de typenaam of een suggestie, en een suggestie als
# "description" is juist een veld dat wél bestaat.
_PAD_RE = re.compile(r"@\[([A-Za-z0-9_/]+)\]")
_VELD_RE = re.compile(r"""field\s+['"]([A-Za-z_][A-Za-z0-9_]*)['"]""", re.IGNORECASE)


def _berichten(errors) -> list:
    """De foutteksten, in welke vorm AH ze ook stuurt: lijst, dict of tekst."""
    if isinstance(errors, (str, dict)):
        errors = [errors]
    if not isinstance(errors, list):
        return []
    return [str(f.get("message", "")) if isinstance(f, dict) else str(f) for f in errors]


def _onbekende_velden(errors) -> list:
    """(selectie of None, veld) voor elk veld dat AH niet kent."""
    treffers = []
    for bericht in _berichten(errors):
        laag = bericht.lower()
        if "undefined" not in laag and "cannot query" not in laag:
            continue
        if pad := _PAD_RE.search(bericht):
            stappen = [deel for deel in pad.group(1).split("/") if deel]
            if len(stappen) >= 3:                 # recipe/ingredients/text
                treffers.append((stappen[1], stappen[-1]))
                continue
            if len(stappen) == 2:                 # recipe/images
                treffers.append((None, stappen[1]))
                continue
        if veld := _VELD_RE.search(bericht):
            treffers.append((None, veld.group(1)))
    return treffers


def _bouw_query(stand: dict) -> str:
    stukken = [_KERN]
    for selectie in _SELECTIES:
        index = stand[selectie.naam]
        if index < len(selectie.vormen):
            stukken.append(selectie.vormen[index])
    return _QUERY % "\n    ".join(stukken)


def _pas_aan(stand: dict, treffers: list) -> list:
    """Zet selecties een vorm terug of helemaal uit; geeft terug wat er veranderde."""
    veranderd = []
    for selectie in _SELECTIES:
        if stand[selectie.naam] >= len(selectie.vormen):
            continue
        for bij, veld in treffers:
            if bij is None and veld == selectie.naam:
                stand[selectie.naam] = len(selectie.vormen)  # het veld bestaat niet
            elif bij == selectie.naam or (bij is None and veld in selectie.subvelden):
                # Zonder pad weten we niet bij welke selectie een gedeeld
                # subveld hoort, en zetten we ze allebei terug. Beter dan
                # blijven vragen naar iets wat er niet is.
                stand[selectie.naam] += 1                    # kalere vorm proberen
            else:
                continue
            veranderd.append(selectie.naam)
            break                                            # één stap per ronde
    return veranderd


def _graphql(query: str, variables: dict, *, api_url: str, session, bearer: str) -> dict:
    return _post("/graphql", {"query": query, "variables": variables},
                 api_url=api_url, session=session, bearer=bearer,
                 fouten_in_body=True)


# Welke vraag bij deze AH werkt, hoeven we niet bij elk recept opnieuw uit te
# zoeken. Zonder deze cache kost elk recept de rondes die nodig waren om
# erachter te komen dat een veld niet bestaat.
#
# Maar niet voor altijd: na een dag beginnen we weer bij de rijkste vraag. Een
# veld dat AH één keer niet kende - een storing, een halve uitrol - hoort niet
# weken weg te blijven in een container die weken doordraait, en een veld dat
# AH erbij zet, komt er zo vanzelf weer bij.
SCHEMA_GELDIG = 24 * 3600

_stand: dict | None = None
_stand_sinds = 0.0
_stand_slot = threading.Lock()


def reset_schema() -> None:
    """Vergeet welke vraag werkte. Voor de tests."""
    global _stand, _stand_sinds
    with _stand_slot:
        _stand, _stand_sinds = None, 0.0


_ZOEK_TIJD = """query RecipeSearch($query: RecipeSearchParams!) {
  recipeSearch(query: $query) {
    result { id time { cook oven wait } }
  }
}"""


def _tijd_uit_zoeken(recipe_id: int, titel, *, api_url: str, session, bearer: str):
    """
    De oven- en wachttijd van een recept, of None.

    AH geeft die alleen bij een zoekresultaat, dus zoeken we op de titel en
    nemen we het resultaat met hetzelfde nummer. Dezelfde vraag als in de
    openbare client albert-heijn-mcp. Lukt het niet - geen resultaat, een
    andere titel, een fout - dan is er geen wachttijd, en verder niets: het
    recept zelf hebben we al.
    """
    if not titel:
        return None
    try:
        antwoord = _graphql(_ZOEK_TIJD, {"query": {"searchText": str(titel), "size": 25}},
                            api_url=api_url, session=session, bearer=bearer)
    except AhError as e:
        log.info("geen oventijd voor recept %s: zoeken mislukte (%s)", recipe_id, e.reason)
        return None
    zoek = (antwoord.get("data") or {}).get("recipeSearch") if isinstance(
        antwoord.get("data"), dict) else None
    for resultaat in (zoek or {}).get("result") or []:
        if isinstance(resultaat, dict) and resultaat.get("id") == recipe_id \
                and isinstance(resultaat.get("time"), dict):
            return resultaat["time"]
    log.info("geen oventijd voor recept %s: niet gevonden bij het zoeken op de titel",
             recipe_id)
    return None


def fetch_recipe(recipe_id: int, *, api_url: str | None = None, session=None) -> dict:
    """Het recept zoals AH het geeft. Gooit AhError met een leesbare reden."""
    try:
        return _fetch_recipe(recipe_id, api_url=api_url, session=session)
    except AhError as e:
        if e.status not in (401, 403):
            raise
        # Het token was toch verlopen. De brug draait weken achter elkaar, dus
        # dit gebeurt een keer: nieuw token, en nog één poging.
        log.info("AH wees het token af; nieuw token halen")
        reset_token()
        return _fetch_recipe(recipe_id, api_url=api_url, session=session)


def _fetch_recipe(recipe_id: int, *, api_url: str | None = None, session=None) -> dict:
    global _stand, _stand_sinds
    api_url = (api_url or API_URL).rstrip("/")
    eigen = session or requests.Session()
    try:
        bearer = token(api_url=api_url, session=eigen)
        with _stand_slot:
            vers = _stand and time.time() - _stand_sinds < SCHEMA_GELDIG
            stand = dict(_stand) if vers else {s.naam: 0 for s in _SELECTIES}
        # Elke ronde valt er minstens een selectie terug of af, dus dit loopt
        # hoe dan ook af; de grens is er voor het geval AH iets zegt wat we
        # verkeerd begrijpen.
        for _ in range(sum(len(s.vormen) for s in _SELECTIES) + 2):
            antwoord = _graphql(_bouw_query(stand), {"id": int(recipe_id)},
                                api_url=api_url, session=eigen, bearer=bearer)
            data = antwoord.get("data")
            recept = data.get("recipe") if isinstance(data, dict) else None
            fouten = antwoord.get("errors")
            treffers = _onbekende_velden(fouten)
            if isinstance(recept, dict) and recept and not treffers:
                # Ook met een melding erbij: GraphQL mag een half antwoord
                # geven (één veld dat niet laadde), en een recept zonder één
                # veld is nog steeds het recept.
                if fouten:
                    log.warning("AH gaf recept %s met een melding erbij: %s", recipe_id,
                                "; ".join(_berichten(fouten)[:3])[:300])
                with _stand_slot:
                    _stand, _stand_sinds = dict(stand), time.time()
                if not isinstance(recept.get("time"), dict):
                    tijd = _tijd_uit_zoeken(recipe_id, recept.get("title"),
                                            api_url=api_url, session=eigen, bearer=bearer)
                    if tijd:
                        recept = {**recept, "time": tijd}
                return recept
            if not fouten:
                if isinstance(data, dict) and "recipe" in data:
                    raise AhError(f"AH kent recept {recipe_id} niet")
                raise AhError("AH gaf een leeg antwoord")
            if not treffers or not _pas_aan(stand, treffers):
                melding = "; ".join(_berichten(fouten)[:3])
                raise AhError(f"AH weigerde de vraag: {melding[:300]}")
            log.info("AH kent %s niet (meer); vraag aangepast",
                     ", ".join(sorted(f"{bij}.{veld}" if bij else veld
                                      for bij, veld in treffers)))
        raise AhError("AH bleef de vraag weigeren")
    finally:
        if session is None:
            eigen.close()


# -- naar een pagina ------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
# NUL en de andere stuurtekens. sanitise_html() in tandoor.py haalt een NUL
# uit de HTML, maar in het JSON-LD staat hij als \u0000 - zes gewone tekens
# die daar niet op lijken. Tandoor pakt het JSON daarna uit en heeft de NUL
# dan alsnog, en die weigert de database. Dus hier al weg.
_STUUR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Een tag wordt een spatie, anders plakken "tips</a>.<br>Bak" aan elkaar; maar
# dan staat er een spatie voor de punt, en die haalt niemand er later uit.
_VOOR_LEESTEKEN_RE = re.compile(r"\s+([,.;:!?)])")

# AH geeft de portiesoort als code terug. Alleen wat we kennen zetten we om;
# een onbekende code laten we weg, want een verkeerd woord achter het getal is
# erger dan geen woord.
_PORTIES = {
    "PERSON": "personen", "PERSONS": "personen", "PEOPLE": "personen",
    "PIECE": "stuks", "PIECES": "stuks",
    "PORTION": "porties", "PORTIONS": "porties", "SERVING": "porties",
}


def _lijst(waarde) -> list:
    """Een AH-veld als lijst. Een losse tekst is één element, geen rij letters."""
    if isinstance(waarde, list):
        return waarde
    if isinstance(waarde, (str, dict)) and waarde:
        return [waarde]
    return []


def _veld(recept: dict, naam: str) -> dict:
    waarde = recept.get(naam)
    return waarde if isinstance(waarde, dict) else {}


def _tekst(waarde) -> str:
    """Een AH-veld naar platte tekst: er zitten links en entiteiten in."""
    if not waarde or isinstance(waarde, (dict, list)):
        return ""
    plat = htmllib.unescape(_TAG_RE.sub(" ", str(waarde)))
    plat = re.sub(r"\s+", " ", _STUUR_RE.sub("", plat)).strip()
    return _VOOR_LEESTEKEN_RE.sub(r"\1", plat)


def _getal(waarde) -> str:
    """4.0 wordt "4", 1.5 wordt "1,5" - zoals AH het op de pagina zet."""
    try:
        getal = float(str(waarde).replace(",", "."))
        if getal == int(getal):
            return str(int(getal))
    except (TypeError, ValueError, OverflowError):     # ook inf en nan
        return ""
    return f"{getal:g}".replace(".", ",")


def _ingredient_regel(ingredient: dict) -> str:
    regel = _tekst(ingredient.get("text"))
    if regel:
        return regel
    # Zonder `text` zelf samenstellen, en dan oppassen: Tandoors parser maakt
    # van het tweede woord de eenheid zodra er drie of meer zijn
    # (cookbook/helper/ingredient_parser.py). "400 zwarte bonen" wordt dan 400,
    # eenheid "zwarte", ingrediënt "bonen" - een verzonnen eenheid in je ruimte
    # en een Food Alias die niet vuurt. Bij een naam van meer woorden zetten we
    # het getal daarom tussen haakjes erachter: dan houdt Tandoor de naam heel
    # en komt het getal in de notitie. De hoeveelheid zelf is dan weg, maar
    # die kan je zien; een verkeerde eenheid valt pas op als het misgaat.
    naam = _veld(ingredient, "name")
    aantal = _getal(ingredient.get("quantity"))
    enkel, meervoud = _tekst(naam.get("singular")), _tekst(naam.get("plural"))
    woord = meervoud if (meervoud and aantal not in ("", "1")) else enkel
    if not woord:
        return ""
    if not aantal:
        return woord
    if len(woord.split()) == 1:
        return f"{aantal} {woord}"
    return f"{woord} ({aantal})"


def _stappen(recept: dict) -> list:
    bereiding = _veld(recept, "preparation")
    for sleutel in ("steps", "summary"):
        regels = [_tekst(stap) for stap in _lijst(bereiding.get(sleutel))]
        if regels := [regel for regel in regels if regel]:
            return regels
    return []


def _tips(recept: dict) -> list:
    tips = []
    for tip in _lijst(recept.get("tips")):
        tekst = _tekst(tip.get("value")) if isinstance(tip, dict) else _tekst(tip)
        if tekst:
            tips.append(f"Tip: {tekst}")
    return tips


def _afbeeldingen(recept: dict) -> list:
    beelden = [b for b in _lijst(recept.get("images"))
               if isinstance(b, dict) and isinstance(b.get("url"), str) and b["url"]]

    def breedte(beeld):
        # width, anders height, anders de ?width= die AH in de URL zet.
        for maat in (beeld.get("width"), beeld.get("height"),
                     (re.search(r"[?&]width=(\d+)", beeld["url"]) or [None, None])[1]):
            try:
                if maat and float(maat) > 0:
                    return float(maat)
            except (TypeError, ValueError):
                continue
        return 0.0

    beelden.sort(key=breedte, reverse=True)
    return list(dict.fromkeys(beeld["url"] for beeld in beelden))[:4]


def _porties(recept: dict) -> str:
    porties = _veld(recept, "servings")
    try:
        getal = float(str(porties.get("number")).replace(",", "."))
        # Tandoor leest alleen het eerste gehele getal (parse_servings), dus
        # "1,5 porties" werd 1 portie met de tekst ",5 porties". Afronden.
        aantal = max(int(getal + 0.5), 1) if getal > 0 else 0
    except (TypeError, ValueError, OverflowError):
        aantal = 0
    if not aantal:
        return ""
    soort = str(porties.get("type") or "").strip()
    woord = _PORTIES.get(soort.upper(), "" if soort.isupper() else soort.lower())
    return f"{aantal} {woord}".strip()


def _minuten(waarde) -> int:
    try:
        return max(int(float(waarde)), 0)
    except (TypeError, ValueError, OverflowError):
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
    tijd = _veld(recept, "time")
    bereiden = _minuten(tijd.get("cook")) or _minuten(recept.get("cookTime"))
    wachten = _minuten(tijd.get("oven")) + _minuten(tijd.get("wait"))
    return bereiden, wachten


def to_jsonld(recept: dict, url: str) -> dict:
    """Het recept als schema.org-Recipe, zoals AH het zelf op de pagina zet."""
    data = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": _tekst(recept.get("title")),
        "url": url,
    }
    if beschrijving := _tekst(recept.get("description")):
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
    regels = [_ingredient_regel(i) for i in _lijst(recept.get("ingredients"))
              if isinstance(i, dict)]
    data["recipeIngredient"] = [regel for regel in regels if regel]
    # De tips komen als laatste stap(pen), na de echte. Niet in de
    # beschrijving: Tandoor knipt die af op 512 tekens en zet hem boven de
    # 256 ook nog eens voor stap 1, zodat je hem twee keer leest. Achteraan
    # verschuift de nummering van de echte stappen niet.
    data["recipeInstructions"] = [{"@type": "HowToStep", "text": stap}
                                  for stap in _stappen(recept) + _tips(recept)]
    trefwoorden = [_tekst(woord) for sleutel in ("courses", "cuisines")
                   for woord in _lijst(recept.get(sleutel))]
    trefwoorden += [_tekst(tag.get("value")) for tag in _lijst(recept.get("tags"))
                    if isinstance(tag, dict) and tag.get("key") in TAG_GROEPEN]
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
