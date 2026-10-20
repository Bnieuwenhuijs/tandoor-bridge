"""
Tests voor de sorteerronde, stap voor stap tegen de nagebouwde Tandoor.
"""

import time

import pytest
import requests

from conftest import mock_server
from sorteerronde import (MISLUKT, OVERGESLAGEN, PROEFDRAAI, UITVOEREN, GESORTEERD,
                          SorteerRonde, SorteerStore, pauze_uit_limiet)
from sorteren import Sorteerder
from tandoor import TandoorClient

KOP = {"Authorization": "Bearer tda_test"}


def _ingr(naam, order):
    return {"food": {"name": naam}, "unit": None, "amount": 1, "order": order}


SORTEERBAAR = [
    {"instruction": "Snipper de ui.", "ingredients": [_ingr("ui", 0), _ingr("wortel", 1)]},
    {"instruction": "Kook de wortel.", "ingredients": []},
]
AL_VERDEELD = [
    {"instruction": "Snipper de ui.", "ingredients": [_ingr("ui", 0)]},
    {"instruction": "Kook de wortel.", "ingredients": [_ingr("wortel", 0)]},
]
ZONDER_INGREDIENTEN = [{"instruction": "Even niks.", "ingredients": []}]


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


@pytest.fixture
def store(tmp_path):
    return SorteerStore(tmp_path / "bridge.sqlite3")


@pytest.fixture
def ronde(store, client):
    return SorteerRonde(store, client, Sorteerder(client), ai_pauze=0, schrijf_pauze=0,
                        onbereikbaar_pauze=0)


def maak(tandoor, stappen, naam="Recept") -> int:
    antwoord = requests.post(f"{tandoor.url}/api/recipe/", headers=KOP,
                             json={"name": naam, "steps": stappen})
    assert antwoord.status_code == 201, antwoord.text
    return antwoord.json()["id"]


def draai(ronde, maximaal=200):
    """Stap tot de ronde uit staat; geeft het aantal stappen terug."""
    for n in range(maximaal):
        if not ronde.store.ronde()["actief"]:
            return n
        ronde.stap()
    raise AssertionError("de ronde kwam niet tot een eind")


def recepten(client, ids):
    return {rid: client.get_recipe(rid)["steps"] for rid in ids}


def test_de_proefdraai_schrijft_niets(ronde, client, tandoor):
    ids = [maak(tandoor, SORTEERBAAR, "Soep"), maak(tandoor, AL_VERDEELD, "Al goed"),
           maak(tandoor, ZONDER_INGREDIENTEN, "Leeg"), maak(tandoor, [], "Geen stappen")]
    voor = recepten(client, ids)

    assert ronde.begin(PROEFDRAAI) is None
    draai(ronde)

    assert recepten(client, ids) == voor
    t = ronde.store.tellingen()
    assert (t["voorstel"], t[OVERGESLAGEN], t[MISLUKT]) == (1, 3, 0)
    # Alleen het sorteerbare recept kostte een AI-aanroep.
    assert tandoor.state()["ai_calls"] == 1
    redenen = {i["naam"]: i["reden"] for i in ronde.store.items(status=OVERGESLAGEN)}
    assert "al over 2 stappen verdeeld" in redenen["Al goed"]
    assert redenen["Leeg"] == "geen ingrediënten" and redenen["Geen stappen"] == "geen stappen"
    [voorstel] = ronde.store.items(status="voorstel")
    assert voorstel["samenvatting"]["stappen_voor"] == 2
    assert {"ingredient": "1 wortel", "stap": 2} in voorstel["samenvatting"]["toewijzing"]
    assert "proefdraai klaar" in ronde.store.ronde()["melding"]


def test_uitvoeren_schrijft_precies_het_voorstel(ronde, client, tandoor):
    rid = maak(tandoor, SORTEERBAAR)
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    aanroepen = tandoor.state()["ai_calls"]

    assert ronde.begin(UITVOEREN) is None
    draai(ronde)

    stappen = client.get_recipe(rid)["steps"]
    assert [[i["food"]["name"] for i in s["ingredients"]] for s in stappen] == [["ui"], ["wortel"]]
    # Geen nieuwe AI-vraag bij het uitvoeren: het voorstel is wat je zag.
    assert tandoor.state()["ai_calls"] == aanroepen
    item = ronde.store.item(rid)
    assert item["status"] == GESORTEERD and "2 → 2 stappen" in item["reden"]
    tellingen = tandoor.state()["tellingen"]
    assert tellingen["ingredienten_dubbel_gekoppeld"] == 0
    assert tellingen["food_schrijfacties"] == 0


