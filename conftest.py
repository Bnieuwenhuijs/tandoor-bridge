"""
Gedeeld testgereedschap: een draaiende nagebouwde Tandoor op localhost.

Echt HTTP, zodat multipart-uploads, headers en statuscodes door dezelfde code
gaan als bij de echte Tandoor.
"""

import pathlib
import threading

import pytest
import requests
from werkzeug.serving import make_server

from mock_tandoor import create_app
from tandoor import TandoorClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
AH_HTML = (FIXTURES / "ah_kip_korma.html").read_text(encoding="utf-8")
AH_URL = "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 256
PUBLIC_URL = "http://tail2bbdeb.ts.net:30290"


class MockServer:
    def __init__(self, app):
        self._server = make_server("127.0.0.1", 0, app)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def state(self) -> dict:
        return requests.get(f"{self.url}/__mock/state").json()

    def stop(self):
        self._server.shutdown()


def mock_server(**opties) -> MockServer:
    """Een nagebouwde Tandoor met eigen instellingen; zelf stoppen."""
    return MockServer(create_app(**opties))


@pytest.fixture
def tandoor():
    server = mock_server()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def client(tandoor):
    return TandoorClient(tandoor.url, "tda_testtoken", public_url=PUBLIC_URL)
