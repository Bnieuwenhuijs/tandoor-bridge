"""
Tests voor de AH-route, tegen de nagebouwde AH-API op localhost.

Geen enkele test raakt api.ah.nl.
"""

import json
import re
import time

import pytest

import ah_api
import mock_ah
import mock_tandoor
from conftest import MockServer


@pytest.fixture(autouse=True)
def leeg_token():
    # Het token en de uitgezochte vraag zitten in een modulecache; zonder dit
    # lekken ze van test naar test.
    ah_api.reset_token()
    ah_api.reset_schema()
    yield
    ah_api.reset_token()
    ah_api.reset_schema()


def ah_server(**opties) -> MockServer:
    return MockServer(mock_ah.create_app(**opties))


@pytest.fixture
def ah():
    server = ah_server()
    try:
        yield server
    finally:
        server.stop()


URL = "https://www.ah.nl/allerhande/recept/R-R1194169/traybake-met-kip"


def jsonld(html: str) -> dict:
    gevonden = re.search(r'application/ld\+json">\s*(.*?)\s*</script>', html, re.S)
    assert gevonden, "geen JSON-LD in de pagina"
    return json.loads(gevonden.group(1))


# -- links -----------------------------------------------------------------

@pytest.mark.parametrize("url, nummer", [
    ("https://www.ah.nl/allerhande/recept/R-R1194169/traybake-met-kip", 1194169),
    ("https://www.ah.nl/allerhande/recept/R-R921580", 921580),
    ("https://www.ah.nl/allerhande/recept/r-r921580/kip", 921580),
    ("https://ah.nl/r/1196876", 1196876),
    ("https://www.ah.nl/producten/product/wi123", None),
    ("https://www.leukerecepten.nl/recepten/kip-korma/", None),
    ("https://www.nietah.nl/allerhande/recept/R-R1/x", None),
    ("", None),
])
def test_receptnummer_uit_de_link(url, nummer):
    assert ah_api.recipe_id_from_url(url) == nummer


def test_een_host_die_alleen_op_ah_nl_eindigt_is_geen_ah():
    # kwaad-ah.nl eindigt op ah.nl; dat mag geen AH-link worden.
    assert ah_api.recipe_id_from_url("https://kwaadah.nl/allerhande/recept/R-R1") is None


# -- token -----------------------------------------------------------------

def test_token_wordt_hergebruikt(ah):
    eerste = ah_api.token(api_url=ah.url)
    tweede = ah_api.token(api_url=ah.url)
    assert eerste == tweede == "mock-anoniem-token"
    tokenvragen = [r for r in gezien(ah) if r[0] == "token"]
    assert len(tokenvragen) == 1


def test_token_zonder_access_token_is_een_leesbare_fout():
    server = ah_server(geen_token=True)
    try:
        with pytest.raises(ah_api.AhError, match="geen token"):
            ah_api.token(api_url=server.url)
    finally:
        server.stop()


def test_een_onbereikbare_api_geeft_geen_stacktrace():
    with pytest.raises(ah_api.AhError, match="niet bereikbaar"):
        # Poort 1 doet niets; de verbinding wordt direct geweigerd.
        ah_api.token(api_url="http://127.0.0.1:1")


def gezien(server) -> list:
    import requests
    return requests.get(f"{server.url}/__mock/gezien").json()


# -- het recept ------------------------------------------------------------

def test_recept_ophalen(ah):
    recept = ah_api.fetch_recipe(1194169, api_url=ah.url)
    assert recept["title"].startswith("Traybake")
    assert recept["ingredients"][0]["text"] == "600 g kipfilet"
    # In een rondje: alle velden bestaan, dus er is niets bij te stellen.
    assert len([r for r in gezien(ah) if r[0] == "graphql" and "recipeSearch" not in r[1]]) == 1


@pytest.mark.parametrize("weigering_als_graphql", [False, True],
                         ids=["kale-401", "graphql-401"])
