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
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from markupsafe import escape

from importqueue import ImportQueue, Store
from tandoor import TandoorClient, TandoorError
from urls import extract_urls, normalize_url

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


def _bookmarklet_pagina(kop: str, tekst: str, link: str | None) -> str:
    """Een klein antwoordpaginaatje; het opent in een nieuw tabblad."""
    knop = (f'<p><a href="{escape(link)}">Bekijk in Tandoor</a></p>' if link else "")
    return (
        "<!DOCTYPE html><html lang=nl><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Tandoor Bridge</title><style>"
        "body{font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "sans-serif;margin:0;padding:32px;color-scheme:light dark}"
        "h1{font-size:20px;margin:0 0 8px}p{margin:4px 0}"
        "</style></head><body>"
        f"<h1>{escape(kop)}</h1><p>{escape(tekst)}</p>{knop}"
        "<p><small>Dit tabblad kun je sluiten.</small></p>"
        "</body></html>"
    )


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
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    # Werkzeug kapt formuliervelden standaard op 500 KB af, en de bookmarklet
    # stuurt een hele receptpagina mee. Zonder dit krijg je een 413 zonder
    # uitleg zodra een pagina iets groter is dan gemiddeld.
    app.config["MAX_FORM_MEMORY_SIZE"] = 16 * 1024 * 1024
    app.store, app.client, app.queue = store, client, queue
    app.bridge_config = instellingen

    if start_worker:
        queue.start()
        # Hier en niet in main(): gunicorn roept main() nooit aan, dus daar
        # kwamen deze regels in de container nooit in het logboek.
        _meld_start(client, instellingen)

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

    @app.post("/bookmarklet")
    def bookmarklet():
        """
        HTML uit je eigen browser, via een formulier-submit.

        Een formulier en geen fetch, en dat is geen smaakkwestie: vanaf een
        https-pagina mag een fetch naar http://<nas> niet van de browser
        (mixed content), terwijl een formulier-submit navigatie is en wel mag.
        Zo doet Tandoors eigen bookmarklet het ook.
        """
        url = (request.form.get("url") or "").strip()
        html = request.form.get("html") or ""
        log.info("bookmarklet: %s (%d KB HTML)", url, len(html) // 1024)

        try:
            uitkomst = queue.process_html(url, html, source="bookmarklet")
        except Exception:
            log.exception("bookmarklet mislukte voor %s", url)
            return _bookmarklet_pagina(
                "Onverwachte fout", "Zie de logboekregels van de brug.", None), 500

        status = uitkomst.get("status")
        recipe_id = uitkomst.get("recipe_id")
        link = client.recipe_url(recipe_id) if recipe_id else None

        if status == "klaar":
            kop, tekst = "Toegevoegd", uitkomst.get("titel") or url
            if uitkomst.get("note"):
                tekst += f" — {uitkomst['note']}"
        elif status == "bestaat_al":
            kop = "Stond er al"
            tekst = uitkomst.get("titel") or uitkomst.get("error") or url
        else:
            kop = "Niet gelukt"
            tekst = uitkomst.get("error") or "onbekende reden"

        return _bookmarklet_pagina(kop, tekst, link), 200

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
        from ah_api import AH_API_AAN
        from fetcher import RENDERER_URL, renderer_diagnose, renderer_health

        return jsonify({
            "counts": store.counts(),
            "worker": queue.running,
            "delay": queue.delay,
            "tandoor": client.public_url,
            # De AH-route: zie /api/ah?url=... om te zien of hij ook werkt.
            "ah_api": AH_API_AAN,
            # Ingesteld zijn en antwoorden zijn twee verschillende dingen, en
            # dat verschil is precies waar je naar zoekt als AH niet lukt.
            "browser": RENDERER_URL or None,
            "browser_bereikbaar": renderer_health(RENDERER_URL),
            # En als hij niet bereikbaar is: waarom niet.
            "browser_diagnose": renderer_diagnose(RENDERER_URL),
        }), 200

    @app.get("/api/diagnose")
    def diagnose():
        """
        Probeer één pagina op alle manieren en zeg wat eruit kwam.

        Dit is de knop voor "waarom lukt deze site niet". Tandoor komt er niet
        aan te pas, dus het kost ook geen plaats in zijn importlimiet. Je ziet
        per poging of de aanvraag doorkwam en of er een recept-schema in de
        pagina staat - dat laatste is wat Tandoor nodig heeft.
        """
        from fetcher import probe

        url = normalize_url(request.args.get("url", ""))
        if not url:
            return jsonify({"error": "geef een url mee: /api/diagnose?url=..."}), 400

        pogingen = probe(url)
        met_schema = [p for p in pogingen
                      if p.get("ok") and (p.get("pagina") or {}).get("heeft_schema")]
        binnengekomen = [p for p in pogingen if p.get("ok")]

        if met_schema:
            conclusie = (f"te importeren: '{met_schema[0]['poging']}' levert een "
                         f"pagina met een recept-schema op")
        elif binnengekomen:
            pagina = binnengekomen[0].get("pagina") or {}
            conclusie = (f"de pagina komt binnen maar bevat geen recept-schema "
                         f"(titel: \"{pagina.get('titel') or '(geen)'}\"). "
                         f"Tandoor kan hier niets mee.")
        else:
            conclusie = "geen enkele poging kwam door; de site weigert deze machine"

        return jsonify({"url": url, "conclusie": conclusie,
                        "pogingen": pogingen}), 200

    @app.get("/api/ah")
    def ah():
        """
        Vraag één Albert Heijn-recept bij de app-API op en laat zien wat er komt.

        Tandoor komt er niet aan te pas: dit is de knop om te kijken of de
        AH-route werkt, en om te zien welke ingrediëntregels Tandoor straks te
        ontleden krijgt.
        """
        import ah_api

        url = normalize_url(request.args.get("url", ""))
        nummer = ah_api.recipe_id_from_url(url)
        if nummer is None:
            return jsonify({"error": "geef een AH-receptlink mee: "
                                     "/api/ah?url=https://www.ah.nl/allerhande/recept/R-R..."}), 400
        if not ah_api.AH_API_AAN:
            return jsonify({"url": url, "ok": False,
                            "reden": "de AH-route staat uit (AH_API=0)"}), 200
        try:
            recept = ah_api.fetch_recipe(nummer)
            schema = ah_api.to_jsonld(recept, url)
        except ah_api.AhError as e:
            return jsonify({"url": url, "ok": False, "recept_id": nummer,
                            "reden": e.reason, "status": e.status}), 200
        except Exception as e:
            # Dit is de knop waarmee je uitzoekt wát er misgaat; een kale 500
            # zegt dan niets.
            log.exception("onverwachte fout in /api/ah voor %s", url)
            return jsonify({"url": url, "ok": False, "recept_id": nummer,
                            "reden": f"onverwachte fout: {e.__class__.__name__}: {e}"}), 200

        return jsonify({
            "url": url, "ok": True, "recept_id": nummer,
            "titel": schema.get("name"),
            "porties": schema.get("recipeYield"),
            # prepTime wordt in Tandoor de bereidingstijd, cookTime de wachttijd.
            "bereidingstijd": schema.get("prepTime"),
            "wachttijd": schema.get("cookTime"),
            "foto": (schema.get("image") or [None])[0],
            # Dit is de tekst die Tandoor straks ontleedt. Staat de eenheid er
            # niet in, dan valt er voor Unit Alias ook niets te doen.
            "ingredienten": schema.get("recipeIngredient"),
            "stappen": [s["text"] for s in schema.get("recipeInstructions", [])],
            "velden_die_ah_gaf": sorted(recept.keys()),
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
    app.run(host="0.0.0.0", port=instellingen["port"])


def _meld_start(client, instellingen: dict) -> None:
    """Zet in het logboek hoe de brug is ingericht, voor wie de logs leest."""
    import ah_api
    from fetcher import RENDERER_URL

    log.info("brug draait op poort %s, pauze van %ss tussen imports",
             instellingen["port"], instellingen["delay"])
    if ah_api.AH_API_AAN:
        log.info("Albert Heijn gaat via de app-API (%s)", ah_api.API_URL)
    else:
        log.info("de AH-route staat uit (AH_API=0); AH-recepten kunnen dan "
                 "alleen via de bookmarklet")
    if RENDERER_URL:
        log.info("browser-container ingesteld op %s", RENDERER_URL)
    else:
        log.info("geen browser-container (RENDERER_URL is leeg); sites die hun "
                 "recept pas met JavaScript opbouwen lukken dan niet")

    # In een eigen thread: een Tandoor die niet antwoordt mag de start niet
    # ophouden - de wachtrij wacht wel. Maar het hoort wel meteen in het
    # logboek, want dit is bij het inrichten de fout die je wilt zien.
    def controleer():
        try:
            log.info("verbonden met Tandoor (%d recepten)", client.check_connection())
        except TandoorError as e:
            log.warning("Tandoor antwoordt nog niet: %s", e.message)
        except Exception:
            log.exception("verbinding met Tandoor niet te controleren")

    threading.Thread(target=controleer, name="startcontrole", daemon=True).start()


if __name__ == "__main__":
    main()
