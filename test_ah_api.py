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
    assert len([r for r in gezien(ah) if r[0] == "graphql"]) == 1


def test_een_verlopen_token_wordt_vernieuwd(ah):
    # Zo ziet het eruit als het token onderweg verloopt: AH antwoordt met 401.
    ah_api._token = ah_api._Token("van-gisteren", time.time() + 3600)
    recept = ah_api.fetch_recipe(1194169, api_url=ah.url)
    assert recept["title"].startswith("Traybake")


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
        rondes = len([r for r in gezien(server) if r[0] == "graphql"])
        # Tweede recept: nu weet de brug al welke vraag werkt.
        ah_api.fetch_recipe(1194169, api_url=server.url)
        daarna = len([r for r in gezien(server) if r[0] == "graphql"]) - rondes
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


def test_een_echte_fout_blijft_een_fout_ook_met_status_400():
    # Geen "errors" in het antwoord: dan is 400 gewoon een 400.
    server = ah_server(recept={"id": 1194169, "title": "x"})
    try:
        with pytest.raises(ah_api.AhError):
            ah_api.fetch_recipe(42, api_url=server.url)
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
    # Geen eenheid - die geeft de API niet - maar wel het getal en de naam,
    # en het meervoud zodra het er meer dan een is.
    assert regels[0] == "600 kipfilet"
    assert regels[1] == "2 rode paprika's"
    assert regels[3] == "400 zwarte bonen"


def test_een_weigering_die_niet_over_velden_gaat_blijft_een_fout():
    schema = {naam: dict(velden) for naam, velden in mock_ah.SCHEMA.items()}
    schema["Recipe"] = {}            # niets bestaat, ook id en title niet
    server = ah_server(schema=schema)
    try:
        with pytest.raises(ah_api.AhError, match="weigerde de vraag"):
            ah_api.fetch_recipe(1194169, api_url=server.url)
    finally:
        server.stop()


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
    # De tijd gaat naar prepTime, want Tandoor maakt daar working_time van.
    # cookTime zou in de wachttijd belanden, en dan staat de bereidingstijd in
    # Tandoor op nul.
    assert data["prepTime"] == "PT20M"
    assert "cookTime" not in data
    assert data["recipeInstructions"][0] == {
        "@type": "HowToStep", "text": "Verwarm de oven voor op 200 °C."}
    # De grootste afbeelding eerst: die wil Tandoor als foto.
    assert data["image"][0].endswith("width=1200")
    assert data["keywords"] == "hoofdgerecht, mexicaans"
    # De tip staat onder de beschrijving en niet tussen de stappen, want dan
    # zou de nummering verschuiven.
    assert "Tip: Lekker met een lik zure room." in data["description"]
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
    ({"number": 1.5, "type": "PORTION"}, "1,5 porties"),
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
