"""
Tests voor het sorteren, tegen de nagebouwde Tandoor.

Na elke schrijfactie kijken we in de hele nagebouwde database, niet alleen
in het antwoord: geen ingrediënt dubbel gekoppeld, geen ingrediënt kwijt,
geen food beschreven, en geen ander recept geraakt.
"""

import pytest
import requests

import sorteren
from conftest import mock_server
from tandoor import TandoorClient

KOP = {"Authorization": "Bearer tda_test"}

STAPPEN = [{
    "instruction": "Snipper de ui. Kook de wortel in water. Pureer met de room.",
    "ingredients": [
        {"food": {"name": "ui"}, "unit": None, "amount": 1, "order": 0,
         "original_text": "1 ui"},
        {"food": {"name": "wortel"}, "unit": {"name": "g"}, "amount": 500, "order": 1,
         "original_text": "500 g wortel"},
        {"food": {"name": "room"}, "unit": {"name": "ml"}, "amount": 100, "order": 2,
         "original_text": "100 ml room"},
    ],
}]

TWEE_STAPPEN = [
    {"instruction": "Snipper de ui. Kook de wortel.", "time": 7, "ingredients": [
        {"food": {"name": "ui"}, "unit": None, "amount": 1, "order": 0},
        {"food": {"name": "wortel"}, "unit": {"name": "g"}, "amount": 500, "order": 1},
    ]},
    {"instruction": "Pureer alles.", "time": 3, "ingredients": []},
]


@pytest.fixture
def tandoor():
    server = mock_server()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(tandoor):
    return TandoorClient(tandoor.url, "tda_test", max_retries=1)


def maak(tandoor, stappen=STAPPEN, naam="Wortelsoep") -> int:
    antwoord = requests.post(f"{tandoor.url}/api/recipe/", headers=KOP,
                             json={"name": naam, "steps": stappen})
    assert antwoord.status_code == 201, antwoord.text
    return antwoord.json()["id"]


def ai(tandoor, gedrag, **meer):
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_gedrag": gedrag, **meer})


def heel(tandoor, voor: dict):
    """Na het schrijven: niets dubbel, niets kwijt, geen food beschreven."""
    na = tandoor.state()["tellingen"]
    assert na["ingredienten_dubbel_gekoppeld"] == 0
    assert na["ingredienten"] == voor["ingredienten"], "er is een ingrediënt bijgemaakt"
    assert na["food_schrijfacties"] == 0, "er is een food beschreven"
    assert na["stappen"] >= voor["stappen"], "er is een stap verwijderd"


def indeling(client, recept_id):
    recept = client.get_recipe(recept_id)
    return [[i["food"]["name"] for i in s["ingredients"]] for s in recept["steps"]]


def sorteer(client, tandoor, recept_id, gedrag="volgens-tekst"):
    ai(tandoor, gedrag)
    origineel = client.get_recipe(recept_id)
    plan = sorteren.maak_plan(origineel, client.ai_step_sort(origineel, 1))
    voor = tandoor.state()["tellingen"]
    melding = sorteren.voer_uit(client, recept_id, plan)
    heel(tandoor, voor)
    return plan, melding


# -- provider en tegoed ----------------------------------------------------

def test_provider_uit_de_omgeving(client):
    p = sorteren.kies_provider(client, "1")
    assert (p.id, p.uitleg) == (1, "ingesteld via TANDOOR_AI_PROVIDER_ID")


def test_een_provider_die_niet_bestaat_noemt_wat_er_wel_is(client):
    p = sorteren.kies_provider(client, "7")
    assert p.id is None
    assert "TANDOOR_AI_PROVIDER_ID=7 bestaat niet" in p.uitleg and "1 (Mock-AI)" in p.uitleg