def test_een_verlopen_token_wordt_vernieuwd(weigering_als_graphql):
    # Zo ziet het eruit als het token onderweg verloopt: AH antwoordt met 401.
    # In beide vormen: {"message": ...}, en de gewone GraphQL-vorm met
    # {"errors": [...]} - die laatste mag niet als uitleg over de vraag gelezen
    # worden, anders komt er nooit een nieuw token.
    server = ah_server(weigering_als_graphql=weigering_als_graphql)
    ah_api._token = ah_api._Token("van-gisteren", time.time() + 3600)
    try:
        recept = ah_api.fetch_recipe(1194169, api_url=server.url)
        tokens = [r for r in gezien(server) if r[0] == "token"]
    finally:
        server.stop()
    assert recept["title"].startswith("Traybake")
    assert len(tokens) == 1          # het oude zat in de cache; één nieuw gehaald


def test_onbekend_recept(ah):
    with pytest.raises(ah_api.AhError, match="kent recept 999 niet"):
        ah_api.fetch_recipe(999, api_url=ah.url)


def test_velden_die_ah_niet_kent_gaan_eruit():
    # Het veldenpakket van de ene openbare client: geen images, geen tips,
    # geen summary, geen plural. De brug hoort dan gewoon een kaler recept te
    # leveren in plaats van te struikelen.
    schema = {naam: dict(velden) for naam, velden in mock_ah.SCHEMA.items()}
    for veld in ("images", "tips"):
        schema["Recipe"].pop(veld)
    schema["Preparation"].pop("summary")
    schema["IngredientName"].pop("plural")
    server = ah_server(schema=schema)
    try:
        html, recept = ah_api.fetch_page_html(URL, api_url=server.url)
        rondes = len([r for r in gezien(server) if r[0] == "graphql" and "recipeSearch" not in r[1]])
        # Tweede recept: nu weet de brug al welke vraag werkt.
        ah_api.fetch_recipe(1194169, api_url=server.url)
        daarna = len([r for r in gezien(server) if r[0] == "graphql" and "recipeSearch" not in r[1]]) - rondes
    finally:
        server.stop()

    assert "images" not in recept and "tips" not in recept
    data = jsonld(html)
    assert "image" not in data
    # Wat er wél is, is er nog steeds: de hele ingrediëntregel vooral.
    assert data["recipeIngredient"][0] == "600 g kipfilet"
    assert data["recipeYield"] == "4 personen"
    assert len(data["recipeInstructions"]) == 3
    assert daarna == 1


@pytest.mark.parametrize("oude_foutvorm, fout_status", [
    (True, 400),            # 'Cannot query field "x" on type "Y"'
    (False, 200),           # wat de meeste GraphQL-servers doen
    (True, 200),
])
def test_de_andere_foutvormen_worden_ook_begrepen(oude_foutvorm, fout_status):
    schema = {naam: dict(velden) for naam, velden in mock_ah.SCHEMA.items()}
    schema["Recipe"].pop("images")
    server = ah_server(schema=schema, oude_foutvorm=oude_foutvorm,
                       fout_status=fout_status)
    try:
        html, _ = ah_api.fetch_page_html(URL, api_url=server.url)
    finally:
        server.stop()
    assert "image" not in jsonld(html)


class _Antwoord:
    """Net genoeg van een requests-antwoord voor _post()."""

    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Sessie:
    def __init__(self, antwoord):
        self.antwoord = antwoord

    def post(self, *args, **kwargs):
        return self.antwoord


@pytest.mark.parametrize("status, body", [
    (400, {"message": "bad request"}),     # 400 zonder uitleg in "errors"
    (400, ValueError("geen json")),        # 400 met een HTML-foutpagina
    (500, {"errors": [{"message": "x"}]}),  # alleen 400 draagt een vraag-uitleg
    (401, {"errors": [{"message": "Unauthorized"}]}),
])
def test_een_echte_fout_houdt_zijn_status(status, body):
    with pytest.raises(ah_api.AhError) as fout:
        ah_api._post("/graphql", {}, api_url="http://x", session=_Sessie(_Antwoord(status, body)),
                     fouten_in_body=True)
    assert fout.value.status == status


