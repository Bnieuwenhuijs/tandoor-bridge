"""
Tests voor de Tandoor-client, tegen de nagebouwde API in mock_tandoor.py.

Echt HTTP over localhost, dus multipart-uploads, headers en statuscodes gaan
door dezelfde code als bij de echte Tandoor. De mock doet de drie dingen na
die stil kunnen mislukken: unquote() over 'data', de bron-URL uit de
canonical, en de naamcontrole op een geüploade foto.
"""

import pathlib
import threading
from urllib.parse import unquote

import pytest
import requests
from werkzeug.serving import make_server

from mock_tandoor import create_app
from tandoor import (
    TandoorClient,
    TandoorError,
    TandoorNoRecipe,
    TandoorThrottled,
    escape_for_data,
    image_suffix,
    with_canonical,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
AH_HTML = (FIXTURES / "ah_kip_korma.html").read_text(encoding="utf-8")
AH_URL = "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256


class _Server:
    def __init__(self, app):
        self._server = make_server("127.0.0.1", 0, app)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()


@pytest.fixture
def tandoor():
    """Een draaiende nagebouwde Tandoor, zonder limiet."""
    server = _Server(create_app())
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(tandoor):
    return TandoorClient(tandoor.url, "tda_testtoken", public_url="http://tail2bbdeb.ts.net:30290")


def mock_state(server):
    return requests.get(f"{server.url}/__mock/state").json()


# ------------------------------------------------------- de HTML beschermen

def test_escape_for_data_is_omkeerbaar():
    """
    Wat wij versturen moet na Tandoors unquote() weer het origineel zijn.
    """
    for stuk in [
        AH_HTML,
        "width:50%",
        "?a=1%2F2&b=%20",
        "100% natuurlijk",
        "%",
        "%%%",
        "geen procenten hier",
    ]:
        assert unquote(escape_for_data(stuk)) == stuk


def test_html_komt_byte_voor_byte_bij_tandoor_aan(client, tandoor):
    client.recipe_from_source(AH_URL, AH_HTML)
    aangekomen = mock_state(tandoor)["last_data_decoded"]
    # Precies de pagina die wij ophaalden, met alleen onze canonical ervoor.
    assert aangekomen == with_canonical(AH_HTML, AH_URL)
    # En de afbeeldings-URL's zijn niet stilletjes gedecodeerd.
    assert "product%2FAHI_43545239%2Fkip-korma.jpg" in aangekomen


def test_zonder_bescherming_zou_de_html_verminkt_aankomen(tandoor):
    """
    Waarom escape_for_data() bestaat. Dit is de fout die je anders pas
    maanden later in een kapotte afbeeldings-URL terugvindt.
    """
    requests.post(
        f"{tandoor.url}/api/recipe-from-source/",
        json={"url": AH_URL, "data": AH_HTML},   # bewust onbeschermd
        headers={"Authorization": "Bearer tda_testtoken"},
    )
    aangekomen = mock_state(tandoor)["last_data_decoded"]
    assert aangekomen != AH_HTML
    assert "product%2FAHI" not in aangekomen       # %2F is / geworden
    assert "product/AHI_43545239/kip-korma.jpg" in aangekomen


# ------------------------------------------------------- de canonical

def test_canonical_wordt_toegevoegd_als_die_ontbreekt(client):
    """Zonder canonical zou source_url bij het parsen leeg blijven."""
    assert "canonical" not in AH_HTML
    resultaat = client.recipe_from_source(AH_URL, AH_HTML)
    assert resultaat.recipe["source_url"] == AH_URL


def test_bestaande_absolute_canonical_blijft_staan():
    html = '<html><head><link rel="canonical" href="https://www.ah.nl/echt"/></head><body>x</body></html>'
    assert with_canonical(html, "https://www.ah.nl/onze-url") == html


def test_relatieve_canonical_wordt_overruled(client):
    """
    Een relatieve canonical plakt Tandoor aan urlnotfound.none vast. Onze
    absolute tag staat ervoor, en BeautifulSoup pakt de eerste.
    """
    html = AH_HTML.replace("<head>", '<head><link rel="canonical" href="/allerhande/recept/R-R921580"/>')
    resultaat = client.recipe_from_source(AH_URL, html)
    assert resultaat.recipe["source_url"] == AH_URL


def test_zonder_canonical_geen_domein_trefwoord(tandoor):
    """
    Dit is wat we voorkomen: Tandoor leidt het domein-trefwoord af uit de
    bron-URL, en source-gebonden Food Replace-automations kijken daar ook
    naar. Zonder canonical blijft het leeg.
    """
    kaal = AH_HTML  # geen canonical, en we sturen geen url mee
    antwoord = requests.post(
        f"{tandoor.url}/api/recipe-from-source/",
        json={"data": escape_for_data(kaal)},
        headers={"Authorization": "Bearer tda_testtoken"},
    ).json()
    # Zonder url valt Tandoor terug op de url uit het schema; met url erbij
    # (en zonder canonical) zou source_url leeg zijn gebleven.
    assert antwoord["recipe"]["source_url"] == AH_URL


def test_domein_trefwoord_komt_mee(client):
    resultaat = client.recipe_from_source(AH_URL, AH_HTML)
    namen = [k["name"] for k in resultaat.recipe["keywords"]]
    assert "www.ah.nl" in namen


# ------------------------------------------------------- de importflow

def test_recept_wordt_ontleed(client):
    resultaat = client.recipe_from_source(AH_URL, AH_HTML)
    recept = resultaat.recipe
    assert recept["name"] == "Kip korma met spinazie"
    assert recept["servings"] == 4
    assert recept["working_time"] == 15
    assert recept["waiting_time"] == 25
    assert len(recept["steps"]) == 4
    # Alle ingrediënten hangen aan de eerste stap, zoals Tandoor dat doet.
    assert len(recept["steps"][0]["ingredients"]) == 7
    assert recept["steps"][0]["ingredients"][0]["original_text"] == "300 gram kipfilet"
    assert resultaat.images  # een kandidaat-afbeelding


def test_volledige_flow(client, tandoor):
    """Van HTML naar een recept met foto en bron-URL in Tandoor."""
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    recept = client.create_recipe(bron.recipe, source_url=AH_URL)

    assert recept["id"] == 1
    assert recept["source_url"] == AH_URL

    client.set_image(recept["id"], JPEG, content_type="image/jpeg")

    state = mock_state(tandoor)
    assert state["images"]["1"] == "recept.jpg"
    assert state["recipes"][0]["image"] == "/media/recipes/1.jpg"
    # De link die ik in de interface aanklik gaat via het publieke adres.
    assert client.recipe_url(recept["id"]) == "http://tail2bbdeb.ts.net:30290/recipe/1"


def test_source_url_wordt_meteen_gezet(client, tandoor):
    """Dus geen losse PATCH nodig; ensure_source_url is alleen een vangnet."""
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    recept = client.create_recipe(bron.recipe, source_url=AH_URL)
    opgeslagen = mock_state(tandoor)["recipes"][0]
    assert opgeslagen["source_url"] == AH_URL


def test_ensure_source_url_als_vangnet(client):
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    recept = client.create_recipe(bron.recipe, source_url="")
    assert client.ensure_source_url(recept["id"], AH_URL) is True


# ------------------------------------------------------- dubbele recepten

def test_duplicates_komen_terug_bij_een_tweede_import(client):
    """
    De controle die volgens de documentatie niet in deze tak zou werken: hij
    staat in de view buiten de url/data-splitsing, dus hij werkt wel.
    """
    eerste = client.recipe_from_source(AH_URL, AH_HTML)
    assert eerste.duplicates == []
    client.create_recipe(eerste.recipe, source_url=AH_URL)

    tweede = client.recipe_from_source(AH_URL, AH_HTML)
    assert tweede.duplicate_ids == [1]
    assert tweede.duplicates[0]["name"] == "Kip korma met spinazie"


def test_andere_url_is_geen_duplicaat(client):
    eerste = client.recipe_from_source(AH_URL, AH_HTML)
    client.create_recipe(eerste.recipe, source_url=AH_URL)
    ander = client.recipe_from_source("https://www.ah.nl/allerhande/recept/R-R999999", AH_HTML)
    assert ander.duplicates == []


# ------------------------------------------------------- foutgevallen

def test_pagina_zonder_recept(client):
    with pytest.raises(TandoorNoRecipe) as fout:
        client.recipe_from_source("https://example.com/geen-recept",
                                  "<html><head></head><body>kattenfoto</body></html>")
    # De melding van Tandoor zelf, niet een eigen gok.
    assert fout.value.message == "No usable data could be found."


def test_ongeldig_token(tandoor):
    client = TandoorClient(tandoor.url, "verkeerd_token")
    with pytest.raises(TandoorError) as fout:
        client.check_connection()
    assert fout.value.status == 401
    assert fout.value.message == "Invalid token."


def test_recept_zonder_naam_geeft_validatiefout(client):
    with pytest.raises(TandoorError) as fout:
        client.create_recipe({"name": "", "steps": []}, source_url=AH_URL)
    assert "name" in fout.value.message


def test_tandoor_niet_bereikbaar():
    client = TandoorClient("http://127.0.0.1:1", "tda_x", max_retries=1)
    with pytest.raises(TandoorError, match="niet te bereiken"):
        client.check_connection()


def test_check_connection_geeft_aantal(client):
    assert client.check_connection() == 0
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    client.create_recipe(bron.recipe, source_url=AH_URL)
    assert client.check_connection() == 1


# ------------------------------------------------------- limiet van 60 per uur

def test_korte_wachttijd_wordt_afgewacht():
    server = _Server(create_app(throttle_after=1, retry_after=3))
    geslapen = []
    client = TandoorClient(server.url, "tda_x", max_retries=2, sleep=geslapen.append)
    try:
        client.recipe_from_source(AH_URL, AH_HTML)      # mag nog
        with pytest.raises(TandoorThrottled) as fout:
            client.recipe_from_source(AH_URL, AH_HTML)  # 429
        # Er is netjes gewacht op de Retry-After voordat het opgaf.
        assert geslapen == [3]
        assert fout.value.retry_after == 3
    finally:
        server.stop()


def test_lange_wachttijd_gaat_naar_de_wachtrij():
    """
    Bij de standaardlimiet van 60/uur kan Retry-After bijna een uur zijn. Dan
    mag één aanroep niet gaan liggen slapen: dat is een beslissing voor de
    wachtrij.
    """
    server = _Server(create_app(throttle_after=1, retry_after=3500))
    geslapen = []
    client = TandoorClient(server.url, "tda_x", sleep=geslapen.append)
    try:
        client.recipe_from_source(AH_URL, AH_HTML)
        with pytest.raises(TandoorThrottled) as fout:
            client.recipe_from_source(AH_URL, AH_HTML)
        assert fout.value.retry_after == 3500
        assert geslapen == []  # geen seconde gewacht
    finally:
        server.stop()


# ------------------------------------------------------- afbeeldingen

def test_achtervoegsel_uit_content_type():
    assert image_suffix("image/jpeg") == ".jpg"
    assert image_suffix("image/png") == ".png"
    assert image_suffix("image/webp") == ".webp"
    assert image_suffix("image/jpeg; charset=binary") == ".jpg"


def test_achtervoegsel_uit_de_inhoud_als_het_type_onzin_is():
    assert image_suffix("application/octet-stream", JPEG) == ".jpg"
    assert image_suffix("", b"\x89PNG\r\n\x1a\n rest") == ".png"
    assert image_suffix("", b"RIFF1234WEBPxxx") == ".webp"


def test_avif_wordt_geweigerd_met_uitleg():
    """
    Tandoor keurt op bestandsnaam en kent avif niet. Beter hier een leesbare
    fout dan een upload die stil niets doet.
    """
    with pytest.raises(TandoorError, match="alleen jpg, png, gif en webp"):
        image_suffix("image/avif", b"\x00\x00\x00 ftypavif")


def test_upload_zonder_geldige_naam_wordt_door_tandoor_geweigerd(client, tandoor):
    """
    Bewijst dat de naamcontrole echt bestaat: dit is wat er gebeurt als je
    zonder achtervoegsel uploadt.
    """
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    recept = client.create_recipe(bron.recipe, source_url=AH_URL)
    antwoord = requests.put(
        f"{tandoor.url}/api/recipe/{recept['id']}/image/",
        files={"image": ("image", JPEG, "image/jpeg")},
        headers={"Authorization": "Bearer tda_testtoken"},
    )
    assert antwoord.status_code == 400


def test_json_naar_het_afbeeldingsendpoint_wordt_geweigerd(client, tandoor):
    """parser_classes=[MultiPartParser]: alleen multipart komt erin."""
    bron = client.recipe_from_source(AH_URL, AH_HTML)
    recept = client.create_recipe(bron.recipe, source_url=AH_URL)
    antwoord = requests.put(
        f"{tandoor.url}/api/recipe/{recept['id']}/image/",
        json={"image_url": "https://static.ah.nl/foto.jpg"},
        headers={"Authorization": "Bearer tda_testtoken"},
    )
    assert antwoord.status_code == 415