@pytest.mark.parametrize("providers, standaard, verwacht, uitleg", [
    ([{"id": 1, "name": "A"}, {"id": 2, "name": "B"}], 2, 2, "standaardprovider"),
    ([{"id": 3, "name": "C"}], None, 3, "de enige"),
    ([], None, None, "geen AI-provider"),
    # Meer dan één en geen standaard: niet gokken, want elke aanroep kost tegoed.
    ([{"id": 1, "name": "A"}, {"id": 2, "name": "B"}], None, None, "TANDOOR_AI_PROVIDER_ID"),
])
def test_provider_kiezen_zonder_instelling(providers, standaard, verwacht, uitleg):
    server = mock_server(ai_providers=providers, ai_default_provider=standaard)
    try:
        p = sorteren.kies_provider(TandoorClient(server.url, "tda_test"), "")
    finally:
        server.stop()
    assert p.id == verwacht and uitleg in p.uitleg


@pytest.mark.parametrize("space, op", [
    ({"ai_enabled": True, "ai_credits_balance": 5}, False),
    ({"ai_enabled": True, "ai_credits_monthly": 100, "ai_monthly_credits_used": 20}, False),
    ({"ai_enabled": True, "ai_credits_balance": 0, "ai_credits_monthly": 10,
      "ai_monthly_credits_used": 10}, True),
    ({"ai_enabled": False, "ai_credits_balance": 50}, True),
])
def test_tegoed(space, op):
    assert (sorteren.tegoed_op(space) is not None) is op


# -- wat er overgeslagen wordt ---------------------------------------------

@pytest.mark.parametrize("recept, reden", [
    ({"steps": []}, "geen stappen"),
    ({"steps": [{"ingredients": []}]}, "geen ingrediënten"),
    ({"steps": [{"ingredients": [{"id": 1}]}, {"ingredients": [{"id": 2}]}]},
     "al over 2 stappen verdeeld"),
    ({"steps": [{"ingredients": [{"id": 1}, {"id": 2}]}, {"ingredients": []}]}, None),
])
def test_overslaan(recept, reden):
    gekregen = sorteren.overslaan(recept)
    assert (gekregen is None) if reden is None else (reden in gekregen)


# -- plan en uitvoeren -----------------------------------------------------

def test_ingredienten_gaan_naar_de_stap_die_ze_noemt(client, tandoor):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    sorteer(client, tandoor, recept_id)
    assert indeling(client, recept_id) == [["ui", "wortel"], []]


def test_splitsen_maakt_nieuwe_stappen_zonder_iets_te_verliezen(client, tandoor):
    recept_id = maak(tandoor)
    plan, melding = sorteer(client, tandoor, recept_id, "splitsen")
    assert indeling(client, recept_id) == [["ui"], ["wortel"], ["room"]]
    assert "1 → 3 stappen" in melding
    instructies = [s["instruction"] for s in client.get_recipe(recept_id)["steps"]]
    assert instructies == ["Snipper de ui.", "Kook de wortel in water.", "Pureer met de room."]


def test_een_gekopieerd_stap_id_wordt_geen_gedeelde_stap(client, tandoor):
    # Het model zet het id van de oorspronkelijke stap op alle stukken. Drie
    # keer hetzelfde id zou in Tandoor één stap drie keer bijwerken.
    recept_id = maak(tandoor)
    sorteer(client, tandoor, recept_id, "splitsen-zelfde-id")
    stappen = client.get_recipe(recept_id)["steps"]
    assert len({s["id"] for s in stappen}) == 3
    assert indeling(client, recept_id) == [["ui"], ["wortel"], ["room"]]


def test_een_dubbel_ingredient_komt_maar_in_een_stap(client, tandoor):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    plan, _ = sorteer(client, tandoor, recept_id, "dubbel")
    alle = [naam for stap in indeling(client, recept_id) for naam in stap]
    assert sorted(alle) == ["ui", "wortel"]
    assert any("meer dan één stap" in n for n in plan["notities"])


def test_een_weggelaten_ingredient_blijft_in_stap_1(client, tandoor):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    plan, _ = sorteer(client, tandoor, recept_id, "weglaten")
    assert "wortel" in indeling(client, recept_id)[0]
    assert any("liet de AI weg" in n for n in plan["notities"])