def test_uitvoeren_kan_niet_zonder_proefdraai(ronde, tandoor):
    maak(tandoor, SORTEERBAAR)
    assert "doe eerst een proefdraai" in ronde.begin(UITVOEREN)


def test_twee_rondes_tegelijk_kan_niet(ronde, tandoor):
    maak(tandoor, SORTEERBAAR)
    ronde.begin(PROEFDRAAI)
    assert "er loopt al een proefdraai" in ronde.begin(PROEFDRAAI)


def test_na_een_herstart_gaat_de_ronde_verder(store, client, tandoor):
    for n in range(3):
        maak(tandoor, SORTEERBAAR, f"Soep {n}")
    eerste = SorteerRonde(store, client, Sorteerder(client), ai_pauze=0)
    eerste.begin(PROEFDRAAI)
    eerste.stap()                       # recepten ophalen
    eerste.stap()                       # het eerste recept
    assert tandoor.state()["ai_calls"] == 1

    # De container herstart: een nieuwe ronde op dezelfde database.
    tweede = SorteerRonde(store, client, Sorteerder(client), ai_pauze=0)
    draai(tweede)
    assert tandoor.state()["ai_calls"] == 3          # niets dubbel gevraagd
    assert store.tellingen()["voorstel"] == 3


def test_na_stoppen_gaat_proefdraai_verder_zonder_opnieuw_te_betalen(ronde, tandoor):
    for n in range(3):
        maak(tandoor, SORTEERBAAR, f"Soep {n}")
    ronde.begin(PROEFDRAAI)
    ronde.stap()
    ronde.stap()
    ronde.stop()
    assert not ronde.store.ronde()["actief"]
    assert "gestopt" in ronde.store.ronde()["melding"]
    assert ronde.stap() is None                     # gestopt is gestopt

    assert ronde.begin(PROEFDRAAI) is None
    draai(ronde)
    assert tandoor.state()["ai_calls"] == 3
    assert ronde.store.tellingen()["voorstel"] == 3


def test_een_lege_portemonnee_stopt_de_ronde_netjes(ronde, tandoor):
    for n in range(3):
        maak(tandoor, SORTEERBAAR, f"Soep {n}")
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_tegoed": 1})
    ronde.begin(PROEFDRAAI)
    draai(ronde)

    melding = ronde.store.ronde()["melding"]
    assert "gestopt" in melding and "tegoed" in melding
    t = ronde.store.tellingen()
    # Eén voorstel; de andere twee wachten, en staan niet op mislukt.
    assert (t["voorstel"], t["te_bekijken"], t[MISLUKT]) == (1, 2, 0)


def test_een_tegoedmelding_in_een_andere_taal_stopt_de_ronde_ook(ronde, tandoor, monkeypatch):
    """
    De tekstherkenning mist hem (vertaald), en de controle vooraf zag nog
    tegoed. Dan komt er een gewone AI-fout terug - en de ronde vraagt het de
    space na in plaats van het recept op mislukt te zetten.
    """
    import tandoor as tandoor_module

    rid = maak(tandoor, SORTEERBAAR)
    monkeypatch.setattr(tandoor_module, "_is_tegoed_op", lambda payload: False)
    echt = ronde._tegoed_op
    keer = []

    def tegoed_op():
        keer.append(1)
        return None if len(keer) == 1 else echt()          # de eerste keer: nog tegoed

    monkeypatch.setattr(ronde, "_tegoed_op", tegoed_op)

    ronde.begin(PROEFDRAAI)
    ronde.stap()                                           # ophalen
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_tegoed": 0})
    ronde.stap()

    assert not ronde.store.ronde()["actief"]
    assert "tegoed" in ronde.store.ronde()["melding"]
    assert ronde.store.item(rid)["status"] == "wachtend"   # niet op mislukt
    assert len(keer) == 2                                  # vooraf, en ná de AI-fout


def test_bij_de_limiet_wacht_hij_en_probeert_hetzelfde_recept_opnieuw(ronde, tandoor):
    rid = maak(tandoor, SORTEERBAAR)
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_limiet": 1})
    requests.post(f"{tandoor.url}/api/recipe-from-source/", headers=KOP,
                  json={"url": "https://x.nl/r", "data": "<html></html>"})
    ronde.begin(PROEFDRAAI)
    ronde.stap()                                   # ophalen
    assert ronde.stap() == 5.0                     # de Retry-After van de mock
    assert ronde.store.item(rid)["plan"] is None
    assert "AI-limiet" in ronde.store.ronde()["melding"]

    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_limiet": 0})
    draai(ronde)
    assert ronde.store.tellingen()["voorstel"] == 1


