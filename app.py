"""
De webkant van de brug: één pagina en een paar JSON-endpoints.

De pagina is bedoeld voor mijn telefoon: een bericht uit de groep "Recepten
dump" erin plakken, kijken wat er gevonden is, en importeren. De endpoints
zijn er ook voor Home Assistant en voor HTTP Shortcuts op Android, zodat
"Delen" naar de brug kan.
"""

import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from importqueue import ImportQueue, Store
from tandoor import TandoorClient, TandoorError
from urls import extract_urls

log = logging.getLogger("tandoor_bridge")


def configureer_logging() -> None:
    """
    Zet het logboek op stdout, waar het ook vandaan gestart wordt.

    Dit stond eerst alleen in main(), en gunicorn roept main() nooit aan - dat
    start create_app() rechtstreeks. Gevolg: in de container bleef de
    root-logger op WARNING staan en stond er geen enkele importregel in de
    Docker-logs, terwijl dat juist de plek is waar je kijkt wat de brug doet.
    """
    niveau = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    wortel = logging.getLogger()
    if wortel.handlers:
        wortel.setLevel(niveau)
        return
    logging.basicConfig(
        level=niveau,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,   # zodat het in de Docker-logs van de NAS staat
    )


def _env_getal(naam: str, default, soort=float):
    """
    Een getal uit de omgeving, met de standaard als het geen getal is.

    Een typefout in IMPORT_DELAY_SECONDS liet de container anders in een
    herstartlus lopen met een kale ValueError, en dat is een vervelende fout
    om op een NAS terug te vinden.
    """
    ruw = os.environ.get(naam, "").strip()
    if not ruw:
        return default
    try:
        return soort(ruw)
    except ValueError:
        log.warning("%s is geen getal (%r); %s wordt gebruikt", naam, ruw, default)
        return default


def _env_bool(naam: str, default: bool = False) -> bool:
    waarde = os.environ.get(naam)
    if waarde is None:
        return default
    return waarde.strip().lower() in ("1", "true", "yes", "ja", "on")


def config_from_env() -> dict:
    return {
        "tandoor_url": os.environ.get("TANDOOR_URL", "").strip(),
        "tandoor_token": os.environ.get("TANDOOR_TOKEN", "").strip(),
        "public_url": os.environ.get("TANDOOR_PUBLIC_URL", "").strip() or None,
        "data_dir": os.environ.get("DATA_DIR", "/data"),
        "delay": max(0.0, _env_getal("IMPORT_DELAY_SECONDS", 5.0, float)),
        "port": _env_getal("PORT", 8095, int),
        "send_properties": _env_bool("SEND_PROPERTIES", False),
    }


def _schrijfbare_datamap(pad: str) -> Path:
    """
    Controleer de datamap voordat er iets anders gebeurt.

    Dit is de fout die bij het inrichten als eerste langskomt: de container
    draait niet als root, en een gekoppelde map van de NAS is vaak van iemand
    anders. Zonder deze controle krijg je een PermissionError ergens diep in
    een gunicorn-traceback met "Worker failed to boot" eronder, en daar staat
    niet in wat je eraan moet doen.
    """
    map_pad = Path(pad)
    try:
        map_pad.mkdir(parents=True, exist_ok=True)
        proef = map_pad / ".schrijftest"
        proef.touch()
        proef.unlink()
    except OSError as e:
        raise RuntimeError(
            f"kan niet schrijven in {map_pad} (deze container draait als uid "
            f"{os.getuid()}, gid {os.getgid()}): {e.strerror}. "
            f"Geef die map eigendom aan deze gebruiker "
            f"(chown -R {os.getuid()}:{os.getgid()} <pad op de host>), of laat de "
            f"container als de eigenaar van die map draaien met user: \"<uid>:<gid>\" "
            f"in docker-compose.yml, of gebruik een Docker-volume in plaats van een "
            f"pad op de host."
        ) from e
    return map_pad


