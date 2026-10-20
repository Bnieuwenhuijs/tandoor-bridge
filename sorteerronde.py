"""
De sorteerronde: bestaande recepten laten sorteren, als achtergrondtaak.

Twee fasen, met een knop ertussen. De proefdraai haalt elk recept op, slaat
over wat geen sortering nodig heeft, en vraagt voor de rest Tandoors AI om
een indeling - maar schrijft niets. Wat de AI voorstelt, gaat de database in,
en is in het tabblad te bekijken. Pas "Uitvoeren" schrijft, en dan precies
dat voorstel: niet een nieuwe AI-vraag, die iets anders kan opleveren dan wat
je net hebt gezien.

Alles staat in SQLite, naast de importwachtrij: per recept de status, de
reden en het tijdstip, en de ronde zelf (welke fase, loopt hij). Een herstart
van de container gaat dus verder waar hij was. De statussen:

  wachtend      nog te bekijken, of - met een voorstel - klaar om uit te voeren
  overgeslagen  hoeft niet: al verdeeld, of geen ingrediënten of stappen
  gesorteerd    geschreven
  mislukt       met de reden
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import sorteren
from sorteren import GESORTEERD, MISLUKT, OVERGESLAGEN, WACHTEND
from tandoor import (TandoorAiCredits, TandoorAiError, TandoorError, TandoorThrottled,
                     TandoorUnreachable)

log = logging.getLogger(__name__)

PROEFDRAAI, UITVOEREN = "proefdraai", "uitvoeren"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sorteren (
    recipe_id    INTEGER PRIMARY KEY,
    naam         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    reden        TEXT,
    plan         TEXT,
    samenvatting TEXT,
    bijgewerkt   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sorteren_status ON sorteren(status);
CREATE TABLE IF NOT EXISTS sorteerronde (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    modus     TEXT,
    actief    INTEGER NOT NULL DEFAULT 0,
    opgesomd  INTEGER NOT NULL DEFAULT 0,
    melding   TEXT,
    gestart   TEXT,
    bijgewerkt TEXT
);
INSERT OR IGNORE INTO sorteerronde (id, actief) VALUES (1, 0);
"""


def _nu() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pauze_uit_limiet(limiet: str) -> float:
    """
    Seconden tussen twee AI-aanroepen bij een limiet als "60/hour".

    Hetzelfde formaat als Tandoors AI_RATELIMIT (DRF: aantal/periode, waarvan
    alleen de eerste letter telt), zodat je de waarde kunt overnemen.
    """
    gevonden = re.fullmatch(r"\s*(\d+)\s*/\s*([smhd])\w*\s*", limiet or "", re.IGNORECASE)
    if not gevonden or int(gevonden.group(1)) <= 0:
        return 60.0
    periode = {"s": 1, "m": 60, "h": 3600, "d": 86400}[gevonden.group(2).lower()]
    return periode / int(gevonden.group(1))


