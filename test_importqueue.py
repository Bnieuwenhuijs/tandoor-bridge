"""
Tests voor de wachtrij: de opslag, en het verwerken van één item per keer.

Het ophalen is hier nagebootst (geen netwerk), maar Tandoor is de echte
nagebouwde API uit mock_tandoor.py, dus de hele keten van HTML naar een
recept met foto loopt door dezelfde code als in productie.
"""

import pytest

from conftest import AH_HTML, AH_URL, JPEG, mock_server
from fetcher import FetchError, FetchResult
from importqueue import (
    BESTAAT_AL,
    KLAAR,
    MISLUKT,
    OPHALEN,
    WACHTEND,
    ImportQueue,
    Store,
)
from tandoor import TandoorClient

TWEEDE_URL = "https://www.leukerecepten.nl/recepten/lasagnesoep/"


def maak_fetch(html=AH_HTML, *, final_url=None, fout=None):
    def fetch(url, *, impersonate=True, timeout=20):
        if fout:
            raise FetchError(fout, status=403)
        return FetchResult(url, final_url or url, 200, html, "requests")
    return fetch


def maak_fotofetch(*, fout=None):
    def fetch_photo(url, *, referer=None, timeout=20):
        if fout:
            raise FetchError(fout)
        return JPEG, "image/jpeg"
    return fetch_photo


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bridge.sqlite3")


@pytest.fixture
def queue(store, client):
    return ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                       fetch_photo=maak_fotofetch())


# ------------------------------------------------------------- de opslag

def test_tekst_toevoegen_haalt_de_links_eruit(store):
    result = store.add_text(
        f"Lieke: deze! {AH_URL} en {TWEEDE_URL} ook 😋")
    assert result.found == 2
    assert len(result.added) == 2
    assert [i["status"] for i in store.items()] == [WACHTEND, WACHTEND]


def test_dezelfde_link_tweemaal_in_een_bericht(store):
    result = store.add_text(f"{AH_URL} nogmaals: https://www.ah.nl/r/921580")
    assert result.found == 1          # extract_urls ontdubbelt al
    assert len(result.added) == 1


def test_wat_al_in_de_rij_staat_wordt_overgeslagen(store):
    store.add([AH_URL])
    result = store.add([AH_URL])
    assert result.added == []
    assert result.skipped[0]["reason"] == "staat al in de wachtrij"


def test_wat_al_geimporteerd_is_wordt_overgeslagen(store):
    [item_id] = store.add([AH_URL]).added
    store.update(item_id, status=KLAAR, title="Kip korma", recipe_id=7)

    result = store.add(["https://www.ah.nl/r/921580"])   # andere vorm, zelfde recept
    assert result.added == []
    assert result.skipped[0]["reason"] == "bestaat al"
    assert result.skipped[0]["recipe_id"] == 7


def test_voorbeeldlijst_laat_zien_wat_er_gaat_gebeuren(store):
    [klaar_id] = store.add([AH_URL]).added
    store.update(klaar_id, status=KLAAR, title="Kip korma", recipe_id=7)
    [mislukt_id] = store.add([TWEEDE_URL]).added
    store.update(mislukt_id, status=MISLUKT, error="geblokkeerd (status 403)")
    store.add(["https://www.voedingscentrum.nl/recept/pasta/"])

    oordeel = {r["url"]: r for r in store.classify([
        AH_URL, TWEEDE_URL, "https://www.voedingscentrum.nl/recept/pasta/",
        "https://example.com/nieuw",
    ])}
    assert oordeel[AH_URL]["state"] == "al_geimporteerd"
    assert oordeel[AH_URL]["recipe_id"] == 7
    assert oordeel[TWEEDE_URL]["state"] == "eerder_mislukt"
    assert "403" in oordeel[TWEEDE_URL]["error"]
    assert oordeel["https://www.voedingscentrum.nl/recept/pasta/"]["state"] == "in_wachtrij"
    assert oordeel["https://example.com/nieuw"]["state"] == "nieuw"


def test_na_een_herstart_gaat_onderweg_terug_in_de_rij(store):
    [item_id] = store.add([AH_URL]).added
    store.update(item_id, status=OPHALEN)      # proces viel hier om

    assert store.requeue_stale() == 1
    assert store.item(item_id)["status"] == WACHTEND


