"""
Tests voor de webkant: de pagina en de JSON-endpoints.

De worker draait hier niet mee (start_worker=False), zodat elke test zelf
bepaalt wanneer er iets verwerkt wordt.
"""

import io

import pytest

from app import create_app
from conftest import AH_URL, mock_server
from importqueue import KLAAR, MISLUKT, ImportQueue, Store
from tandoor import TandoorClient

TWEEDE_URL = "https://www.leukerecepten.nl/recepten/lasagnesoep/"
BERICHT = f"Lieke: deze moeten we maken! {AH_URL} en {TWEEDE_URL} ook 😋"


@pytest.fixture
def brug(tmp_path, client):
    store = Store(tmp_path / "bridge.sqlite3")
    queue = ImportQueue(store, client, delay=0)
    app = create_app(store=store, client=client, queue=queue, start_worker=False)
    app.config.update(TESTING=True)
    with app.test_client() as http:
        yield http, store, app


# ---------------------------------------------------------------- de pagina

def test_de_pagina_laadt(brug):
    http, _, _ = brug
    antwoord = http.get("/")
    assert antwoord.status_code == 200
    assert "Tandoor Bridge" in antwoord.get_data(as_text=True)


def test_healthz(brug):
    http, _, _ = brug
    assert http.get("/healthz").get_json()["ok"] is True


# ---------------------------------------------------------------- voorbeeld

def test_voorbeeld_laat_zien_wat_erin_zit(brug):
    http, _, _ = brug
    data = http.post("/api/preview", json={"text": BERICHT}).get_json()
    assert data["gevonden"] == 2
    assert [r["state"] for r in data["urls"]] == ["nieuw", "nieuw"]
    assert data["urls"][0]["url"] == AH_URL


def test_voorbeeld_markeert_wat_al_geimporteerd_is(brug):
    http, store, _ = brug
    [item_id] = store.add([AH_URL]).added
    store.update(item_id, status=KLAAR, title="Kip korma", recipe_id=7)

    data = http.post("/api/preview", json={"text": BERICHT}).get_json()
    eerste = data["urls"][0]
    assert eerste["state"] == "al_geimporteerd"
    assert eerste["title"] == "Kip korma"
    # Met een aanklikbare link naar het bestaande recept.
    assert eerste["tandoor_url"].endswith("/recipe/7")


# ---------------------------------------------------------------- importeren

def test_importeren_zet_in_de_wachtrij(brug):
    http, store, _ = brug
    antwoord = http.post("/api/import", json={"text": BERICHT})
    assert antwoord.status_code == 202
    data = antwoord.get_json()
    assert data["toegevoegd"] == 2
    assert len(store.items()) == 2


def test_een_losse_url_zoals_http_shortcuts_hem_stuurt(brug):
    """De Android-app stuurt liever {"url": ...} dan een heel bericht."""
    http, store, _ = brug
    antwoord = http.post("/api/import", json={"url": "https://www.ah.nl/r/921580"})
    assert antwoord.status_code == 202
    # En de deelvorm is onderweg herschreven naar de echte recept-URL.
    assert store.items()[0]["url"] == "https://www.ah.nl/allerhande/recept/R-R921580"


def test_een_lijst_urls(brug):
    http, store, _ = brug
    http.post("/api/import", json={"urls": [AH_URL, TWEEDE_URL]})
    assert len(store.items()) == 2


def test_url_via_de_querystring(brug):
    """Zodat een simpele GET-achtige integratie ook werkt."""
    http, store, _ = brug
    assert http.post(f"/api/import?url={AH_URL}").status_code == 202
    assert len(store.items()) == 1


def test_zonder_tekst(brug):
    http, _, _ = brug
    antwoord = http.post("/api/import", json={})
    assert antwoord.status_code == 400
    assert "geen tekst" in antwoord.get_json()["error"]


def test_tekst_zonder_links(brug):
    http, _, _ = brug
    antwoord = http.post("/api/import", json={"text": "wat eten we vandaag?"})
    assert antwoord.status_code == 400
    assert "geen enkele link" in antwoord.get_json()["error"]


def test_dubbele_worden_gemeld_niet_toegevoegd(brug):
    http, store, _ = brug
    http.post("/api/import", json={"text": BERICHT})
    data = http.post("/api/import", json={"text": BERICHT}).get_json()
    assert data["toegevoegd"] == 0
    assert len(data["overgeslagen"]) == 2
    assert len(store.items()) == 2


# ---------------------------------------------------------------- bestand