def create_app(*, store=None, client=None, queue=None, start_worker=True,
               config=None) -> Flask:
    """
    Zet de app in elkaar.

    De onderdelen zijn allemaal van buiten in te geven, zodat de tests de app
    tegen een eigen database en een nagebouwde Tandoor kunnen zetten zonder
    omgevingsvariabelen.
    """
    configureer_logging()
    instellingen = config or config_from_env()

    if client is None:
        client = TandoorClient(
            instellingen["tandoor_url"], instellingen["tandoor_token"],
            public_url=instellingen["public_url"],
            send_properties=instellingen["send_properties"],
        )
    if store is None:
        store = Store(_schrijfbare_datamap(instellingen["data_dir"]) / "bridge.sqlite3")
    if queue is None:
        queue = ImportQueue(store, client, delay=instellingen["delay"])

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024   # een lijst URL's is klein
    app.store, app.client, app.queue = store, client, queue
    app.bridge_config = instellingen

    if start_worker:
        queue.start()

    # ------------------------------------------------------------- helpers

    def verrijk(item: dict) -> dict:
        """Voeg de aanklikbare Tandoor-link toe."""
        item = dict(item)
        if item.get("recipe_id"):
            item["tandoor_url"] = client.recipe_url(item["recipe_id"])
        return item

    def tekst_uit_verzoek() -> str:
        """
        Haal de te importeren tekst uit het verzoek.

        Met opzet ruim: de webpagina stuurt JSON met 'text', HTTP Shortcuts
        stuurt liever {"url": ...}, en Home Assistant stuurt soms gewoon een
        formulierveld.
        """
        # get_json geeft ook een lijst of een losse string terug als de body
        # dat was; .get() daarop is een AttributeError en dus een 500.
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        stukken = []
        for sleutel in ("text", "url", "urls"):
            waarde = body.get(sleutel)
            if isinstance(waarde, str):
                stukken.append(waarde)
            elif isinstance(waarde, (list, tuple)):
                stukken += [str(w) for w in waarde]
        for sleutel in ("text", "url"):
            if waarde := request.form.get(sleutel):
                stukken.append(waarde)
        if not stukken and (waarde := request.args.get("url")):
            stukken.append(waarde)
        return "\n".join(stukken)

    # ------------------------------------------------------------- pagina

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "worker": queue.running}), 200

    # ------------------------------------------------------------- API

    @app.post("/api/preview")
    def preview():
        """Wat gaat er gebeuren als ik nu op importeren druk?"""
        tekst = tekst_uit_verzoek()
        gevonden = store.classify(extract_urls(tekst))
        for regel in gevonden:
            if regel.get("recipe_id"):
                regel["tandoor_url"] = client.recipe_url(regel["recipe_id"])
        return jsonify({"urls": gevonden, "gevonden": len(gevonden)}), 200

    @app.post("/api/import")
    def importeer():
        tekst = tekst_uit_verzoek()
        if not tekst.strip():
            return jsonify({"error": "geen tekst of URL meegestuurd"}), 400
        result = store.add_text(tekst, source=request.args.get("source", "web"))
        if not result.added and not result.skipped:
            return jsonify({"error": "geen enkele link in deze tekst gevonden",
                            **result.as_dict()}), 400
        log.info("%d toegevoegd, %d overgeslagen", len(result.added), len(result.skipped))
        return jsonify(result.as_dict()), 202

    @app.post("/api/upload")
    def upload():
        """Een .txt met URL's, één per regel."""
        bestand = request.files.get("file")
        if bestand is None:
            return jsonify({"error": "geen bestand meegestuurd"}), 400
        try:
            tekst = bestand.read().decode("utf-8", "replace")
        except Exception:
            return jsonify({"error": "kon het bestand niet lezen"}), 400
        result = store.add_text(tekst, source="bestand")
        return jsonify(result.as_dict()), 202

    @app.get("/api/items")
    def items():
        status = request.args.get("status") or None
        try:
            limiet = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            limiet = 100
        limiet = max(1, min(limiet, 1000))   # een negatieve limiet haalde de grens eruit
        return jsonify({
            "items": [verrijk(i) for i in store.items(status=status, limit=limiet)],
            "counts": store.counts(),
        }), 200

    @app.post("/api/items/<int:item_id>/retry")
    def retry(item_id):
        if not store.retry(item_id):
            return jsonify({"error": "dit item staat niet op mislukt"}), 409
        return jsonify({"ok": True}), 200

    @app.post("/api/retry-failed")
    def retry_failed():
        return jsonify({"opnieuw": store.retry_all_failed()}), 200

    @app.get("/api/status")
    def status():
        return jsonify({
            "counts": store.counts(),
            "worker": queue.running,
            "delay": queue.delay,
            "tandoor": client.public_url,
        }), 200

    @app.get("/api/diagnose")
    def diagnose():
        """
        Haal één pagina op en zeg wat erin zit, zonder Tandoor erbij.

        Dit is de knop voor "waarom lukt deze site niet": je ziet of de
        aanvraag doorkwam, via welke poging, en of er een recept-schema in de
        pagina staat.
        """
        from fetcher import FetchError, describe_page, fetch_page
        from urls import normalize_url

        url = normalize_url(request.args.get("url", ""))
        if not url:
            return jsonify({"error": "geef een url mee: /api/diagnose?url=..."}), 400
        try:
            pagina = fetch_page(url)
        except FetchError as e:
            return jsonify({"url": url, "ok": False, "reden": e.reason,
                            "status": e.status}), 200
        beschrijving = describe_page(pagina.html)
        return jsonify({
            "url": url,
            "ok": True,
            "via": pagina.via,
            "status": pagina.status,
            "final_url": pagina.final_url,
            "pagina": beschrijving,
            "verwachting": ("Tandoor kan dit ontleden" if beschrijving["heeft_schema"]
                            else "Tandoor vindt hier geen recept"),
            "begin": pagina.html[:600],
        }), 200

    @app.get("/api/check")
    def check():
        """Werkt de verbinding met Tandoor? Voor de rooktest en de startpagina."""
        try:
            return jsonify({"ok": True, "recepten": client.check_connection()}), 200
        except TandoorError as e:
            return jsonify({"ok": False, "error": e.message}), 502

    @app.errorhandler(404)
    def niet_gevonden(_):
        return jsonify({"error": "niet gevonden"}), 404

    @app.errorhandler(500)
    def serverfout(e):
        log.exception("onverwachte fout in een verzoek")
        return jsonify({"error": "onverwachte fout, zie de logboekregels"}), 500

    return app


def main():
    configureer_logging()
    instellingen = config_from_env()
    ontbreekt = [naam for naam, sleutel in
                 (("TANDOOR_URL", "tandoor_url"), ("TANDOOR_TOKEN", "tandoor_token"))
                 if not instellingen[sleutel]]
    if ontbreekt:
        print(f"Zet eerst {' en '.join(ontbreekt)} in je .env", file=sys.stderr)
        raise SystemExit(2)

    app = create_app(config=instellingen)

    # Niet afbreken als Tandoor even niet luistert: de wachtrij blijft staan en
    # pakt het later op. Wel meteen in het logboek, want dit is bij het
    # inrichten de fout die je wilt zien.
    try:
        aantal = app.client.check_connection()
        log.info("verbonden met Tandoor (%d recepten)", aantal)
    except TandoorError as e:
        log.warning("Tandoor antwoordt nog niet: %s", e.message)

    log.info("brug draait op poort %s, pauze van %ss tussen imports",
             instellingen["port"], instellingen["delay"])
    app.run(host="0.0.0.0", port=instellingen["port"])


if __name__ == "__main__":
    main()