def test_opnieuw_proberen(store):
    [item_id] = store.add([AH_URL]).added
    store.update(item_id, status=MISLUKT, error="geblokkeerd")

    assert store.retry(item_id) is True
    hersteld = store.item(item_id)
    assert hersteld["status"] == WACHTEND
    assert hersteld["error"] is None
    # Een recept dat al klaar is, probeer je niet opnieuw.
    store.update(item_id, status=KLAAR)
    assert store.retry(item_id) is False


def test_alle_mislukte_in_een_keer(store):
    ids = store.add([AH_URL, TWEEDE_URL]).added
    for item_id in ids:
        store.update(item_id, status=MISLUKT, error="geblokkeerd")
    assert store.retry_all_failed() == 2
    assert store.counts() == {WACHTEND: 2}


def test_geschiedenis_filtert_op_status(store):
    ids = store.add([AH_URL, TWEEDE_URL]).added
    store.update(ids[0], status=KLAAR)
    store.update(ids[1], status=MISLUKT, error="stuk")
    assert len(store.items(status=MISLUKT)) == 1
    assert store.items(status=MISLUKT)[0]["error"] == "stuk"


# ------------------------------------------------------------- verwerken

def test_niets_te_doen(queue):
    assert queue.process_once() is None


def test_een_recept_helemaal_door_de_keten(queue, store, tandoor):
    [item_id] = store.add([AH_URL]).added

    uitkomst = queue.process_once()
    assert uitkomst["status"] == KLAAR

    item = store.item(item_id)
    assert item["title"] == "Kip korma met spinazie"
    assert item["recipe_id"] == 1
    assert item["error"] is None
    assert item["note"] is None          # foto gelukt, niets te melden

    # En aan de Tandoor-kant staat het recept er echt, met bron en foto.
    state = tandoor.state()
    assert state["recipes"][0]["source_url"] == AH_URL
    assert state["images"]["1"] == "recept.jpg"


def test_een_ah_recept_gaat_via_de_api_naar_tandoor(store, client, tandoor, monkeypatch):
    """
    De route waarvoor de brug bestaat, van link tot recept in Tandoor.

    Hier draait het échte fetch_page(), dus de AH-poging zit erin. De pagina
    van ah.nl wordt niet opgehaald: de nagebootste app-API levert het recept,
    en Tandoor krijgt daar een pagina van te zien.
    """
    import ah_api
    import mock_ah
    from conftest import MockServer
    from fetcher import fetch_page

    import fetcher

    ah = MockServer(mock_ah.create_app())
    monkeypatch.setattr(ah_api, "API_URL", ah.url)
    # De andere pogingen zouden www.ah.nl echt benaderen. Gaat de AH-route
    # hier mis, dan moet de test dat zeggen - niet stilletjes een 403 van
    # Akamai ophalen en de schuld bij Albert Heijn leggen.
    for poging in ("_attempt_requests", "_attempt_curl", "_attempt_renderer"):
        monkeypatch.setattr(fetcher, poging, lambda *a, _p=poging, **k: pytest.fail(
            f"{_p} draaide: de AH-route is niet gelukt"))
    ah_api.reset_token()
    ah_api.reset_schema()
    url = "https://www.ah.nl/allerhande/recept/R-R1194169/traybake-met-kip"
    try:
        rij = ImportQueue(store, client, delay=0, fetch=fetch_page,
                          fetch_photo=maak_fotofetch())
        [item_id] = store.add([url]).added
        uitkomst = rij.process_once()
    finally:
        ah.stop()
        ah_api.reset_token()
        ah_api.reset_schema()

    assert uitkomst["status"] == KLAAR
    item = store.item(item_id)
    assert item["title"] == "Traybake met kip, paprika, zwarte bonen en chipotle"
    assert item["error"] is None

    recept = tandoor.state()["recipes"][0]
    assert recept["source_url"] == url
    # Dit is waar het om gaat: Tandoor heeft de regel ontleed, niet wij. Dus
    # werken Food Alias, Unit Alias en de rest ook op deze recepten.
    eerste = recept["steps"][0]["ingredients"][0]
    assert eerste["original_text"] == "600 g kipfilet"
    assert eerste["amount"] == 600.0
    assert eerste["unit"]["name"] == "g"
    assert eerste["food"]["name"] == "kipfilet"
    # En het domein-trefwoord klopt, want de canonical wees naar ah.nl.
    assert {"id": None, "name": "www.ah.nl"} in recept["keywords"]
    # De oventijd uit de zoekresultaten van de app wordt Tandoors wachttijd.
    assert recept["working_time"] == 20
    assert recept["waiting_time"] == 15