class SorteerStore:
    """De twee tabellen, in hetzelfde databasebestand als de importwachtrij."""

    def __init__(self, path):
        self.path = str(path)
        with self._connect() as db:
            db.executescript(_SCHEMA)

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

    # -- de ronde ---------------------------------------------------------

    def ronde(self) -> dict:
        with self._connect() as db:
            return dict(db.execute("SELECT * FROM sorteerronde WHERE id = 1").fetchone())

    def zet_ronde(self, **velden):
        velden["bijgewerkt"] = _nu()
        kolommen = ", ".join(f"{k} = ?" for k in velden)
        with self._connect() as db:
            db.execute(f"UPDATE sorteerronde SET {kolommen} WHERE id = 1", list(velden.values()))

    def begin(self, modus: str) -> str | None:
        """Zet een ronde aan. Geeft een reden terug als dat niet kan."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ronde = dict(db.execute("SELECT * FROM sorteerronde WHERE id = 1").fetchone())
            if ronde["actief"]:
                return f"er loopt al een {ronde['modus']}"
            if modus == UITVOEREN:
                klaar = db.execute("SELECT COUNT(*) FROM sorteren WHERE status = ? "
                                   "AND plan IS NOT NULL", (WACHTEND,)).fetchone()[0]
                if not klaar:
                    return "er is nog geen voorstel om uit te voeren; doe eerst een proefdraai"
                melding = f"{klaar} recept(en) uitvoeren"
            else:
                # Een proefdraai gaat verder waar hij was: elk voorstel heeft
                # tegoed gekost, en wie na "stop" opnieuw op Proefdraai drukt,
                # wil dat niet nog eens betalen. Alleen wat mislukte, wordt
                # opnieuw bekeken - ook een uitvoering die mislukte omdat het
                # recept intussen veranderd was. Nieuwe recepten in Tandoor
                # komen er bij het ophalen vanzelf bij.
                db.execute("UPDATE sorteren SET status = ?, reden = NULL, plan = NULL, "
                           "samenvatting = NULL, bijgewerkt = ? WHERE status = ?",
                           (WACHTEND, _nu(), MISLUKT))
                melding = "recepten ophalen uit Tandoor"
            db.execute("UPDATE sorteerronde SET modus = ?, actief = 1, opgesomd = 0, "
                       "melding = ?, gestart = ?, bijgewerkt = ? WHERE id = 1",
                       (modus, melding, _nu(), _nu()))
        return None

    def stop(self, melding: str):
        self.zet_ronde(actief=0, melding=melding)

    # -- recepten ---------------------------------------------------------

    def voeg_toe(self, recepten):
        """Nieuwe recept-ids als wachtend; wat er al staat blijft zoals het is."""
        with self._connect() as db:
            db.executemany("INSERT OR IGNORE INTO sorteren (recipe_id, naam, status, "
                           "bijgewerkt) VALUES (?, ?, ?, ?)",
                           [(rid, naam, WACHTEND, _nu()) for rid, naam in recepten])
            # Een naam die in Tandoor veranderde, ook hier.
            db.executemany("UPDATE sorteren SET naam = ? WHERE recipe_id = ?",
                           [(naam, rid) for rid, naam in recepten])

    def zet(self, recipe_id: int, status: str, *, reden=None, plan=None,
            samenvatting=None, naam=None):
        velden = {"status": status, "reden": reden, "bijgewerkt": _nu(),
                  "plan": json.dumps(plan) if plan is not None else None,
                  "samenvatting": json.dumps(samenvatting) if samenvatting is not None else None}
        if naam is not None:
            velden["naam"] = naam
        kolommen = ", ".join(f"{k} = ?" for k in velden)
        with self._connect() as db:
            db.execute(f"UPDATE sorteren SET {kolommen} WHERE recipe_id = ?",
                       [*velden.values(), recipe_id])

    def zet_resultaat(self, recipe_id: int, status: str, reden: str):
        """Na het uitvoeren: plan en samenvatting blijven, voor de geschiedenis."""
        with self._connect() as db:
            db.execute("UPDATE sorteren SET status = ?, reden = ?, bijgewerkt = ? "
                       "WHERE recipe_id = ?", (status, reden, _nu(), recipe_id))

    def volgende(self, *, met_plan: bool) -> dict | None:
        voorwaarde = "plan IS NOT NULL" if met_plan else "plan IS NULL"
        with self._connect() as db:
            rij = db.execute(f"SELECT * FROM sorteren WHERE status = ? AND {voorwaarde} "
                             f"ORDER BY recipe_id LIMIT 1", (WACHTEND,)).fetchone()
        return self._uit(rij) if rij else None

    def item(self, recipe_id: int) -> dict | None:
        with self._connect() as db:
            rij = db.execute("SELECT * FROM sorteren WHERE recipe_id = ?",
                             (recipe_id,)).fetchone()
        return self._uit(rij) if rij else None

    def items(self, *, status: str | None = None, limit: int = 500) -> list:
        sql, args = "SELECT * FROM sorteren", []
        if status == "voorstel":
            sql += " WHERE status = ? AND plan IS NOT NULL"
            args.append(WACHTEND)
        elif status == "te_bekijken":
            sql += " WHERE status = ? AND plan IS NULL"
            args.append(WACHTEND)
        elif status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY bijgewerkt DESC, recipe_id LIMIT ?"
        args.append(int(limit))
        with self._connect() as db:
            return [self._uit(rij) for rij in db.execute(sql, args).fetchall()]

    def tellingen(self) -> dict:
        with self._connect() as db:
            rijen = db.execute("SELECT status, plan IS NOT NULL AS voorstel, COUNT(*) AS n "
                               "FROM sorteren GROUP BY status, plan IS NOT NULL").fetchall()
        uit = {WACHTEND: 0, OVERGESLAGEN: 0, GESORTEERD: 0, MISLUKT: 0,
               "voorstel": 0, "te_bekijken": 0}
        for rij in rijen:
            uit[rij["status"]] = uit.get(rij["status"], 0) + rij["n"]
            if rij["status"] == WACHTEND:
                uit["voorstel" if rij["voorstel"] else "te_bekijken"] += rij["n"]
        return uit

    @staticmethod
    def _uit(rij) -> dict:
        item = dict(rij)
        for veld in ("plan", "samenvatting"):
            item[veld] = json.loads(item[veld]) if item.get(veld) else None
        return item


class SorteerRonde:
    """
    De achtergrondtaak. Eén recept per stap; tussen twee AI-aanroepen de
    pauze die bij AI_RATELIMIT hoort.

    Let op wat die limiet in Tandoor is: AiEndpointThrottle en
    RecipeImportThrottle zijn allebei een UserRateThrottle zonder eigen scope,
    en delen dus één teller. Een import die tegelijk loopt, telt mee. Komt er
    toch een 429, dan wacht de ronde de Retry-After af en probeert hetzelfde
    recept opnieuw; hij is een achtergrondtaak en heeft geen haast.
    """

    def __init__(self, store: SorteerStore, client, sorteerder, *,
                 ai_pauze: float = 60.0, schrijf_pauze: float = 1.0,
                 onbereikbaar_pauze: float = 60.0):
        self.store = store
        self.client = client
        self.sorteerder = sorteerder
        self.ai_pauze = float(ai_pauze)
        self.schrijf_pauze = float(schrijf_pauze)
        self.onbereikbaar_pauze = float(onbereikbaar_pauze)
        self._stop = threading.Event()
        self._wakker = threading.Event()
        self._thread = None
        # Alleen voor het tabblad: waar hij nu mee bezig is.
        self.bezig = None

    # -- draaien ----------------------------------------------------------

    def start(self):
        ronde = self.store.ronde()
        if ronde["actief"]:
            log.info("sorteerronde (%s) gaat verder na de herstart", ronde["modus"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sorteerronde", daemon=True)
        self._thread.start()

    def stop_thread(self, timeout=5.0):
        self._stop.set()
        self._wakker.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wek(self):
        """Na een start- of stopknop: niet wachten tot de pauze om is."""
        self._wakker.set()

    def begin(self, modus: str) -> str | None:
        fout = self.store.begin(modus)
        if fout is None:
            self.wek()
        return fout

    def stop(self) -> None:
        ronde = self.store.ronde()
        if ronde["actief"]:
            self.store.stop(f"{ronde['modus']} gestopt; met dezelfde knop ga je verder")
        self.wek()

    def _wacht(self, seconden: float):
        self._wakker.clear()
        self._wakker.wait(max(0.0, seconden))

    def _run(self):
        while not self._stop.is_set():
            try:
                wacht = self.stap()
            except Exception:
                # Niets mag deze thread doodmaken; dan staat de ronde stil
                # zonder dat iemand het ziet.
                log.exception("onverwachte fout in de sorteerronde")
                self.store.zet_ronde(melding="onverwachte fout; zie het logboek, de ronde "
                                             "probeert het over een minuut opnieuw")
                wacht = 60.0
            self._wacht(2.0 if wacht is None else wacht)

    # -- één stap ---------------------------------------------------------

    def stap(self) -> float | None:
        """
        Doe één stukje werk. Geeft terug hoe lang er daarna gewacht moet
        worden, of None als er niets te doen is. Apart van _run(), zodat de
        tests de ronde stap voor stap kunnen draaien.
        """
        ronde = self.store.ronde()
        if not ronde["actief"]:
            self.bezig = None
            return None
        if ronde["modus"] == PROEFDRAAI:
            return self._proefdraai(ronde)
        if ronde["modus"] == UITVOEREN:
            return self._uitvoeren()
        self.store.stop(f"onbekende fase {ronde['modus']!r}")
        return None

    def _proefdraai(self, ronde: dict) -> float:
        if not ronde["opgesomd"]:
            try:
                recepten = self.client.recipe_ids()
            except TandoorError as e:
                self.store.zet_ronde(melding=f"recepten ophalen mislukte ({e.message}); "
                                             f"opnieuw over {self.onbereikbaar_pauze:g} s")
                return self.onbereikbaar_pauze
            self.store.voeg_toe(recepten)
            self.store.zet_ronde(opgesomd=1, melding=f"{len(recepten)} recepten bekijken")
            return 0.0

        item = self.store.volgende(met_plan=False)
        if item is None:
            t = self.store.tellingen()
            self.store.stop(f"proefdraai klaar: {t['voorstel']} voorstel(len), "
                            f"{t[OVERGESLAGEN]} overgeslagen, {t[MISLUKT]} mislukt")
            self.bezig = None
            return 0.0
        rid = item["recipe_id"]
        self.bezig = {"recipe_id": rid, "naam": item["naam"], "wat": "bekijken"}

        try:
            recept = self.client.get_recipe(rid)
        except TandoorUnreachable as e:
            self.store.zet_ronde(melding=f"Tandoor niet bereikbaar ({e.message}); opnieuw "
                                         f"over {self.onbereikbaar_pauze:g} s")
            return self.onbereikbaar_pauze
        except TandoorError as e:
            reden = "bestaat niet meer in Tandoor" if e.status == 404 else e.message
            self.store.zet(rid, MISLUKT, reden=reden)
            return 0.0

        naam = recept.get("name") or item["naam"]
        if reden := sorteren.overslaan(recept):
            self.store.zet(rid, OVERGESLAGEN, reden=reden, naam=naam)
            return 0.0                          # geen AI-aanroep, dus geen pauze

        provider = self.sorteerder.provider()
        if provider.id is None:
            self.store.stop(f"proefdraai gestopt: {provider.uitleg}")
            return 0.0
        if waarom := self._tegoed_op():
            self.store.stop(f"proefdraai gestopt: {waarom}. Vul het tegoed aan en druk "
                            f"opnieuw op Proefdraai; wat al bekeken is, blijft bekeken.")
            return 0.0

        self.bezig = {"recipe_id": rid, "naam": naam, "wat": "AI vraagt"}
        try:
            antwoord = self.client.ai_step_sort(recept, provider.id)
        except TandoorThrottled as e:
            self.store.zet_ronde(melding=f"AI-limiet bereikt; verder over {e.retry_after} s")
            return float(e.retry_after)
        except TandoorAiCredits as e:
            self.store.stop(f"proefdraai gestopt: {e.message}. Vul het tegoed aan en druk "
                            f"opnieuw op Proefdraai; wat al bekeken is, blijft bekeken.")
            return 0.0
        except TandoorAiError as e:
            # Kan ook een tegoedmelding in een andere taal zijn; de space weet
            # het zeker.
            if waarom := self._tegoed_op():
                self.store.stop(f"proefdraai gestopt: {waarom}")
                return 0.0
            self.store.zet(rid, MISLUKT, reden=e.message, naam=naam)
            return self.ai_pauze
        except TandoorError as e:
            self.store.zet(rid, MISLUKT, reden=e.message, naam=naam)
            return self.ai_pauze

        try:
            plan = sorteren.maak_plan(recept, antwoord)
        except sorteren.PlanFout as e:
            self.store.zet(rid, MISLUKT, reden=str(e), naam=naam)
            return self.ai_pauze
        if not sorteren.verandert_iets(recept, plan):
            self.store.zet(rid, OVERGESLAGEN, reden="de AI liet het recept zoals het was",
                           naam=naam)
            return self.ai_pauze
        self.store.zet(rid, WACHTEND, plan=plan, naam=naam,
                       samenvatting=sorteren.samenvatting(recept, plan))
        self.store.zet_ronde(melding=f"proefdraai loopt; volgende AI-vraag over "
                                     f"{self.ai_pauze:g} s")
        return self.ai_pauze

    def _uitvoeren(self) -> float:
        item = self.store.volgende(met_plan=True)
        if item is None:
            t = self.store.tellingen()
            self.store.stop(f"uitvoeren klaar: {t[GESORTEERD]} gesorteerd, "
                            f"{t[MISLUKT]} mislukt")
            self.bezig = None
            return 0.0
        rid = item["recipe_id"]
        self.bezig = {"recipe_id": rid, "naam": item["naam"], "wat": "schrijven"}
        try:
            melding = sorteren.voer_uit(self.client, rid, item["plan"])
        except sorteren.UitvoerFout as e:
            self.store.zet_resultaat(rid, MISLUKT, str(e))
            return self.schrijf_pauze
        except TandoorUnreachable as e:
            # Het ophalen vooraf lukte niet: er is niets geschreven.
            self.store.zet_ronde(melding=f"Tandoor niet bereikbaar ({e.message}); opnieuw "
                                         f"over {self.onbereikbaar_pauze:g} s")
            return self.onbereikbaar_pauze
        except TandoorError as e:
            reden = "bestaat niet meer in Tandoor" if e.status == 404 else e.message
            self.store.zet_resultaat(rid, MISLUKT, reden)
            return self.schrijf_pauze
        self.store.zet_resultaat(rid, GESORTEERD, melding)
        return self.schrijf_pauze

    def _tegoed_op(self) -> str | None:
        try:
            return sorteren.tegoed_op(self.client.current_space())
        except TandoorError:
            return None                 # niet te zien; Tandoor zegt het wel

    # -- voor het tabblad -------------------------------------------------

    def status(self) -> dict:
        ronde = self.store.ronde()
        provider = self.sorteerder.provider() if self.sorteerder else None
        return {
            "ronde": {k: ronde.get(k) for k in ("modus", "actief", "melding", "gestart",
                                                 "bijgewerkt")},
            "tellingen": self.store.tellingen(),
            "bezig": self.bezig if ronde["actief"] else None,
            "provider": provider.als_dict() if provider else None,
            "ai_pauze": self.ai_pauze,
            "draait": self.running,
        }
