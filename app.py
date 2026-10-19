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
        "delay": float(os.environ.get("IMPORT_DELAY_SECONDS", "5")),
        "port": int(os.environ.get("PORT", "8095")),
        "send_properties": _env_bool("SEND_PROPERTIES", False),
    }


def create_app(*, store=None, client=None, queue=None, start_worker=True,
               config=None) -> Flask:
    """
    Zet de app in elkaar.

    De onderdelen zijn allemaal van buiten in te geven, zodat de tests de app
    tegen een eigen database en een nagebouwde Tandoor kunnen zetten zonder
    omgevingsvariabelen.
    """
    instellingen = config or config_from_env()

    if client is None:
        client = TandoorClient(
            instellingen["tandoor_url"], instellingen["tandoor_token"],
            public_url=instellingen["public_url"],
            send_properties=instellingen["send_properties"],
        )
    if store is None:
        map_pad = Path(instellingen["data_dir"])
        map_pad.mkdir(parents=True, exist_ok=True)
        store = Store(map_pad / "bridge.sqlite3")
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
        body = request.get_json(silent=True) or {}
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
        limiet = min(int(request.args.get("limit", 100)), 1000)
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
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,   # zodat het in de Docker-logs van UGOS staat
    )
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