def test_een_afgewezen_vraag_met_400_komt_als_uitleg_terug():
    body = {"errors": [{"message": "Field 'x' in type 'Recipe' is undefined"}]}
    assert ah_api._post("/graphql", {}, api_url="http://x",
                        session=_Sessie(_Antwoord(400, body)), fouten_in_body=True) == body


def test_een_onbekend_recept_zegt_dat_ook(ah):
    with pytest.raises(ah_api.AhError, match="kent recept 42 niet"):
        ah_api.fetch_recipe(42, api_url=ah.url)


def test_een_leeg_antwoord_is_niet_hetzelfde_als_een_onbekend_recept():
    server = ah_server(leeg_antwoord=True)
    try:
        with pytest.raises(ah_api.AhError, match="leeg antwoord"):
            ah_api.fetch_recipe(1194169, api_url=server.url)
    finally:
        server.stop()


def test_zonder_ingredientregel_bouwen_we_er_zelf_een():
    schema = {naam: dict(velden) for naam, velden in mock_ah.SCHEMA.items()}
    schema["RecipeIngredient"].pop("text")
    server = ah_server(schema=schema)
    try:
        html, _ = ah_api.fetch_page_html(URL, api_url=server.url)
    finally:
        server.stop()
    regels = jsonld(html)["recipeIngredient"]
    # Het gaat om wat Tandoor ervan maakt, niet om de tekst: de mock ontleedt
    # met dezelfde regel als Tandoor (het tweede van drie woorden wordt de
    # eenheid). Er mag dus nooit een eenheid uit een naam ontstaan.
    tandoor = [mock_tandoor._ingredient(regel) for regel in regels]
    assert all(i["unit"] is None for i in tandoor), [i["unit"] for i in tandoor]
    # Een naam van één woord houdt z'n getal.
    assert (tandoor[0]["amount"], tandoor[0]["food"]["name"]) == (600.0, "kipfilet")
    # Bij meer woorden blijft de naam heel en gaat het getal in de notitie -
    # met het meervoud zodra het er meer dan één is.
    assert (tandoor[1]["food"]["name"], tandoor[1]["note"]) == ("rode paprika's", "2")
    assert (tandoor[3]["food"]["name"], tandoor[3]["note"]) == ("zwarte bonen", "400")


def test_bij_een_hoeveelheid_van_een_blijft_het_enkelvoud():
    regel = ah_api._ingredient_regel(
        {"quantity": 1, "name": {"singular": "rode paprika", "plural": "rode paprika's"}})
    assert regel == "rode paprika (1)"
    assert ah_api._ingredient_regel(
        {"quantity": None, "name": {"singular": "zout", "plural": None}}) == "zout"


def test_een_weigering_die_niet_over_velden_gaat_blijft_een_fout():
    schema = {naam: dict(velden) for naam, velden in mock_ah.SCHEMA.items()}
    schema["Recipe"] = {}            # niets bestaat, ook id en title niet
    server = ah_server(schema=schema)
    try:
        with pytest.raises(ah_api.AhError, match="weigerde de vraag"):
            ah_api.fetch_recipe(1194169, api_url=server.url)
    finally:
        server.stop()


def _schema_zonder(**weg):
    """Een kopie van het mockschema met per type de velden die er niet zijn."""
    schema = {naam: dict(velden) for naam, velden in mock_ah.SCHEMA.items()}
    for typenaam, velden in weg.items():
        for veld in velden:
            schema[typenaam].pop(veld)
    return schema


@pytest.mark.parametrize("weg, kapot, heel", [
    # `type` zit in servings én in tips. Een onbekende tips.type mag servings
    # niet raken, en andersom. Het pad in de foutmelding zegt welke het is.
    ({"Tip": ["type"]}, "tips", "servings"),
    ({"Servings": ["type"]}, "servings", "tips"),
])
def test_een_gedeeld_subveld_raakt_alleen_zijn_eigen_selectie(weg, kapot, heel):
    server = ah_server(schema=_schema_zonder(**weg))
    try:
        html, _ = ah_api.fetch_page_html(URL, api_url=server.url)
    finally:
        server.stop()
    data = jsonld(html)
    if heel == "servings":
        assert data["recipeYield"] == "4 personen"
    else:
        assert data["recipeInstructions"][-1]["text"].startswith("Tip: ")
    assert ah_api._stand[heel] == 0