def _pagina_met_tijden(**tijden):
    import json
    schema = {"@context": "https://schema.org", "@type": "Recipe",
              "name": "Stoofpot", "recipeIngredient": ["1 ui"],
              "recipeInstructions": ["Stoven."], **tijden}
    return ("<html><head><script type=\"application/ld+json\">"
            + json.dumps(schema) + "</script></head><body></body></html>")


@pytest.mark.parametrize("tijden, werk, wacht", [
    # Geen cookTime, wel bereiding en totaal: het verschil is de wachttijd.
    ({"prepTime": "PT15M", "totalTime": "PT1H"}, 15, 45),
    # Tandoor vulde de wachttijd zelf al: daar blijven we af.
    ({"prepTime": "PT15M", "cookTime": "PT30M", "totalTime": "PT1H"}, 15, 30),
    # Alleen een totale tijd: niet te zeggen welk deel wachten is.
    ({"totalTime": "PT1H"}, 60, 0),
    # Totaal gelijk aan de bereiding: niets te wachten.
    ({"prepTime": "PT20M", "totalTime": "PT20M"}, 20, 0),
])
def test_wachttijd_uit_totaal_min_bereiding(store, client, tandoor, tijden, werk, wacht):
    rij = ImportQueue(store, client, delay=0, fetch=maak_fetch(_pagina_met_tijden(**tijden)),
                      fetch_photo=maak_fotofetch())
    store.add([TWEEDE_URL])
    assert rij.process_once()["status"] == KLAAR
    recept = tandoor.state()["recipes"][0]
    assert (recept["working_time"], recept["waiting_time"]) == (werk, wacht)


def test_een_fout_stopt_de_rest_niet(store, client):
    """Precies wat de lijst-import van Tandoor zelf verkeerd doet."""
    pogingen = []

    def fetch(url, *, impersonate=True, timeout=20):
        pogingen.append(url)
        if url == AH_URL:
            raise FetchError("geblokkeerd (status 403)", status=403)
        return FetchResult(url, url, 200, AH_HTML, "requests")

    queue = ImportQueue(store, client, delay=0, fetch=fetch,
                        fetch_photo=maak_fotofetch())
    store.add([AH_URL, TWEEDE_URL])

    assert queue.process_once()["status"] == MISLUKT
    assert queue.process_once()["status"] == KLAAR
    assert pogingen == [AH_URL, TWEEDE_URL]

    mislukt, klaar = store.items(status=MISLUKT), store.items(status=KLAAR)
    assert mislukt[0]["error"] == "geblokkeerd (status 403)"
    assert klaar[0]["recipe_id"] == 1


def test_pagina_zonder_schema_wordt_niet_naar_tandoor_gestuurd(store, client, tandoor):
    """
    Een pagina zonder recept-schema geeft bij Tandoor een kale 500, want zijn
    data-tak vangt NoSchemaFoundInWildMode niet af. Die aanroep doen we dus
    niet - hij kost een plaats in de limiet en zegt niets.
    """
    queue = ImportQueue(store, client, delay=0,
                        fetch=maak_fetch("<html><head><title>Even geduld…</title>"
                                         "</head><body>laden</body></html>"),
                        fetch_photo=maak_fotofetch())
    [item_id] = store.add([AH_URL]).added

    assert queue.process_once()["status"] == MISLUKT
    fout = store.item(item_id)["error"]
    assert "geen recept-schema" in fout
    assert "Even geduld" in fout                        # je ziet wát er kwam
    assert "Tandoor vond geen recept" not in fout        # niet aan Tandoor toeschrijven
    assert tandoor.state()["import_calls"] == 0


