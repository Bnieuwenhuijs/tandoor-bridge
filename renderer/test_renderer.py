"""
Test voor de browser-container.

Slaat over als Playwright of Chromium hier niet beschikbaar is, zodat de
pytest-suite van de brug bruikbaar blijft zonder een browser erbij te
installeren. Draait wel als je in renderer/ werkt met playwright geïnstalleerd:

    pip install -r renderer/requirements.txt
    playwright install chromium
    python -m pytest renderer/
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

playwright = pytest.importorskip("playwright.sync_api",
                                 reason="playwright niet geïnstalleerd")

# Een pagina mét recept, en de challenge die een botbeveiliging eerst stuurt.
RECEPT = ('<html><head><title>Traybake met kip</title>'
          '<script type="application/ld+json">'
          '{"@context":"https://schema.org","@type":"Recipe",'
          '"name":"Traybake met kip","recipeIngredient":["500 g kipfilet"]}'
          '</script></head><body>recept</body></html>').encode()

CHALLENGE = (b'<html><head><title>Access Denied</title></head><body>'
             b'<h1>Access Denied</h1><p>Reference #0.123abc</p>'
             b'<script src="/sensor.js"></script></body></html>')


class _Akamai(BaseHTTPRequestHandler):
    """Gedraagt zich als AH: de eerste aanvraag krijgt een challenge."""
    hits = 0

    def do_GET(self):
        if self.path.endswith(".js"):
            self._stuur(200, "application/javascript", b"// sensor")
            return
        _Akamai.hits += 1
        if _Akamai.hits == 1:
            self._stuur(403, "text/html", CHALLENGE)
        else:
            self._stuur(200, "text/html", RECEPT)

    def _stuur(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Akamai)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Akamai.hits = 0
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def client(monkeypatch):
    """De renderer-app, met korte wachttijden zodat de test niet traag is."""
    monkeypatch.setenv("CHALLENGE_WACHT_MS", "800")
    monkeypatch.setenv("SETTLE_MS", "200")
    if pad := os.environ.get("CHROMIUM_PATH"):
        monkeypatch.setenv("CHROMIUM_PATH", pad)

    # Bewust via de bestandsnaam en niet via "import app": de brug heeft ook
    # een app.py, en die staat in sys.modules zodra haar tests gedraaid zijn.
    # Dan krijg je de verkeerde module en een onbegrijpelijke ValueError.
    import importlib.util

    pad = os.path.join(os.path.dirname(__file__), "renderer.py")
    spec = importlib.util.spec_from_file_location("tandoor_renderer", pad)
    renderer_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer_app)

    flask_app = renderer_app.create_app()
    with flask_app.test_client() as http:
        if http.get("/healthz").status_code != 200:
            pytest.skip("browserthread kwam niet op")
        # /healthz zegt alleen dat de thread leeft; de browser start pas bij
        # het eerste verzoek. Dus hier echt proberen, en overslaan als
        # Chromium op deze machine niet op te starten is.
        proef = http.get("/fingerprint")
        if proef.status_code != 200:
            pytest.skip(f"Chromium start hier niet: "
                        f"{proef.get_json().get('error', '?')[:120]}")
        yield http


def test_een_challenge_wordt_doorbroken(client, site):
    """
    Een botbeveiliging stuurt op de eerste aanvraag geen pagina maar een
    script. Dat moet draaien, en pas de herladen pagina heeft het recept.
    Zonder die herlading leest de renderer de challenge en meldt 403 - ook op
    een verbinding waar het wél zou lukken.
    """
    antwoord = client.post("/render", json={"url": f"{site}/recept"})
    assert antwoord.status_code == 200, antwoord.get_data(as_text=True)[:300]

    data = antwoord.get_json()
    assert data["status"] == 200
    assert data["titel"] == "Traybake met kip"
    assert "application/ld+json" in data["html"]

    # En je ziet in het antwoord dat er een challenge doorbroken is.
    pogingen = data["pogingen"]
    assert len(pogingen) >= 2
    assert pogingen[0]["status"] == 403
    assert pogingen[-1]["status"] == 200


def test_de_browser_verraadt_zich_niet(client):
    """De twee signalen waar een botdetectie als eerste naar kijkt."""
    antwoord = client.get("/fingerprint")
    assert antwoord.status_code == 200

    signalen = antwoord.get_json()
    assert "Headless" not in signalen["ua"]
    assert not signalen["webdriver"]
    assert signalen["languages"].startswith("nl")


def test_een_url_zonder_schema_wordt_geweigerd(client):
    assert client.post("/render", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.post("/render", json={}).status_code == 400
