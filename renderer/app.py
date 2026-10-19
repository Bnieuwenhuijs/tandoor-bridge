"""
Losse container die een pagina met een echte browser ophaalt.

Waarom dit bestaat: Albert Heijn weigert elke server-side aanvraag met een
403, ook met de juiste headers en ook met curl_cffi's TLS-vingerafdruk. De
botbeveiliging van Akamai zet z'n sensor-cookies (_abck, bm_sz) pas nadat
JavaScript op de pagina gedraaid heeft, en zonder die cookies is er geen
pagina. Een echte browser lost dat op, want die draait dat JavaScript gewoon.

Dit is bewust een aparte container: de brug blijft daarmee licht, en wie geen
AH importeert pullt dit image nooit.

    POST /render  {"url": "..."}            -> {"html": ..., "final_url": ...}
    GET  /healthz

De browser start pas bij het eerste verzoek en sluit zichzelf weer af als er
BROWSER_IDLE_SECONDS niets te doen was. Tussen twee importrondes kost deze
container dus geen geheugen - dat telt op een NAS waar ook Immich, Jellyfin en
Audiobookshelf staan.

Alleen voor je eigen netwerk: dit haalt elke URL op die je hem geeft, dus zet
hem niet open naar het internet.
"""

import logging
import os
import queue
import sys
import threading

from flask import Flask, jsonify, request

log = logging.getLogger("renderer")

IDLE_SECONDS = int(os.environ.get("BROWSER_IDLE_SECONDS", "120"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "30000"))
# Na het laden nog even wachten: Akamai's script zet z'n cookies en de pagina
# vult zich pas daarna. Dit is het verschil tussen een lege en een volle pagina.
SETTLE_MS = int(os.environ.get("SETTLE_MS", "1500"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH") or None

# Een cookiemelding staat tussen ons en het recept. Een knop zoeken op tekst
# werkt op meer sites dan een selector per site bijhouden.
CONSENT_TEKSTEN = (
    "Accepteer alle", "Alles accepteren", "Accepteren", "Akkoord",
    "Ik ga akkoord", "Alle cookies toestaan", "Accept all",
)


class Renderer:
    """
    Eén thread die de browser bezit.

    De sync-API van Playwright mag alleen gebruikt worden vanuit de thread die
    hem heeft aangemaakt. Daarom doet één eigen thread al het werk en leveren
    de webverzoeken hun opdracht via een wachtrij af.
    """

    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="browser", daemon=True)
        self._gestart = threading.Event()
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def render(self, url: str, timeout_ms: int) -> dict:
        antwoord: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((url, timeout_ms, antwoord))
        try:
            return antwoord.get(timeout=(timeout_ms / 1000) + 20)
        except queue.Empty:
            return {"error": "de browser antwoordde niet binnen de tijd"}

    # -- de browserthread ------------------------------------------------

    def _run(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            self._gestart.set()
            browser = None
            while True:
                try:
                    url, timeout_ms, antwoord = self._jobs.get(timeout=IDLE_SECONDS)
                except queue.Empty:
                    if browser is not None:
                        log.info("niets te doen, browser gaat uit")
                        browser.close()
                        browser = None
                    continue

                try:
                    if browser is None:
                        log.info("browser starten")
                        browser = pw.chromium.launch(
                            executable_path=CHROMIUM_PATH,
                            args=["--no-sandbox", "--disable-dev-shm-usage",
                                  "--disable-gpu"],
                        )
                    antwoord.put(self._haal_op(browser, url, timeout_ms))
                except Exception as e:
                    log.exception("ophalen mislukte voor %s", url)
                    # Browser kan half stuk zijn; volgende keer opnieuw starten.
                    try:
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass
                    browser = None
                    antwoord.put({"error": f"{e.__class__.__name__}: {e}"})

    def _haal_op(self, browser, url: str, timeout_ms: int) -> dict:
        context = browser.new_context(
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"},
        )
        try:
            page = context.new_page()
            antwoord = page.goto(url, wait_until="domcontentloaded",
                                 timeout=timeout_ms)
            status = antwoord.status if antwoord else 0

            self._weg_met_de_cookiemelding(page)

            # Het schema komt soms pas na het uitvoeren van scripts.
            page.wait_for_timeout(SETTLE_MS)
            try:
                page.wait_for_selector('script[type="application/ld+json"]',
                                       timeout=3000, state="attached")
            except Exception:
                pass  # niet elke site heeft het; de brug beoordeelt dat zelf

            return {
                "html": page.content(),
                "final_url": page.url,
                "status": status,
                "titel": page.title(),
            }
        finally:
            context.close()

    def _weg_met_de_cookiemelding(self, page):
        for tekst in CONSENT_TEKSTEN:
            try:
                knop = page.get_by_role("button", name=tekst, exact=False)
                if knop.count() and knop.first.is_visible():
                    knop.first.click(timeout=2000)
                    log.info("cookiemelding weggeklikt via %r", tekst)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue


def create_app() -> Flask:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    app = Flask(__name__)
    renderer = Renderer()
    app.renderer = renderer

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": renderer.alive}), (200 if renderer.alive else 503)

    @app.post("/render")
    def render():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not (body.get("url") or "").strip():
            return jsonify({"error": "geef een url mee"}), 400

        url = body["url"].strip()
        if not url.lower().startswith(("http://", "https://")):
            return jsonify({"error": "alleen http en https"}), 400

        timeout_ms = int(body.get("timeout_ms") or NAV_TIMEOUT_MS)
        log.info("ophalen: %s", url)
        uitkomst = renderer.render(url, timeout_ms)
        if "error" in uitkomst:
            return jsonify(uitkomst), 502
        log.info("klaar: %s (%d KB, status %s)", url,
                 len(uitkomst["html"]) // 1024, uitkomst["status"])
        return jsonify(uitkomst), 200

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8096")))