def test_pagina_met_ander_schema_krijgt_tandoors_eigen_melding(store, client):
    queue = ImportQueue(store, client, delay=0, fetch=maak_fetch('<html><head><title>Kattenfoto</title><script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","name":"Kat"}</script></head><body>miauw</body></html>'),
                        fetch_photo=maak_fotofetch())
    [item_id] = store.add([AH_URL]).added

    assert queue.process_once()["status"] == MISLUKT
    assert "No usable data could be found." in store.item(item_id)["error"]


def test_dubbele_uit_onze_eigen_database(queue, store):
    [eerste] = store.add([AH_URL]).added
    queue.process_once()
    assert store.item(eerste)["status"] == KLAAR

    # Dezelfde link in de deelvorm wordt al bij het toevoegen gezien, ook
    # via de API waar geen bericht aan te pas komt.
    assert store.add(["https://www.ah.nl/r/921580"]).added == []

    # En komt hij er tóch in - twee verzoeken die tegelijk aankomen zien
    # elkaar niet - dan vangt de wachtrij het alsnog. Daarom zetten we hier
    # met de hand een tweede regel met dezelfde sleutel in de database.
    with store._connect() as db:
        db.execute(
            "INSERT INTO items (url, dedupe_key, status, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
            (AH_URL, "ah:R-R921580", WACHTEND))

    assert queue.process_once()["status"] == BESTAAT_AL
    laatste = store.items()[0]
    assert laatste["recipe_id"] == store.item(eerste)["recipe_id"]
    assert laatste["note"] == "al eerder geïmporteerd via de brug"


def test_dubbele_die_al_in_tandoor_stond(store, client):
    """
    Een recept dat ik eerder met Tandoors eigen bookmarklet importeerde: dat
    staat niet in onze database, maar Tandoor kent het wel.
    """
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    client.create_recipe(bron.recipe, source_url=AH_URL)

    queue = ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                        fetch_photo=maak_fotofetch())
    [item_id] = store.add([AH_URL]).added

    assert queue.process_once()["status"] == BESTAAT_AL
    item = store.item(item_id)
    assert item["recipe_id"] == 1
    assert item["note"] == "stond al in Tandoor"


def test_mislukte_foto_is_een_aantekening_geen_mislukking(store, client):
    """Een recept zonder foto is nog steeds een goed recept."""
    queue = ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                        fetch_photo=maak_fotofetch(fout="foto gaf status 403"))
    [item_id] = store.add([AH_URL]).added

    assert queue.process_once()["status"] == KLAAR
    item = store.item(item_id)
    assert item["status"] == KLAAR
    assert item["recipe_id"] == 1
    assert "foto mislukte" in item["note"]


def test_importlimiet_zet_het_item_terug_in_de_rij(store):
    server = mock_server(throttle_after=1, retry_after=42)
    client = TandoorClient(server.url, "tda_x", sleep=lambda s: None)
    queue = ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                        fetch_photo=maak_fotofetch())
    try:
        store.add([AH_URL, TWEEDE_URL])
        assert queue.process_once()["status"] == KLAAR

        uitkomst = queue.process_once()
        assert uitkomst["status"] == WACHTEND
        assert uitkomst["wait"] == 42          # de worker wacht de Retry-After af

        tweede = store.items(status=WACHTEND)[0]
        assert "importlimiet" in tweede["note"]
    finally:
        server.stop()


def test_de_worker_draait_de_rij_leeg(store, client):
    queue = ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                        fetch_photo=maak_fotofetch())
    store.add([AH_URL, TWEEDE_URL])
    queue.start()
    try:
        for _ in range(100):
            if store.counts().get(KLAAR) == 2:
                break
            import time
            time.sleep(0.05)
    finally:
        queue.stop()
    assert store.counts() == {KLAAR: 2}
    assert queue.running is False


def test_een_kapotte_client_maakt_de_worker_niet_dood(store):
    """
    Wat er ook gebeurt, de thread moet blijven draaien - anders staat de hele
    brug stil zonder dat iemand het ziet.
    """
    class Kapot:
        def recipe_from_source(self, url, html):
            raise RuntimeError("iets onverwachts")

    queue = ImportQueue(store, Kapot(), delay=0, fetch=maak_fetch(),
                        fetch_photo=maak_fotofetch())
    store.add([AH_URL])
    with pytest.raises(RuntimeError):
        queue.process_once()          # process_once laat het wel zien
    queue.start()                      # maar de worker overleeft het
    try:
        import time
        time.sleep(0.2)
        assert queue.running is True
    finally:
        queue.stop()