def test_een_suggestie_in_de_foutmelding_is_geen_onbekend_veld():
    # graphql-js zet er "Did you mean ..." achter, met velden die wél bestaan.
    # Die mogen er niet uit vallen.
    server = ah_server(schema=_schema_zonder(Preparation=["summary"]),
                       oude_foutvorm=True, suggesties=True)
    try:
        html, _ = ah_api.fetch_page_html(URL, api_url=server.url)
    finally:
        server.stop()
    data = jsonld(html)
    assert "één plaat" in data["description"]
    assert len(data["recipeInstructions"]) == 4


def test_een_half_antwoord_levert_toch_het_recept():
    # Een veld dat niet laadde (een resolver die faalde) staat in "errors",
    # naast een recept dat verder compleet is.
    server = ah_server(half_antwoord=True)
    try:
        recept = ah_api.fetch_recipe(1194169, api_url=server.url)
    finally:
        server.stop()
    assert recept["ingredients"][0]["text"] == "600 g kipfilet"


def test_een_uitgezochte_vraag_wordt_na_een_dag_opnieuw_geprobeerd(monkeypatch):
    server = ah_server(schema=_schema_zonder(Recipe=["images"]))
    try:
        ah_api.fetch_recipe(1194169, api_url=server.url)
        assert ah_api._stand["images"] == len(ah_api._SELECTIES[5].vormen)
        rondes = len([r for r in gezien(server) if r[0] == "graphql" and "recipeSearch" not in r[1]])
        # Binnen de dag: meteen de uitgezochte vraag, in één ronde.
        ah_api.fetch_recipe(1194169, api_url=server.url)
        assert len([r for r in gezien(server) if r[0] == "graphql" and "recipeSearch" not in r[1]]) == rondes + 1
        # Een dag later: weer van de rijkste vraag af, dus images wordt opnieuw
        # gevraagd.
        monkeypatch.setattr(ah_api, "_stand_sinds", time.time() - ah_api.SCHEMA_GELDIG - 1)
        ah_api.fetch_recipe(1194169, api_url=server.url)
        vragen = [r[1] for r in gezien(server) if r[0] == "graphql" and "recipeSearch" not in r[1]]
    finally:
        server.stop()
    assert "images" in vragen[rondes + 1]


@pytest.mark.parametrize("errors", [
    "boom", {"message": "boom"}, [None, 3, "boom"], 42, None,
])
def test_rare_errors_geven_geen_uitzondering(errors):
    assert ah_api._onbekende_velden(errors) == []


def test_kent_ah_width_niet_dan_wordt_het_de_hoogste_foto():
    server = ah_server(schema=_schema_zonder(Image=["width"]))
    try:
        html, _ = ah_api.fetch_page_html(URL, api_url=server.url)
    finally:
        server.stop()
    assert jsonld(html)["image"][0].endswith("width=1200")


# -- de pagina -------------------------------------------------------------