def test_lijst_uit_een_bestand(brug):
    http, store, _ = brug
    inhoud = f"{AH_URL}\n{TWEEDE_URL}\n\n# deze regel is geen link\n".encode()
    antwoord = http.post("/api/upload", data={
        "file": (io.BytesIO(inhoud), "recepten.txt")})
    assert antwoord.status_code == 202
    assert antwoord.get_json()["toegevoegd"] == 2
    assert {i["source"] for i in store.items()} == {"bestand"}


def test_upload_zonder_bestand(brug):
    http, _, _ = brug
    assert http.post("/api/upload", data={}).status_code == 400


# ---------------------------------------------------------------- geschiedenis

def test_geschiedenis_met_link_naar_tandoor(brug):
    http, store, _ = brug
    [item_id] = store.add([AH_URL]).added
    store.update(item_id, status=KLAAR, title="Kip korma", recipe_id=12)

    data = http.get("/api/items").get_json()
    assert data["counts"] == {KLAAR: 1}
    assert data["items"][0]["tandoor_url"].endswith("/recipe/12")


def test_geschiedenis_filtert_op_mislukt(brug):
    http, store, _ = brug
    ids = store.add([AH_URL, TWEEDE_URL]).added
    store.update(ids[0], status=KLAAR, recipe_id=1)
    store.update(ids[1], status=MISLUKT, error="geblokkeerd (status 403)")

    data = http.get("/api/items?status=mislukt").get_json()
    assert len(data["items"]) == 1
    assert data["items"][0]["error"] == "geblokkeerd (status 403)"


def test_opnieuw_proberen(brug):
    http, store, _ = brug
    [item_id] = store.add([AH_URL]).added
    store.update(item_id, status=MISLUKT, error="geblokkeerd")

    assert http.post(f"/api/items/{item_id}/retry").status_code == 200
    assert store.item(item_id)["status"] == "wachtend"
    # Een tweede keer kan niet: hij staat niet meer op mislukt.
    assert http.post(f"/api/items/{item_id}/retry").status_code == 409


def test_alle_mislukte_in_een_keer(brug):
    http, store, _ = brug
    for item_id in store.add([AH_URL, TWEEDE_URL]).added:
        store.update(item_id, status=MISLUKT, error="stuk")
    assert http.post("/api/retry-failed").get_json()["opnieuw"] == 2


# ---------------------------------------------------------------- status

def test_status(brug):
    http, store, _ = brug
    store.add([AH_URL])
    data = http.get("/api/status").get_json()
    assert data["counts"] == {"wachtend": 1}
    assert data["worker"] is False      # in de tests draait hij niet mee
    assert data["tandoor"].startswith("http")


def test_check_bevestigt_de_verbinding(brug):
    http, _, _ = brug
    data = http.get("/api/check").get_json()
    assert data["ok"] is True
    assert data["recepten"] == 0


def test_check_meldt_een_onbereikbare_tandoor(tmp_path):
    store = Store(tmp_path / "b.sqlite3")
    stille_client = TandoorClient("http://127.0.0.1:1", "tda_x", max_retries=1)
    app = create_app(store=store, client=stille_client,
                     queue=ImportQueue(store, stille_client), start_worker=False)
    with app.test_client() as http:
        antwoord = http.get("/api/check")
        assert antwoord.status_code == 502
        assert "niet te bereiken" in antwoord.get_json()["error"]


# ---------------------------------------------------------------- de keten

def test_van_bericht_tot_recept_in_tandoor(brug, tandoor, monkeypatch):
    """
    De hele route in één test: bericht plakken, importeren, de worker één
    item laten doen, en kijken of het recept met bron en foto in Tandoor
    staat. Alleen het ophalen is nagebootst.
    """
    http, store, app = brug
    from conftest import AH_HTML, JPEG
    from fetcher import FetchResult

    monkeypatch.setattr(app.queue, "_fetch",
                        lambda url, **kw: FetchResult(url, url, 200, AH_HTML, "requests"))
    monkeypatch.setattr(app.queue, "_fetch_photo",
                        lambda url, **kw: (JPEG, "image/jpeg"))

    http.post("/api/import", json={"text": f"deze! {AH_URL}"})
    assert app.queue.process_once()["status"] == KLAAR

    item = http.get("/api/items").get_json()["items"][0]
    assert item["status"] == KLAAR
    assert item["title"] == "Kip korma met spinazie"
    assert item["tandoor_url"].endswith("/recipe/1")
    assert tandoor.state()["recipes"][0]["source_url"] == AH_URL