# ------------------------------------------------------------- sorteren bij importeren

def _rij_met_sorteren(store, client, **sorteerder_opties):
    from sorteren import Sorteerder

    return ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                       fetch_photo=maak_fotofetch(),
                       sorteerder=Sorteerder(client, **sorteerder_opties))


def _ai(tandoor, **instelling):
    import requests

    requests.post(f"{tandoor.url}/__mock/ai", json=instelling)


def test_een_nieuw_recept_wordt_gesorteerd_aangemaakt(store, client, tandoor):
    rij = _rij_met_sorteren(store, client)
    [item_id] = store.add([AH_URL]).added
    assert rij.process_once()["status"] == KLAAR

    recept = tandoor.state()["recipes"][0]
    verdeeld = [s for s in recept["steps"] if s["ingredients"]]
    assert len(verdeeld) > 1, "alle ingrediënten staan nog in één stap"
    # Niets kwijt: dezelfde zeven ingrediënten als zonder sorteren.
    assert sum(len(s["ingredients"]) for s in recept["steps"]) == 7
    assert store.item(item_id)["note"] is None
    assert tandoor.state()["ai_calls"] == 1


@pytest.mark.parametrize("instelling, reden", [
    ({"ai_tegoed": 0}, "tegoed van de space is op"),
    ({"ai_gedrag": "fout"}, "could not process"),
    ({"ai_gedrag": "kapot"}, "interne fout"),
    # De limiet is gedeeld met de import zelf: recipe-from-source telt al mee.
    ({"ai_limiet": 1}, "AI-limiet bereikt"),
])
def test_mislukt_de_ai_dan_ongesorteerd_met_de_reden(store, client, tandoor, instelling, reden):
    _ai(tandoor, **instelling)
    rij = _rij_met_sorteren(store, client)
    [item_id] = store.add([AH_URL]).added
    assert rij.process_once()["status"] == KLAAR

    recept = tandoor.state()["recipes"][0]
    assert [len(s["ingredients"]) for s in recept["steps"]][0] == 7
    notitie = store.item(item_id)["note"]
    assert notitie.startswith("ongesorteerd: ") and reden in notitie


def test_weigert_tandoor_het_ai_resultaat_dan_ongesorteerd(store, client, tandoor):
    class Kapot:
        def sorteer_nieuw(self, recept):
            # Een model dat de naam kwijtraakt: daar geeft Tandoor een 400 op,
            # en daar valt met opschonen niets aan te doen.
            return {**recept, "name": ""}, None, []

    rij = ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                      fetch_photo=maak_fotofetch(), sorteerder=Kapot())
    [item_id] = store.add([AH_URL]).added
    assert rij.process_once()["status"] == KLAAR
    assert len(tandoor.state()["recipes"]) == 1          # één recept, niet twee
    assert "Tandoor weigerde het AI-resultaat" in store.item(item_id)["note"]


def test_zonder_bruikbare_provider_ongesorteerd_met_uitleg(store, client, tandoor):
    _ai(tandoor, ai_providers=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
    rij = _rij_met_sorteren(store, client)
    [item_id] = store.add([AH_URL]).added
    assert rij.process_once()["status"] == KLAAR
    assert "TANDOOR_AI_PROVIDER_ID" in store.item(item_id)["note"]
    assert tandoor.state()["ai_calls"] == 0


def test_een_mislukte_foto_en_ongesorteerd_staan_allebei_in_de_notitie(store, client, tandoor):
    from sorteren import Sorteerder

    _ai(tandoor, ai_tegoed=0)
    rij = ImportQueue(store, client, delay=0, fetch=maak_fetch(),
                      fetch_photo=maak_fotofetch(fout="404"), sorteerder=Sorteerder(client))
    [item_id] = store.add([AH_URL]).added
    rij.process_once()
    notitie = store.item(item_id)["note"]
    assert "ongesorteerd" in notitie and "foto mislukte" in notitie