def test_de_pagina_ziet_eruit_als_de_echte(ah):
    html, _ = ah_api.fetch_page_html(URL, api_url=ah.url)
    data = jsonld(html)

    assert data["@type"] == "Recipe"
    assert data["name"] == "Traybake met kip, paprika, zwarte bonen en chipotle"
    assert data["url"] == URL
    # De hele regel, inclusief eenheid: Tandoor ontleedt, wij niet.
    assert data["recipeIngredient"] == [
        "600 g kipfilet", "2 rode paprika's",
        "1½ el chipotlepasta", "400 g zwarte bonen",
    ]
    assert data["recipeYield"] == "4 personen"
    # Bereiden naar prepTime (Tandoors werktijd), de oventijd - die AH alleen
    # bij een zoekresultaat geeft - naar cookTime (Tandoors wachttijd).
    assert data["prepTime"] == "PT20M"
    assert data["cookTime"] == "PT15M"
    assert data["totalTime"] == "PT35M"
    assert data["recipeInstructions"][0] == {
        "@type": "HowToStep", "text": "Verwarm de oven voor op 200 °C."}
    # De grootste afbeelding eerst: die wil Tandoor als foto.
    assert data["image"][0].endswith("width=1200")
    # Van de tags alleen speciale-wensen en soort-gerecht; hoofdgerecht stond
    # er via courses al in en komt er niet dubbel bij.
    assert data["keywords"] == "hoofdgerecht, mexicaans, glutenvrij, traybake"
    # De tip is de laatste stap, na de echte drie. Niet in de beschrijving:
    # die knipt Tandoor af op 512 tekens en zet hij boven de 256 nog eens voor
    # stap 1 (get_from_scraper).
    assert len(data["recipeInstructions"]) == 4
    assert data["recipeInstructions"][-1]["text"] == "Tip: Lekker met een lik zure room."
    assert "Tip:" not in data["description"]
    # Entiteiten zijn opgelost en de <a> uit de beschrijving is weg.
    assert "één plaat" in data["description"] and "<a" not in data["description"]

    # En de pagina zelf is een pagina: fetcher/Tandoor moeten hem herkennen.
    assert html.startswith("<!doctype html>")
    assert f'<link rel="canonical" href="{URL}">' in html


def test_de_canonical_en_het_schema_overleven_de_route_naar_tandoor():
    from fetcher import describe_page
    from tandoor import prepare_data

    html = ah_api.to_html(mock_ah.RECEPT, URL)
    assert describe_page(html)["heeft_schema"]
    # prepare_data() plakt geen tweede canonical erin.
    assert prepare_data(html, URL).count("rel=") == 1


def test_json_kan_niet_uit_zijn_script_breken():
    stout = dict(mock_ah.RECEPT, title="Kip </script><script>alert(1)</script>")
    html = ah_api.to_html(stout, URL)
    # Buiten onze eigen tag zit er geen <script> in de pagina.
    assert html.count("<script") == 1 and html.count("</script>") == 1
    # Tags uit een AH-veld gaan er hoe dan ook al uit.
    assert jsonld(html)["name"] == "Kip alert(1)"


def test_een_losse_punthaak_blijft_staan_maar_breekt_niets():
    # "< 10 minuten" is geen tag, dus die tekst hoort heel te blijven - en het
    # JSON eromheen hoort er niet van om te vallen.
    html = ah_api.to_html({"id": 7, "title": "Soep in < 10 minuten"}, URL)
    assert html.count("<script") == 1 and html.count("</script>") == 1
    assert "\\u003c" in html
    assert jsonld(html)["name"] == "Soep in < 10 minuten"


def test_een_recept_zonder_bijna_alles_levert_nog_een_pagina():
    html = ah_api.to_html({"id": 7, "title": "Kaal"}, URL)
    data = jsonld(html)
    assert data["name"] == "Kaal"
    assert data["recipeIngredient"] == [] and data["recipeInstructions"] == []
    assert "recipeYield" not in data and "cookTime" not in data


@pytest.mark.parametrize("porties, verwacht", [
    ({"number": 4, "type": "PERSON"}, "4 personen"),
    ({"number": 12, "type": "PIECE"}, "12 stuks"),
    ({"number": 2, "type": "personen"}, "2 personen"),
    # Een code die we niet kennen laten we weg: liever geen woord dan het
    # verkeerde woord achter het getal.
    ({"number": 4, "type": "IETS_NIEUWS"}, "4"),
    # Tandoor leest alleen een geheel getal: "1,5 porties" werd daar 1 portie
    # met de tekst ",5 porties". Dus afronden.
    ({"number": 1.5, "type": "PORTION"}, "2 porties"),
    ({"number": "2,4", "type": "PERSON"}, "2 personen"),
    ({"number": 0.2, "type": "PERSON"}, "1 personen"),
    ({"number": None, "type": "PERSON"}, ""),
    ({}, ""),
])
def test_porties(porties, verwacht):
    assert ah_api._porties({"servings": porties}) == verwacht