def test_zonder_ids_worden_de_bestaande_stappen_hergebruikt(client, tandoor):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    voor = [s["id"] for s in client.get_recipe(recept_id)["steps"]]
    sorteer(client, tandoor, recept_id, "zonder-ids")
    na = client.get_recipe(recept_id)["steps"]
    assert [s["id"] for s in na] == voor
    # De tijd van de stap hoort bij de stap en is er nog.
    assert [s["time"] for s in na] == [7, 3]
    assert indeling(client, recept_id) == [["ui", "wortel"], []]


def test_een_id_van_een_ander_recept_raakt_dat_recept_niet(client, tandoor):
    ander = maak(tandoor, naam="Ander recept")
    voor_ander = client.get_recipe(ander)
    recept_id = maak(tandoor, TWEE_STAPPEN)
    plan, _ = sorteer(client, tandoor, recept_id, "vreemd-id")
    assert client.get_recipe(ander)["steps"] == voor_ander["steps"]
    assert sorted(n for s in indeling(client, recept_id) for n in s) == ["ui", "wortel"]
    assert any("niet bij dit recept hoort" in n for n in plan["notities"])


def test_een_veranderde_hoeveelheid_wordt_niet_geschreven(client, tandoor):
    # Bij een bestaand recept alleen stappen en indeling, verder niets.
    recept_id = maak(tandoor, TWEE_STAPPEN)
    sorteer(client, tandoor, recept_id, "ander-bedrag")
    ingredienten = [i for s in client.get_recipe(recept_id)["steps"] for i in s["ingredients"]]
    assert {(i["food"]["name"], i["amount"]) for i in ingredienten} == {("ui", 1), ("wortel", 500)}


def test_een_ingepakt_antwoord_wordt_uitgepakt(client, tandoor):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    sorteer(client, tandoor, recept_id, "inpakken")
    assert indeling(client, recept_id) == [["ui", "wortel"], []]


def test_een_recept_dat_sinds_de_proefdraai_veranderde_blijft_ongemoeid(client, tandoor):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    origineel = client.get_recipe(recept_id)
    plan = sorteren.maak_plan(origineel, client.ai_step_sort(origineel, 1))
    requests.patch(f"{tandoor.url}/api/recipe/{recept_id}/", headers=KOP,
                   json={"name": "Door mij aangepast"})
    with pytest.raises(sorteren.UitvoerFout, match="sinds de proefdraai veranderd"):
        sorteren.voer_uit(client, recept_id, plan)
    assert client.get_recipe(recept_id)["steps"] == origineel["steps"]


def test_mislukt_het_indelen_dan_wordt_alles_teruggezet(client, tandoor, monkeypatch):
    recept_id = maak(tandoor)
    ai(tandoor, "splitsen")
    origineel = client.get_recipe(recept_id)
    plan = sorteren.maak_plan(origineel, client.ai_step_sort(origineel, 1))

    echt = client.patch_steps
    aanroepen = []

    def tweede_mislukt(recept_id, steps, *, retry):
        aanroepen.append(steps)
        if len(aanroepen) == 2:                     # stap B
            from tandoor import TandoorUnreachable
            raise TandoorUnreachable("weg")
        return echt(recept_id, steps, retry=retry)

    monkeypatch.setattr(client, "patch_steps", tweede_mislukt)
    with pytest.raises(sorteren.UitvoerFout, match="indelen mislukte"):
        sorteren.voer_uit(client, recept_id, plan)
    # Stap A had al twee lege stappen aangemaakt; die zijn weer losgekoppeld.
    na = client.get_recipe(recept_id)
    assert [s["id"] for s in na["steps"]] == [s["id"] for s in origineel["steps"]]
    assert indeling(client, recept_id) == [["ui", "wortel", "room"]]
    assert tandoor.state()["tellingen"]["ingredienten_dubbel_gekoppeld"] == 0