def test_een_ai_fout_is_mislukt_en_wordt_bij_de_volgende_proefdraai_herhaald(ronde, tandoor):
    rid = maak(tandoor, SORTEERBAAR)
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_gedrag": "fout"})
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    assert ronde.store.item(rid)["status"] == MISLUKT
    assert "could not process" in ronde.store.item(rid)["reden"]

    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_gedrag": "volgens-tekst"})
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    assert ronde.store.item(rid)["plan"] is not None


def test_een_recept_dat_tussendoor_veranderde_wordt_opnieuw_bekeken(ronde, tandoor, client):
    rid = maak(tandoor, SORTEERBAAR)
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    requests.patch(f"{tandoor.url}/api/recipe/{rid}/", headers=KOP, json={"name": "Anders"})
    voor = client.get_recipe(rid)["steps"]

    ronde.begin(UITVOEREN)
    draai(ronde)
    assert ronde.store.item(rid)["status"] == MISLUKT
    assert client.get_recipe(rid)["steps"] == voor

    ronde.begin(PROEFDRAAI)
    draai(ronde)
    ronde.begin(UITVOEREN)
    draai(ronde)
    assert ronde.store.item(rid)["status"] == GESORTEERD


def test_gesorteerd_blijft_staan_en_wordt_niet_opnieuw_bekeken(ronde, tandoor):
    maak(tandoor, SORTEERBAAR)
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    ronde.begin(UITVOEREN)
    draai(ronde)
    aanroepen = tandoor.state()["ai_calls"]
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    assert tandoor.state()["ai_calls"] == aanroepen
    assert ronde.store.tellingen()[GESORTEERD] == 1


def test_zonder_bruikbare_provider_stopt_de_proefdraai(ronde, tandoor):
    maak(tandoor, SORTEERBAAR)
    requests.post(f"{tandoor.url}/__mock/ai", json={"ai_providers": [
        {"id": 1, "name": "A"}, {"id": 2, "name": "B"}]})
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    assert "TANDOOR_AI_PROVIDER_ID" in ronde.store.ronde()["melding"]
    assert tandoor.state()["ai_calls"] == 0


def test_de_thread_draait_de_ronde_af(ronde, tandoor):
    maak(tandoor, SORTEERBAAR)
    ronde.start()
    try:
        ronde.begin(PROEFDRAAI)
        einde = time.time() + 10
        while ronde.store.ronde()["actief"] and time.time() < einde:
            time.sleep(0.05)
        assert ronde.store.tellingen()["voorstel"] == 1
    finally:
        ronde.stop_thread()


@pytest.mark.parametrize("limiet, pauze", [
    ("60/hour", 60.0), ("10/minute", 6.0), ("120/h", 30.0), ("1000/day", 86.4),
    ("onzin", 60.0), ("0/hour", 60.0),
])
def test_pauze_uit_de_limiet(limiet, pauze):
    assert pauze_uit_limiet(limiet) == pytest.approx(pauze)


def test_een_recept_waar_de_ai_niets_aan_verandert_is_geen_voorstel(ronde, tandoor):
    # Eén stap, en de AI splitst niet: dan blijft alles zoals het was, en
    # uitvoeren zou alleen hetzelfde nog eens wegschrijven.
    rid = maak(tandoor, [{"instruction": "Alles in de pan.",
                          "ingredients": [_ingr("ui", 0), _ingr("wortel", 1)]}])
    ronde.begin(PROEFDRAAI)
    draai(ronde)
    item = ronde.store.item(rid)
    assert item["status"] == OVERGESLAGEN
    assert item["reden"] == "de AI liet het recept zoals het was"
    assert "doe eerst een proefdraai" in ronde.begin(UITVOEREN)


def test_te_bekijken_is_een_eigen_filter(ronde, tandoor):
    for n in range(2):
        maak(tandoor, SORTEERBAAR, f"Soep {n}")
    ronde.begin(PROEFDRAAI)
    ronde.stap()
    ronde.stap()
    assert [i["naam"] for i in ronde.store.items(status="te_bekijken")] == ["Soep 1"]
    assert [i["naam"] for i in ronde.store.items(status="voorstel")] == ["Soep 0"]
