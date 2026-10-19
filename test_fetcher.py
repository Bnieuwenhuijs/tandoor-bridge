"""
Tests voor het ophalen, tegen een eigen HTTP-server op localhost.

Geen enkele test raakt het internet: de blokkadepagina die ah.nl teruggeeft
staat hieronder letterlijk in AKAMAI_BLOKKADE, overgenomen van een echte
geweigerde aanvraag.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fetcher import (
    FetchError,
    _Attempt,
    fetch_image,
    fetch_page,
    image_headers,
    looks_like_bot_wall,
)

# Letterlijk het antwoord van ah.nl op een geweigerde aanvraag.
AKAMAI_BLOKKADE = (
    b"<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n"
    b"<H1>Access Denied</H1>\n<P>Reference #0.8291eb8.1792426920.1139f2a1</P>\n"
    b"</BODY>\n</HTML>\n"
)

# Groot genoeg om niet voor een blokkadepagina te kunnen doorgaan.
RECEPTPAGINA = (
    "<html><head><title>Kip korma met spinazie</title>"
    '<link rel="canonical" href="https://www.ah.nl/allerhande/recept/R-R921580"/>'
    "</head><body>" + "<p>Snijd de ui fijn.</p>" * 2000 + "</body></html>"
).encode()

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 512

# Wat een echte browser zou teruggeven: dezelfde pagina, maar mét het schema
# dat Akamai pas na het uitvoeren van JavaScript laat zien.
MET_SCHEMA = (
    '<html><head><title>Kip korma</title>'
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Recipe","name":"Kip korma"}'
    '</script></head><body>recept</body></html>'
)


class _Handler(BaseHTTPRequestHandler):
    ontvangen: list = []
    # Wat de nagebootste browser-container antwoordt; tests zetten dit.
    render_html: str | None = MET_SCHEMA
    render_fout: str | None = None

    def do_POST(self):
        lengte = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(lengte)
        _Handler.ontvangen.append((self.path, dict(self.headers)))
        if self.path != "/render":
            self._stuur(404, "application/json", b'{"error":"onbekend"}')
            return
        if _Handler.render_fout:
            self._stuur(502, "application/json",
                        json.dumps({"error": _Handler.render_fout}).encode())
            return
        gevraagd = json.loads(body or b"{}").get("url", "")
        self._stuur(200, "application/json", json.dumps({
            "html": _Handler.render_html, "final_url": gevraagd,
            "status": 200, "titel": "Kip korma",
        }).encode())

    def do_GET(self):
        _Handler.ontvangen.append((self.path, dict(self.headers)))
        if self.path == "/ok":
            self._stuur(200, "text/html; charset=utf-8", RECEPTPAGINA)
        elif self.path == "/geweigerd":
            self._stuur(403, "text/html", AKAMAI_BLOKKADE)
        elif self.path == "/zachte-blokkade":
            self._stuur(200, "text/html", AKAMAI_BLOKKADE)
        elif self.path == "/alleen-met-volledige-headers":
            # Sommige sites willen juist de headers zien die een echte
            # navigatie verraden; die geven de minimale set een 403.
            if "Sec-Fetch-Mode" in self.headers:
                self._stuur(200, "text/html; charset=utf-8", RECEPTPAGINA)
            else:
                self._stuur(403, "text/html", AKAMAI_BLOKKADE)
        elif self.path == "/verdwenen":
            self._stuur(404, "text/html", b"<html>niet gevonden</html>")
        elif self.path == "/leeg":
            self._stuur(200, "text/html", b"   \n  ")
        elif self.path == "/omleiding":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/foto.jpg":
            self._stuur(200, "image/jpeg", JPEG)
        elif self.path == "/foto-is-html":
            self._stuur(200, "text/html", b"<html>oeps</html>")
        elif self.path == "/foto-weg":
            self._stuur(404, "image/jpeg", b"")
        else:
            self._stuur(500, "text/plain", b"onbekend pad")

    def _stuur(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # geen serverlogboek door de testuitvoer heen


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Handler.ontvangen = []
    _Handler.render_html = MET_SCHEMA
    _Handler.render_fout = None
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def laatste_headers(pad):
    for p, headers in reversed(_Handler.ontvangen):
        if p == pad:
            return headers
    raise AssertionError(f"{pad} is nooit opgevraagd")


def test_gewone_pagina_komt_onbewerkt_binnen(site):
    result = fetch_page(f"{site}/ok")
    assert result.status == 200
    assert result.via == "requests"
    assert "Kip korma met spinazie" in result.html
    # Onbewerkt: de canonical staat er nog precies zoals de site hem gaf.
    assert '<link rel="canonical" href="https://www.ah.nl/allerhande/recept/R-R921580"/>' in result.html


def test_eerste_poging_gebruikt_de_bewezen_headers(site):
    """
    De eerste poging is de set uit recipe_parser, en dat is geen willekeurige
    keuze. De uitgebreidere set hieronder leverde bij AH juist een 403 op:
    Akamai kijkt naar de combinatie, en Chrome-headers zonder bijpassende
    TLS-vingerafdruk zijn verdachter dan een kale User-Agent.
    """
    fetch_page(f"{site}/ok")
    headers = laatste_headers("/ok")
    assert "Chrome/126" in headers["User-Agent"]
    assert headers["Accept-Language"] == "nl-NL,nl;q=0.9,en;q=0.8"
    assert "Sec-Fetch-Mode" not in headers          # bewust niet, nog niet
    assert "Sec-Ch-Ua" not in headers


def test_tweede_poging_stuurt_de_volledige_headers(site):
    """En een site die juist dié headers wil, komt alsnog binnen."""
    result = fetch_page(f"{site}/alleen-met-volledige-headers")
    assert result.via == "requests+headers"
    assert "Kip korma met spinazie" in result.html

    headers = laatste_headers("/alleen-met-volledige-headers")
    assert headers["Sec-Fetch-Mode"] == "navigate"
    assert headers["Upgrade-Insecure-Requests"] == "1"


def test_omleiding_wordt_gevolgd(site):
    result = fetch_page(f"{site}/omleiding")
    assert result.final_url.endswith("/ok")
    assert result.url.endswith("/omleiding")  # de gevraagde URL blijft bewaard


def test_403_is_een_blokkade(site):
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/geweigerd", impersonate=False)
    assert fout.value.status == 403
    assert "geblokkeerd" in fout.value.reason


def test_blokkade_met_status_200_wordt_herkend(site):
    """Akamai geeft de blokkade soms met een 200 terug."""
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/zachte-blokkade", impersonate=False)
    assert "blokkadepagina" in fout.value.reason


def test_verdwenen_pagina_krijgt_eigen_melding(site):
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/verdwenen")
    assert "bestaat niet meer" in fout.value.reason
    assert fout.value.status == 404


def test_lege_pagina(site):
    with pytest.raises(FetchError, match="leeg"):
        fetch_page(f"{site}/leeg")


def test_tweede_poging_met_curl_cffi(site, monkeypatch):
    """Wat requests geweigerd krijgt, mag curl_cffi nog proberen."""
    import fetcher

    def nagebootst(url, *, timeout):
        return _Attempt(
            True, status=200,
            result=fetcher.FetchResult(url, url, 200, "<html>gelukt</html>", "curl_cffi"),
        )

    monkeypatch.setattr(fetcher, "_attempt_curl", nagebootst)
    result = fetch_page(f"{site}/geweigerd")
    assert result.via == "curl_cffi"


def test_zonder_curl_cffi_blijft_de_reden_leesbaar(site, monkeypatch):
    import fetcher

    monkeypatch.setattr(fetcher, "_attempt_curl", lambda url, *, timeout: None)
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/geweigerd")
    assert "403" in fout.value.reason
    assert "curl_cffi" in fout.value.reason


def test_beide_pogingen_geblokkeerd(site, monkeypatch):
    import fetcher

    monkeypatch.setattr(
        fetcher, "_attempt_curl",
        lambda url, *, timeout: _Attempt(False, "geblokkeerd (status 403)", 403),
    )
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/geweigerd")
    # Elke poging staat met z'n eigen reden in de melding, zodat je ziet
    # welke manieren geprobeerd zijn.
    assert "minimale headers" in fout.value.reason
    assert "volledige headers" in fout.value.reason
    assert "curl_cffi" in fout.value.reason


def test_foto_komt_binnen_met_referer(site):
    inhoud, content_type = fetch_image(f"{site}/foto.jpg", referer=f"{site}/ok")
    assert inhoud.startswith(b"\xff\xd8\xff")  # JPEG
    assert content_type == "image/jpeg"
    headers = laatste_headers("/foto.jpg")
    # Beeld-CDN's weigeren een aanvraag zonder Referer van de eigen pagina.
    assert headers["Referer"] == f"{site}/ok"
    assert headers["Sec-Fetch-Dest"] == "image"


def test_foto_die_geen_foto_is(site):
    with pytest.raises(FetchError, match="geen afbeelding"):
        fetch_image(f"{site}/foto-is-html")


def test_foto_met_foutstatus(site):
    with pytest.raises(FetchError) as fout:
        fetch_image(f"{site}/foto-weg")
    assert fout.value.status == 404


def test_grote_pagina_met_verdachte_woorden_is_geen_blokkade():
    """
    De grens op paginagrootte voorkomt valse blokkades: een recept mag het
    woord "access denied" bevatten zonder dat de brug afhaakt.
    """
    groot = "x" * 50_000 + "access denied"
    assert looks_like_bot_wall(200, groot) is None
    assert looks_like_bot_wall(200, "<html>Access Denied</html>") is not None
    assert looks_like_bot_wall(403, "") == "status 403"
    assert looks_like_bot_wall(200, "<html>een gewoon recept</html>") is None


def test_404_is_geen_blokkade():
    """Een tweede poging met curl_cffi heeft bij een 404 geen zin."""
    assert looks_like_bot_wall(404, "niet gevonden") is None


def test_fotoheaders_zonder_referer():
    headers = image_headers()
    assert "Referer" not in headers
    assert headers["Accept"].startswith("image/")


# ------------------------------------------------- de browser-container

def test_browser_is_de_laatste_redding(site):
    """
    Albert Heijn weigert elke server-side aanvraag met 403, ook curl_cffi met
    TLS-vingerafdruk: Akamai zet z'n sensor-cookies pas nadat het JavaScript
    op de pagina gedraaid heeft. Dan is een echte browser wat er nog overblijft.
    """
    result = fetch_page(f"{site}/geweigerd", renderer_url=site)
    assert result.via == "browser"
    assert "Kip korma" in result.html


def test_browser_wordt_ook_gebruikt_bij_een_pagina_zonder_schema(site):
    """
    Een cookiemuur komt met een nette 200 terug, dus "het kwam door" is niet
    genoeg: er moet een recept in staan.
    """
    assert "application/ld+json" not in RECEPTPAGINA.decode()

    result = fetch_page(f"{site}/ok", renderer_url=site)
    assert result.via == "browser"
    assert "application/ld+json" in result.html


def test_zonder_browser_container_geen_extra_poging(site):
    """
    Zonder browser hebben extra pogingen geen zin: dezelfde server stuurt
    dezelfde inhoud. Dan houden we wat we hebben.
    """
    result = fetch_page(f"{site}/ok", renderer_url="")
    assert result.via == "requests"
    assert "application/ld+json" not in result.html


def test_een_kapotte_browser_container_laat_de_reden_zien(site):
    import test_fetcher

    test_fetcher._Handler.render_fout = "Page.goto: net::ERR_TIMED_OUT"
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/geweigerd", renderer_url=site)
    assert "browser" in fout.value.reason
    assert "ERR_TIMED_OUT" in fout.value.reason


def test_browser_container_onbereikbaar(site):
    """De brug mag niet omvallen als de tweede container uit staat."""
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/geweigerd", renderer_url="http://127.0.0.1:1")
    assert "niet te bereiken" in fout.value.reason


def test_verdwenen_pagina_kost_geen_browserronde(site):
    """Een 404 is definitief; daar helpt geen enkele andere poging aan."""
    _Handler.ontvangen = []
    with pytest.raises(FetchError) as fout:
        fetch_page(f"{site}/verdwenen", renderer_url=site)
    assert fout.value.status == 404
    assert not [p for p, _ in _Handler.ontvangen if p == "/render"]