def test_klopt_het_resultaat_niet_dan_wordt_alles_teruggezet(client, tandoor, monkeypatch):
    recept_id = maak(tandoor, TWEE_STAPPEN)
    ai(tandoor, "volgens-tekst")
    origineel = client.get_recipe(recept_id)
    plan = sorteren.maak_plan(origineel, client.ai_step_sort(origineel, 1))
    plan["stappen"][1]["ingredienten"] = []         # zo bedoeld...
    echt = client.patch_steps

    def verkeerd(recept_id, steps, *, retry):
        antwoord = echt(recept_id, steps, retry=retry)
        antwoord["steps"][0]["ingredients"] = []    # ...maar zo teruggekregen
        return antwoord

    monkeypatch.setattr(client, "patch_steps", verkeerd)
    with pytest.raises(sorteren.UitvoerFout, match="klopte het niet"):
        sorteren.voer_uit(client, recept_id, plan)
    monkeypatch.setattr(client, "patch_steps", echt)
    assert indeling(client, recept_id) == [["ui", "wortel"], []]


def test_samenvatting_voor_de_proefdraai(client, tandoor):
    recept_id = maak(tandoor)
    ai(tandoor, "splitsen")
    origineel = client.get_recipe(recept_id)
    plan = sorteren.maak_plan(origineel, client.ai_step_sort(origineel, 1))
    s = sorteren.samenvatting(origineel, plan)
    assert (s["stappen_voor"], s["stappen_na"]) == (1, 3)
    assert {"ingredient": "500 g wortel", "stap": 2} in s["toewijzing"]


# -- nieuwe recepten -------------------------------------------------------

@pytest.mark.parametrize("instelling, reden", [
    ({"ai_gedrag": "fout"}, "could not process"),
    ({"ai_gedrag": "kapot"}, "interne fout"),
    ({"ai_tegoed": 0}, "tegoed van de space is op"),
    ({"ai_limiet": 1}, "AI-limiet bereikt"),
])
def test_een_mislukte_ai_aanroep_geeft_de_reden(client, tandoor, instelling, reden):
    requests.post(f"{tandoor.url}/__mock/ai", json=instelling)
    if "ai_limiet" in instelling:
        requests.post(f"{tandoor.url}/api/recipe-from-source/", headers=KOP,
                      json={"url": "https://x.nl/r", "data": "<html></html>"})
    gesorteerd, gekregen, _ = sorteren.sorteer_nieuw(client, 1, {"name": "x", "steps": STAPPEN})
    assert gesorteerd is None and reden in gekregen


def test_bij_een_nieuw_recept_bepaalt_de_ai_de_stappen_en_tandoor_de_ingredienten(
        client, tandoor):
    # De AI krijgt alleen de slanke vorm, en geeft dus ook alleen die terug.
    # De stappen en de indeling komen van de AI; wat er in een ingrediënt
    # staat - de oorspronkelijke regel, de hoeveelheid - uit Tandoors eigen
    # ontleding. Ook als de AI een hoeveelheid verandert.
    ai(tandoor, "splitsen")
    recept = {"name": "Soep", "keywords": [{"id": None, "name": "www.ah.nl"}],
              "steps": STAPPEN}
    gesorteerd, reden, notities = sorteren.sorteer_nieuw(client, 1, recept)
    assert reden is None and notities == []
    assert [s["instruction"] for s in gesorteerd["steps"]] == [
        "Snipper de ui.", "Kook de wortel in water.", "Pureer met de room."]
    assert [[i["original_text"] for i in s["ingredients"]] for s in gesorteerd["steps"]] == [
        ["1 ui"], ["500 g wortel"], ["100 ml room"]]
    assert gesorteerd["keywords"] == recept["keywords"]
    # Geen tijdelijke ids in wat naar Tandoor gaat.
    assert all("id" not in s for s in gesorteerd["steps"])
    assert all("id" not in i for s in gesorteerd["steps"] for i in s["ingredients"])

    ai(tandoor, "ander-bedrag")
    gesorteerd, _, _ = sorteren.sorteer_nieuw(client, 1, recept)
    assert {i["amount"] for s in gesorteerd["steps"] for i in s["ingredients"]} == {1, 500, 100}