@pytest.mark.parametrize("recept, verwacht", [
    # Wat AH vandaag op recipe(id:) geeft.
    ({"cookTime": 25}, (25, 0)),
    ({"cookTime": None}, (0, 0)),
    ({"cookTime": "een halfuur"}, (0, 0)),
    ({}, (0, 0)),
    # En de gesplitste vorm, voor als AH die hier ooit ook geeft.
    ({"time": {"cook": 20, "oven": 15, "wait": 0}}, (20, 15)),
    ({"time": {"cook": 20, "oven": 0, "wait": 60}}, (20, 60)),
    ({"time": {"cook": 0}, "cookTime": 25}, (25, 0)),
])
def test_tijden(recept, verwacht):
    assert ah_api._tijden(recept) == verwacht


def test_zonder_stappen_vallen_we_terug_op_de_samenvatting():
    recept = {"preparation": {"steps": [], "summary": ["Alles in de oven."]}}
    assert ah_api._stappen(recept) == ["Alles in de oven."]


def test_een_nul_komt_niet_via_het_json_alsnog_in_tandoor():
    # sanitise_html() haalt een NUL uit de HTML, maar in het JSON staat hij als
    # \u0000 en dat ziet hij niet. Tandoor pakt het JSON uit en heeft hem dan
    # toch - en de database weigert hem.
    html = ah_api.to_html({"id": 7, "title": "Kip\x00korma", "ingredients": [
        {"text": "1 ui\x07"}]}, URL)
    assert "\\u0000" not in html and "\\u0007" not in html
    assert jsonld(html)["name"] == "Kipkorma"


@pytest.mark.parametrize("ruw, plat", [
    ('Lees onze <a href=x>tips</a>.<br>Bak nu', "Lees onze tips. Bak nu"),
    ("Soep in < 10 minuten", "Soep in < 10 minuten"),
    ("1&#189; el", "1½ el"),
    ({"tekst": "geen tekst"}, ""),
])
def test_tekst(ruw, plat):
    assert ah_api._tekst(ruw) == plat


def test_een_losse_tekst_wordt_niet_letter_voor_letter_gelezen():
    # Komt er een tekst waar een lijst hoort, dan is dat één stap - geen stap
    # per letter.
    data = ah_api.to_jsonld({"title": "x", "preparation": {"steps": "Alles in de oven."},
                             "courses": "hoofdgerecht", "ingredients": [{"text": "1 ui"}]}, URL)
    assert data["recipeInstructions"] == [{"@type": "HowToStep", "text": "Alles in de oven."}]
    assert data["keywords"] == "hoofdgerecht"


def test_een_vreemde_maat_breekt_de_omzetting_niet():
    data = ah_api.to_jsonld({"title": "x", "cookTime": float("inf"),
                             "servings": {"number": float("nan")},
                             "ingredients": [{"quantity": float("inf"),
                                              "name": {"singular": "ui"}}]}, URL)
    assert data["recipeIngredient"] == ["ui"]
    assert "recipeYield" not in data and "prepTime" not in data



# -- oven- en wachttijd ----------------------------------------------------

def test_de_oventijd_komt_uit_het_zoekresultaat_met_hetzelfde_nummer(ah):
    recept = ah_api.fetch_recipe(1194169, api_url=ah.url)
    # Niet die van "Iets anders" (oven 99), dat de zoekopdracht ook vindt.
    assert recept["time"] == {"cook": 20, "oven": 15, "wait": None}
    assert ah_api._tijden(recept) == (20, 15)


@pytest.mark.parametrize("opties", [{"zoek_tijd": None}, {"zoeken_mislukt": True}])
def test_zonder_zoekresultaat_is_er_geen_wachttijd_maar_wel_een_recept(opties):
    server = ah_server(**opties)
    try:
        html, recept = ah_api.fetch_page_html(URL, api_url=server.url)
    finally:
        server.stop()
    data = jsonld(html)
    assert data["prepTime"] == "PT20M" and "cookTime" not in data
    assert data["recipeIngredient"][0] == "600 g kipfilet"


def test_de_wachttijd_telt_op_bij_de_oventijd():
    # "wait" is marineren, laten rijzen, laten opstijven: ook wachttijd.
    assert ah_api._tijden({"time": {"cook": 15, "oven": 30, "wait": 60}}) == (15, 90)
