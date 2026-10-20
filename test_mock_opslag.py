"""
De nagebouwde Tandoor werkt geneste stappen en ingrediënten bij zoals Tandoor.

Dit zijn geen tests van de brug maar van de mock. Ze leggen vast wat er in de
broncode van Tandoor 2.6.9 en drf-writable-nested 0.7.2 staat (zie de uitleg
bovenin mock_opslag.py), zodat een test van de brug tegen deze mock iets
zegt over de echte Tandoor. Wijkt de mock hier af, dan kan een fout in de
brug er ongezien doorheen.
"""

import pytest
import requests

from conftest import mock_server

KOP = {"Authorization": "Bearer tda_test"}


@pytest.fixture
def tandoor():
    server = mock_server()
    try:
        yield server
    finally:
        server.stop()


def maak(tandoor, naam="Soep", stappen=None) -> dict:
    stappen = stappen or [
        {"instruction": "Snipper de ui.", "ingredients": [
            {"food": {"name": "ui"}, "unit": None, "amount": 1, "order": 0},
            {"food": {"name": "wortel"}, "unit": {"name": "g"}, "amount": 200, "order": 1},
        ]},
        {"instruction": "Kook de wortel.", "ingredients": []},
    ]
    antwoord = requests.post(f"{tandoor.url}/api/recipe/", headers=KOP,
                             json={"name": naam, "steps": stappen})
    assert antwoord.status_code == 201, antwoord.text
    return antwoord.json()


def patch(tandoor, recept_id, body):
    return requests.patch(f"{tandoor.url}/api/recipe/{recept_id}/", headers=KOP, json=body)


def test_een_nieuwe_stap_met_alleen_het_id_van_een_ingredient_is_een_400(tandoor):
    # Een nieuwe stap is nooit partial, ook niet voor de bestaande
    # ingrediënten eronder: dan zijn food, unit en amount verplicht.
    recept = maak(tandoor)
    ui = recept["steps"][0]["ingredients"][0]["id"]
    antwoord = patch(tandoor, recept["id"], {"steps": [
        {"id": recept["steps"][0]["id"], "instruction": "al gewijzigd"},
        {"instruction": "nieuw", "ingredients": [{"id": ui}]},
    ]})
    assert antwoord.status_code == 400
    assert antwoord.json()["steps"][1]["ingredients"][0]["food"] == ["This field is required."]
    # En er is geen transactie: de eerste stap is al wel bijgewerkt. Alleen
    # de koppeling aan het recept is niet veranderd.
    nu = requests.get(f"{tandoor.url}/api/recipe/{recept['id']}/", headers=KOP).json()
    assert nu["steps"][0]["instruction"] == "al gewijzigd"
    assert len(nu["steps"]) == 2


def test_een_bestaand_ingredient_wordt_partial_bijgewerkt(tandoor):
    recept = maak(tandoor)
    eerste, tweede = recept["steps"]
    wortel = eerste["ingredients"][1]
    antwoord = patch(tandoor, recept["id"], {"steps": [
        {"id": eerste["id"], "ingredients": [{"id": eerste["ingredients"][0]["id"], "order": 0}]},
        {"id": tweede["id"], "ingredients": [{"id": wortel["id"], "order": 0}]},
    ]})
    assert antwoord.status_code == 200, antwoord.text
    nu = antwoord.json()
    assert [i["food"]["name"] for i in nu["steps"][1]["ingredients"]] == ["wortel"]
    # Alleen de koppeling en de volgorde: hoeveelheid en food zijn ongemoeid.
    assert nu["steps"][1]["ingredients"][0]["amount"] == 200
    staat = tandoor.state()["tellingen"]
    assert staat["food_schrijfacties"] == 0
    assert staat["ingredienten_dubbel_gekoppeld"] == 0


def test_een_weggelaten_stap_wordt_losgekoppeld_niet_verwijderd(tandoor):
    recept = maak(tandoor)
    voor = tandoor.state()["tellingen"]["stappen"]
    antwoord = patch(tandoor, recept["id"], {"steps": [{"id": recept["steps"][0]["id"]}]})
    assert antwoord.status_code == 200
    assert len(antwoord.json()["steps"]) == 1
    assert tandoor.state()["tellingen"]["stappen"] == voor


def test_een_id_wordt_overal_opgezocht_niet_alleen_in_het_recept(tandoor):
    # Model.objects.filter(pk__in=...) zonder het recept erbij: een id van een
    # ander recept werkt dát object bij en hangt het ook hier aan.
    eerste = maak(tandoor, "Eerste")
    tweede = maak(tandoor, "Tweede")
    vreemde_stap = tweede["steps"][0]["id"]
    antwoord = patch(tandoor, eerste["id"], {"steps": [
        {"id": eerste["steps"][0]["id"]}, {"id": vreemde_stap, "instruction": "gekaapt"}]})
    assert antwoord.status_code == 200
    ander = requests.get(f"{tandoor.url}/api/recipe/{tweede['id']}/", headers=KOP).json()
    assert ander["steps"][0]["instruction"] == "gekaapt"


def test_een_meegestuurde_food_wordt_ook_zelf_bijgewerkt(tandoor):
    # Een directe relatie: update_or_create_direct_relations schrijft de food
    # bij. Die food hoort bij elk recept dat hem gebruikt.
    recept = maak(tandoor)
    ui = recept["steps"][0]["ingredients"][0]
    patch(tandoor, recept["id"], {"steps": [{"id": recept["steps"][0]["id"], "ingredients": [
        {"id": ui["id"], "food": {"id": ui["food"]["id"], "name": "sjalot"}}]}]})
    assert tandoor.state()["tellingen"]["food_schrijfacties"] == 1


def test_de_volgorde_is_order_en_dan_id(tandoor):
    recept = maak(tandoor)
    eerste, tweede = recept["steps"]
    antwoord = patch(tandoor, recept["id"], {"steps": [
        {"id": eerste["id"], "order": 5}, {"id": tweede["id"], "order": 1}]})
    assert [s["id"] for s in antwoord.json()["steps"]] == [tweede["id"], eerste["id"]]


def test_ai_en_import_delen_een_teller():
    # AiEndpointThrottle en RecipeImportThrottle hebben dezelfde cachesleutel.
    server = mock_server(ai_limiet=1)
    try:
        requests.post(f"{server.url}/api/recipe-from-source/", headers=KOP,
                      json={"url": "https://x.nl/r", "data": "<html></html>"})
        antwoord = requests.post(f"{server.url}/api/ai-step-sort/?provider=1",
                                 headers=KOP, json={"name": "x", "steps": []})
        assert antwoord.status_code == 429
    finally:
        server.stop()