# -- de slanke vorm --------------------------------------------------------

def test_de_ai_krijgt_alleen_wat_hij_nodig_heeft(client, tandoor):
    """
    Een echt Tandoor-recept van 15 ingrediënten is zo 50 KB: per ingrediënt de
    hele food, omrekeningen, waar het nog meer in staat. Het model moet het
    hele recept terugtypen, en deed daar drie minuten over - terwijl de nginx
    in Tandoor na 60 seconden een 504 geeft.
    """
    recept_id = maak(tandoor)
    volledig = client.get_recipe(recept_id)
    slank = sorteren.voor_ai(volledig)
    assert set(slank) == {"name", "steps"}
    assert set(slank["steps"][0]) == {"id", "instruction", "ingredients"}
    assert set(slank["steps"][0]["ingredients"][0]) == {"id", "food", "unit", "amount", "note"}
    assert slank["steps"][0]["ingredients"][0]["food"] == {"name": "ui"}
    import json
    assert len(json.dumps(slank)) < len(json.dumps(volledig)) / 2


def test_een_groot_recept_haalt_de_60_seconden_met_de_slanke_vorm(client, tandoor):
    # De mock geeft een 504 als wat er binnenkomt te groot is - zoals de nginx
    # van Tandoor doet als het model te lang moet typen.
    ingredienten = [{"food": {"name": f"kruid {n}"}, "unit": {"name": "g"}, "amount": n,
                     "order": n, "original_text": f"{n} g kruid {n}"} for n in range(30)]
    recept_id = maak(tandoor, [{"instruction": "Alles. Door elkaar.",
                                "ingredients": ingredienten}])
    volledig = client.get_recipe(recept_id)
    import json
    grens = len(json.dumps(sorteren.voor_ai(volledig))) + 50
    assert len(json.dumps(volledig)) > grens, "de test meet niets als volledig ook past"
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_max_invoer": grens})

    with pytest.raises(sorteren.TandoorAiTimeout):
        client.ai_step_sort(volledig, 1)                 # zo ging het mis
    antwoord = client.ai_step_sort(sorteren.voor_ai(volledig), 1)
    plan = sorteren.maak_plan(volledig, antwoord)
    assert sum(len(s["ingredienten"]) for s in plan["stappen"]) == 30


def test_een_504_van_de_ai_is_een_time_out_geen_interne_fout(client, tandoor):
    ai(tandoor, "traag")
    gesorteerd, reden, _ = sorteren.sorteer_nieuw(client, 1, {"name": "x", "steps": STAPPEN})
    assert gesorteerd is None
    assert "60 seconden" in reden and "interne fout" not in reden


def test_na_een_time_out_wacht_het_importeren_met_de_ai(client, tandoor):
    klok = [1000.0]
    sorteerder = sorteren.Sorteerder(client, klok=lambda: klok[0])
    ai(tandoor, "traag")
    _, reden, _ = sorteerder.sorteer_nieuw({"name": "x", "steps": STAPPEN})
    assert "60 seconden" in reden
    aanroepen = tandoor.state()["ai_calls"]

    ai(tandoor, "volgens-tekst")
    klok[0] += 60
    _, reden, _ = sorteerder.sorteer_nieuw({"name": "x", "steps": STAPPEN})
    assert "even overgeslagen" in reden
    assert tandoor.state()["ai_calls"] == aanroepen          # geen nieuwe vraag

    klok[0] += sorteren.NA_TIME_OUT
    gesorteerd, reden, _ = sorteerder.sorteer_nieuw({"name": "x", "steps": STAPPEN})
    assert gesorteerd is not None and reden is None
