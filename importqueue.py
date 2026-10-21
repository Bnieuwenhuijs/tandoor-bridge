"""
De wachtrij: één recept tegelijk, en een fout stopt de rest nooit.

Dat laatste is precies wat de lijst-import van Tandoor zelf verkeerd doet.
Hier krijgt elke URL een eigen regel in SQLite met een eigen status, en die
overleeft een herstart van de container: wat tijdens het afsluiten onderweg
was, gaat bij de start terug naar "wachtend".

Over de bestandsnaam: in het plan heette dit queue.py, maar een queue.py in
de projectmap schaduwt de queue uit de standaardbibliotheek. urllib3 haalt
daar LifoQueue uit, dus met queue.py ernaast importeert requests niet eens
meer - Python stelt zelf voor het bestand te hernoemen. Vandaar deze naam.
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

from fetcher import FetchError, FetchResult, fetch_image, fetch_page
from tandoor import (
    NoRecipeSchema,
    TandoorError,
    TandoorNoRecipe,
    TandoorThrottled,
    TandoorUnreachable,
    recipe_payload,
)
import wachttijd
from urls import dedupe_key, extract_urls, normalize_url

log = logging.getLogger(__name__)

# De statussen zoals ze in de interface staan.
WACHTEND = "wachtend"
OPHALEN = "ophalen"
NAAR_TANDOOR = "naar_tandoor"
KLAAR = "klaar"
MISLUKT = "mislukt"
BESTAAT_AL = "bestaat_al"

# Onderweg toen het proces stopte: die gaan bij de start terug in de rij.
ONDERWEG = (OPHALEN, NAAR_TANDOOR)
# Afgehandeld, en dus bruikbaar om een dubbele te herkennen.
AFGEROND = (KLAAR, BESTAAT_AL)

# Een limiet die zo vaak terugkomt is geen limiet meer maar een probleem.
MAX_LIMIET_POGINGEN = 50

# Tandoor die niet antwoordt is meestal tijdelijk: een herstart na een update
# duurt seconden. Maar niet eindeloos blijven proberen.
MAX_ONBEREIKBAAR = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT    NOT NULL,
    dedupe_key  TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    title       TEXT,
    recipe_id   INTEGER,
    error       TEXT,
    note        TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_key ON items(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AddResult:
    """Wat er van een geplakt bericht in de rij terechtkwam."""
    found: int = 0
    added: list = field(default_factory=list)      # item-ids
    skipped: list = field(default_factory=list)     # [{"url":..., "reason":...}]

    def as_dict(self):
        return {"gevonden": self.found, "toegevoegd": len(self.added),
                "ids": self.added, "overgeslagen": self.skipped}


class Store:
    """
    SQLite-opslag voor de wachtrij en de geschiedenis.

    Eén verbinding per aanroep: de worker-thread en de webverzoeken komen
    hier door elkaar langs, en een verbinding delen tussen threads is met
    sqlite3 vragen om moeilijkheden. Openen kost microseconden.
    """

    def __init__(self, path):
        self.path = str(path)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @staticmethod
    def _prepare(urls) -> list:
        """Opschonen en binnen de lijst ontdubbelen, met de volgorde intact."""
        gezien, schoon = set(), []
        for url in urls:
            genormaliseerd = normalize_url(url or "")
            if not genormaliseerd:
                continue
            key = dedupe_key(genormaliseerd)
            if key in gezien:
                continue
            gezien.add(key)
            schoon.append(genormaliseerd)
        return schoon

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=15000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # -- lezen ------------------------------------------------------------

    def classify(self, urls) -> list:
        """
        Per URL: is dit nieuw, staat het al in Tandoor, of al in de rij?

        Dit is wat de voorbeeldlijst in de interface laat zien, zodat ik vóór
        het importeren zie wat er gaat gebeuren.

        De URL's worden hier eerst genormaliseerd, en niet alleen in
        extract_urls(). Anders komt een losse AH-deellink via POST /api/import
        er als een ander recept langs dan dezelfde link uit een geplakt
        bericht - en dat is precies het geval waarvoor de dubbelcontrole
        bestaat. Normaliseren is bewust herhaalbaar, dus dubbel doen kan geen
        kwaad.
        """
        resultaat = []
        with self._connect() as db:
            for url in self._prepare(urls):
                key = dedupe_key(url)
                rij = db.execute(
                    "SELECT id, status, title, recipe_id FROM items "
                    "WHERE dedupe_key = ? AND status IN (?, ?) "
                    "ORDER BY id DESC LIMIT 1", (key, KLAAR, BESTAAT_AL)).fetchone()
                if rij:
                    resultaat.append({"url": url, "state": "al_geimporteerd",
                                      "item_id": rij["id"], "title": rij["title"],
                                      "recipe_id": rij["recipe_id"]})
                    continue
                rij = db.execute(
                    "SELECT id, status FROM items WHERE dedupe_key = ? "
                    "AND status IN (?, ?, ?) ORDER BY id DESC LIMIT 1",
                    (key, WACHTEND, OPHALEN, NAAR_TANDOOR)).fetchone()
                if rij:
                    resultaat.append({"url": url, "state": "in_wachtrij",
                                      "item_id": rij["id"]})
                    continue
                rij = db.execute(
                    "SELECT id, error FROM items WHERE dedupe_key = ? AND status = ? "
                    "ORDER BY id DESC LIMIT 1", (key, MISLUKT)).fetchone()
                if rij:
                    resultaat.append({"url": url, "state": "eerder_mislukt",
                                      "item_id": rij["id"], "error": rij["error"]})
                    continue
                resultaat.append({"url": url, "state": "nieuw"})
        return resultaat

    def items(self, *, status=None, limit=200, offset=0) -> list:
        vraag = "SELECT * FROM items"
        parameters = []
        if status:
            vraag += " WHERE status = ?"
            parameters.append(status)
        vraag += " ORDER BY id DESC LIMIT ? OFFSET ?"
        parameters += [int(limit), int(offset)]
        with self._connect() as db:
            return [dict(r) for r in db.execute(vraag, parameters).fetchall()]

    def item(self, item_id) -> dict | None:
        with self._connect() as db:
            rij = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            return dict(rij) if rij else None

    def counts(self) -> dict:
        with self._connect() as db:
            rijen = db.execute(
                "SELECT status, COUNT(*) AS aantal FROM items GROUP BY status").fetchall()
        return {r["status"]: r["aantal"] for r in rijen}

    # -- schrijven --------------------------------------------------------

    def add(self, urls, *, source="web") -> AddResult:
        """Zet URL's in de rij, en sla over wat er al is."""
        beoordelingen = self.classify(urls)
        result = AddResult(found=len(beoordelingen))
        for beoordeling in beoordelingen:
            url = beoordeling["url"]
            if beoordeling["state"] == "al_geimporteerd":
                result.skipped.append({"url": url, "reason": "bestaat al",
                                       "recipe_id": beoordeling.get("recipe_id")})
                continue
            if beoordeling["state"] == "in_wachtrij":
                result.skipped.append({"url": url, "reason": "staat al in de wachtrij"})
                continue
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO items (url, dedupe_key, status, source, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (url, dedupe_key(url), WACHTEND, source, _now(), _now()))
                result.added.append(cursor.lastrowid)
        return result

    def add_claimed(self, url, *, source="bookmarklet") -> int | None:
        """
        Voeg één URL toe die meteen verwerkt wordt, al geclaimd.

        Voor de bookmarklet: die levert de HTML zelf aan en wordt direct
        afgehandeld. Hem eerst als "wachtend" invoegen zou een race met de
        worker opleveren - die kan hem dan oppakken en opnieuw gaan ophalen.

        Geeft None als deze URL al geïmporteerd is of al in de rij staat.
        """
        beoordeling = self.classify([url])
        if not beoordeling:
            return None
        oordeel = beoordeling[0]
        if oordeel["state"] in ("al_geimporteerd", "in_wachtrij"):
            return None
        schoon = oordeel["url"]
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO items (url, dedupe_key, status, attempts, source, "
                "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
                (schoon, dedupe_key(schoon), NAAR_TANDOOR, source, _now(), _now()))
            return cursor.lastrowid

    def add_text(self, text, *, source="web") -> AddResult:
        return self.add(extract_urls(text), source=source)

    def update(self, item_id, **velden):
        if not velden:
            return
        velden["updated_at"] = _now()
        toewijzing = ", ".join(f"{k} = ?" for k in velden)
        with self._connect() as db:
            db.execute(f"UPDATE items SET {toewijzing} WHERE id = ?",
                       [*velden.values(), item_id])

    def claim_next(self) -> dict | None:
        """Pak het oudste wachtende item en zet het op 'ophalen'."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rij = db.execute(
                "SELECT * FROM items WHERE status = ? ORDER BY id LIMIT 1",
                (WACHTEND,)).fetchone()
            if not rij:
                return None
            db.execute("UPDATE items SET status = ?, attempts = attempts + 1, "
                       "error = NULL, updated_at = ? WHERE id = ?",
                       (OPHALEN, _now(), rij["id"]))
            return dict(rij)

    def requeue_stale(self) -> int:
        """
        Wat onderweg was toen het proces stopte, gaat terug in de rij.

        Zonder dit blijft een item na een herstart van de container voor
        altijd op "ophalen" staan.
        """
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE status IN (?, ?)",
                (WACHTEND, _now(), *ONDERWEG))
            return cursor.rowcount

    def retry(self, item_id) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE items SET status = ?, error = NULL, note = NULL, "
                "attempts = 0, updated_at = ? WHERE id = ? AND status = ?",
                (WACHTEND, _now(), item_id, MISLUKT))
            return cursor.rowcount > 0

    def retry_all_failed(self) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE items SET status = ?, error = NULL, note = NULL, "
                "attempts = 0, updated_at = ? WHERE status = ?",
                (WACHTEND, _now(), MISLUKT))
            return cursor.rowcount

    def find_imported(self, key, *, exclude_id=None) -> dict | None:
        """Een eerder geïmporteerd recept met dezelfde sleutel."""
        with self._connect() as db:
            rij = db.execute(
                "SELECT * FROM items WHERE dedupe_key = ? AND status IN (?, ?) "
                "AND id != ? ORDER BY id LIMIT 1",
                (key, KLAAR, BESTAAT_AL, exclude_id or -1)).fetchone()
            return dict(rij) if rij else None


class ImportQueue:
    """
    De worker. Verwerkt één item per keer, met een pauze ertussen.

    De pauze is er voor Tandoor: alleen de importroute is afgeknepen
    (standaard 60 per uur), en de brug hoort een nette gast te zijn. Komt er
    tóch een 429, dan wacht hij de Retry-After af en gaat daarna door.
    """

    def __init__(self, store: Store, client, *, delay=5.0,
                 fetch=fetch_page, fetch_photo=fetch_image, impersonate=True,
                 sorteerder=None):
        self.store = store
        self.client = client
        # Tandoors AI-sortering voor nieuwe recepten; None is uit.
        self.sorteerder = sorteerder
        self.delay = float(delay)
        self._fetch = fetch
        self._fetch_photo = fetch_photo
        self._impersonate = impersonate
        self._stop = threading.Event()
        self._thread = None

    # -- draaien ----------------------------------------------------------

    def start(self):
        hersteld = self.store.requeue_stale()
        if hersteld:
            log.info("%d item(s) stonden nog onderweg en gaan terug in de rij", hersteld)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="importqueue", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        while not self._stop.is_set():
            try:
                uitkomst = self.process_once()
            except Exception:
                # Niets mag deze thread doodmaken: dan staat de hele brug stil.
                log.exception("onverwachte fout in de wachtrij")
                self._stop.wait(5.0)
                continue
            if uitkomst is None:
                self._stop.wait(2.0)            # niets te doen
            else:
                self._stop.wait(uitkomst.get("wait", self.delay))

    # -- één item ---------------------------------------------------------

    def process_once(self) -> dict | None:
        """
        Verwerk het eerstvolgende item. None als er niets te doen was.

        Geeft terug welke status het item kreeg en hoe lang de worker daarna
        moet wachten, zodat de tests dit stap voor stap kunnen aanroepen.
        """
        item = self.store.claim_next()
        if not item:
            return None
        try:
            return self._process(item)
        except Exception as e:
            # Het item staat op dit moment op 'ophalen' of 'naar_tandoor'.
            # requeue_stale() draait alleen bij de start, dus zonder deze
            # vangst blijft het daar staan tot de container herstart - en
            # verdwijnt het uit de rij zonder dat iemand het ziet.
            self.store.update(item["id"], status=MISLUKT,
                              error=f"onverwachte fout: {e.__class__.__name__}: {e}")
            raise

    def _process(self, item: dict) -> dict:
        item_id, url = item["id"], item["url"]
        log.info("[%s] %s", item_id, url)

        # 1. Hebben we dit al eens binnengehaald?
        eerder = self.store.find_imported(item["dedupe_key"], exclude_id=item_id)
        if eerder:
            self.store.update(item_id, status=BESTAAT_AL, title=eerder["title"],
                              recipe_id=eerder["recipe_id"],
                              note="al eerder geïmporteerd via de brug")
            return {"status": BESTAAT_AL, "wait": 0}

        # 2. Pagina ophalen - de reden dat de brug bestaat.
        try:
            pagina = self._fetch(url, impersonate=self._impersonate)
        except FetchError as e:
            self.store.update(item_id, status=MISLUKT, error=e.reason)
            return {"status": MISLUKT}

        self.store.update(item_id, status=NAAR_TANDOOR)
        return self._naar_tandoor(item, pagina)

    def _naar_tandoor(self, item: dict, pagina) -> dict:
        """
        De helft na het ophalen: laten ontleden, aanmaken, foto erbij.

        Apart van _process() omdat de bookmarklet de HTML zelf aanlevert en
        dus bij deze stap begint.
        """
        item_id, url = item["id"], item["url"]

        # 3. Tandoor laten ontleden.
        try:
            bron = self.client.recipe_from_source(url, pagina.html)
        except TandoorThrottled as e:
            return self._handle_throttle(item, e)
        except TandoorUnreachable as e:
            return self._handle_unreachable(item, e)
        except NoRecipeSchema as e:
            # Onze eigen voorcontrole; Tandoor heeft deze pagina nooit gezien.
            self.store.update(item_id, status=MISLUKT, error=e.message)
            return {"status": MISLUKT}
        except TandoorNoRecipe as e:
            self.store.update(item_id, status=MISLUKT,
                              error=f"Tandoor vond geen recept: {e.message}")
            return {"status": MISLUKT}
        except TandoorError as e:
            self.store.update(item_id, status=MISLUKT, error=e.message)
            return {"status": MISLUKT}

        # 4. Tandoor kent dit recept al van een eerdere import buiten de brug om.
        if bron.duplicate_ids:
            eerste = bron.duplicates[0]
            self.store.update(item_id, status=BESTAAT_AL, title=eerste.get("name"),
                              recipe_id=eerste.get("id"),
                              note="stond al in Tandoor")
            return {"status": BESTAAT_AL, "wait": 0}

        # 5. Een wachttijd die op de pagina staat maar die Tandoor liet liggen:
        #    alleen een bereidingstijd en een totale tijd, geen cookTime. Zie
        #    wachttijd.py; in elk ander geval blijft staan wat Tandoor maakte.
        basis = bron.recipe
        if extra := wachttijd.ontbrekende_wachttijd(bron.recipe, pagina.html):
            log.info("[%s] wachttijd %s min uit totale tijd min bereidingstijd",
                     item_id, extra)
            basis = {**bron.recipe, "waiting_time": extra}

        # 6. Sorteren: Tandoors AI verdeelt de ingrediënten over de stappen.
        #    Mislukt dat - een fout, een time-out, de limiet, het tegoed - dan
        #    wordt het recept gewoon ongesorteerd aangemaakt, met de reden erbij.
        notities = []
        te_maken = basis
        if self.sorteerder is not None:
            gesorteerd, reden, bijgesteld = self.sorteerder.sorteer_nieuw(recipe_payload(
                basis, url, send_properties=getattr(self.client, "send_properties", False)))
            if gesorteerd is not None:
                te_maken = gesorteerd
                notities.extend(f"sortering: {n}" for n in bijgesteld)
            elif reden:
                notities.append(f"ongesorteerd: {reden}")

        # 7. Aanmaken.
        try:
            try:
                recept = self.client.create_recipe(te_maken, source_url=url)
            except TandoorError as e:
                # Een 400 op het AI-resultaat betekent dat Tandoor het niet
                # aanneemt; er is dan niets aangemaakt. Dan het recept zoals
                # Tandoor het zelf ontleedde. Een 429 of een onbereikbare
                # Tandoor gaan wel naar de afhandeling hieronder: bij die laatste
                # kan het recept er al staan.
                if te_maken is basis or e.status != 400:
                    raise
                log.warning("[%s] Tandoor weigerde het AI-resultaat: %s", item_id, e.message)
                notities.append(f"ongesorteerd: Tandoor weigerde het AI-resultaat ({e.message})")
                recept = self.client.create_recipe(basis, source_url=url)
        except TandoorThrottled as e:
            return self._handle_throttle(item, e)
        except TandoorUnreachable as e:
            # Let op: het recept kán aangemaakt zijn en alleen het antwoord
            # verloren. Daarom gaat dit item terug in de rij in plaats van op
            # mislukt - bij de volgende poging ziet Tandoors duplicates-lijst
            # het recept en wordt het "bestaat al".
            return self._handle_unreachable(item, e)
        except TandoorError as e:
            self.store.update(item_id, status=MISLUKT, error=e.message)
            return {"status": MISLUKT}

        recipe_id = recept.get("id")
        titel = recept.get("name") or bron.recipe.get("name")

        # 8. Foto. Een recept zonder foto is nog steeds een goed recept, dus
        #    een mislukte foto is een aantekening en geen mislukking.
        if foto := self._attach_photo(recipe_id, bron, pagina):
            notities.append(foto)
        note = "; ".join(notities) or None

        self.store.update(item_id, status=KLAAR, title=titel,
                          recipe_id=recipe_id, note=note, error=None)
        log.info("[%s] klaar: %s (recept %s)", item_id, titel, recipe_id)
        return {"status": KLAAR}

    def process_html(self, url: str, html: str, *, source="bookmarklet") -> dict:
        """
        Verwerk HTML die iemand zelf heeft aangeleverd.

        Dit is de route voor sites die geen enkele server-side aanvraag
        toelaten: jouw browser heeft de cookies en voert het JavaScript uit, en
        stuurt de pagina die daaruit komt hiernaartoe. Alles ná het ophalen is
        hetzelfde als bij een gewone import, dus de dubbelcontrole, de
        bron-URL, de foto en de geschiedenis werken precies zo.
        """
        schoon = normalize_url(url or "")
        if not schoon:
            return {"status": MISLUKT, "error": "geen geldige URL meegestuurd"}
        if not (html or "").strip():
            return {"status": MISLUKT, "error": "geen HTML meegestuurd"}

        eerder = self.store.find_imported(dedupe_key(schoon))
        if eerder:
            return {"status": BESTAAT_AL, "item_id": eerder["id"],
                    "titel": eerder["title"], "recipe_id": eerder["recipe_id"]}

        item_id = self.store.add_claimed(schoon, source=source)
        if item_id is None:
            return {"status": BESTAAT_AL, "error": "staat al in de wachtrij"}

        item = self.store.item(item_id)
        pagina = FetchResult(schoon, schoon, 200, html, source)
        try:
            uitkomst = self._naar_tandoor(item, pagina)
        except Exception as e:
            self.store.update(item_id, status=MISLUKT,
                              error=f"onverwachte fout: {e.__class__.__name__}: {e}")
            raise
        ververst = self.store.item(item_id)
        return {**uitkomst, "item_id": item_id, "titel": ververst["title"],
                "recipe_id": ververst["recipe_id"], "error": ververst["error"],
                "note": ververst["note"]}

    def _handle_throttle(self, item, fout: TandoorThrottled) -> dict:
        """Terug in de rij, en wachten tot de limiet weer ruimte geeft."""
        if item["attempts"] >= MAX_LIMIET_POGINGEN:
            self.store.update(item["id"], status=MISLUKT,
                              error=f"importlimiet bleef aanhouden: {fout.message}")
            return {"status": MISLUKT}
        self.store.update(item["id"], status=WACHTEND,
                          note=f"wacht op de importlimiet van Tandoor "
                               f"({fout.retry_after} s)")
        log.warning("importlimiet bereikt; %s seconden wachten", fout.retry_after)
        return {"status": WACHTEND, "wait": fout.retry_after}

    def _handle_unreachable(self, item, fout: TandoorUnreachable) -> dict:
        """Tandoor antwoordde niet; terug in de rij, want dat is vaak tijdelijk."""
        if item["attempts"] >= MAX_ONBEREIKBAAR:
            self.store.update(item["id"], status=MISLUKT, error=fout.message)
            return {"status": MISLUKT}
        self.store.update(
            item["id"], status=WACHTEND,
            note=f"Tandoor antwoordde niet; poging {item['attempts']} van "
                 f"{MAX_ONBEREIKBAAR}")
        log.warning("Tandoor antwoordde niet voor item %s: %s", item["id"], fout.message)
        return {"status": WACHTEND, "wait": min(60, 5 * max(1, item["attempts"]))}

    def _attach_photo(self, recipe_id, bron, pagina) -> str | None:
        foto_url = bron.recipe.get("image_url") or (bron.images[0] if bron.images else None)
        if not foto_url:
            return "geen foto op de pagina gevonden"
        foto_url = urljoin(pagina.final_url, foto_url)
        try:
            inhoud, content_type = self._fetch_photo(foto_url, referer=pagina.final_url)
            self.client.set_image(recipe_id, inhoud, content_type=content_type)
        except (FetchError, TandoorError) as e:
            melding = getattr(e, "reason", None) or getattr(e, "message", str(e))
            log.warning("foto mislukte voor recept %s: %s", recipe_id, melding)
            return f"foto mislukte: {melding}"
        return None
