"""
Ingrediënten bij de juiste stap, met Tandoors eigen AI-sortering.

Tandoor kan dit zelf: POST /api/ai-step-sort/ is de knop "Automatisch
sorteren" in de recepteditor. Die knop slaat niets op - het gesorteerde
recept komt terug, en de editor laat het je eerst zien. Hier doet de brug
dat werk: voor een nieuw recept tussen "Tandoor laten ontleden" en
"aanmaken", en voor een bestaand recept als aparte ronde, met eerst een
proefdraai.

Bij een nieuw recept nemen we het antwoord over zoals het is. Bij een
bestaand recept veranderen alleen de stappen en welk ingrediënt bij welke
stap hoort; de ingrediënten zelf - hoeveelheid, eenheid, food - schrijven
we niet. En dat is meer dan een voorkeur: hoe Tandoor een bestaand recept
bijwerkt, bepaalt wat er veilig te sturen valt. Zie mock_opslag.py voor de
details uit de broncode; kort:

  * Een id wordt in de hele database opgezocht, niet in het recept. Een id
    dat de AI verzint of verwisselt, zou een stap of ingrediënt van een
    ander recept bijwerken en hier aanhangen. Dus gebruiken we alleen ids
    die in dít recept staan.
  * Een ingrediënt in twee stappen staat er twee keer; een ingrediënt in
    geen enkele stap is weg uit het recept. Dus komt elk ingrediënt in
    precies één stap: dubbele tellen één keer, en wat de AI weglaat gaat
    terug naar stap 1, waar Tandoor het bij het importeren zette.
  * Een food die mee wordt gestuurd, wordt bijgewerkt - voor elk recept dat
    hem gebruikt. Dus sturen we per ingrediënt alleen id en volgorde.
  * Een nieuwe stap is nooit partial, ook niet voor de ingrediënten eronder:
    die zouden dan food, unit en amount nodig hebben, en dan schrijven we de
    food alsnog. Dus maken we nieuwe stappen eerst leeg aan (stap A), en
    verplaatsen we daarna in één keer alles naar bestaande stappen (stap B).
  * Er is geen transactie. Wat we sturen kan daarom niet halverwege op
    validatie stuklopen: in A staan alleen een id per bestaande stap en lege
    nieuwe stappen, in B alleen bestaande stappen en ingrediënten, partial.
    Gaat er toch iets mis, of klopt het resultaat niet, dan zetten we de
    oorspronkelijke indeling terug.
  * Wat uit de lijst valt wordt losgekoppeld, niet verwijderd. Een stap die
    de AI samenvoegt, staat daarna nog in de database, alleen aan geen recept.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

import copy

from tandoor import (TandoorAiCredits, TandoorAiError, TandoorAiTimeout, TandoorClient,
                     TandoorError, TandoorThrottled)

log = logging.getLogger(__name__)

WACHTEND, OVERGESLAGEN, GESORTEERD, MISLUKT = "wachtend", "overgeslagen", "gesorteerd", "mislukt"


class PlanFout(Exception):
    """Van dit AI-antwoord is geen indeling te maken."""


class UitvoerFout(Exception):
    """Het plan kon niet worden geschreven. Het recept is zoals het was."""


# -- provider en tegoed ----------------------------------------------------

@dataclass
class Provider:
    """Welke AI-provider de brug gebruikt, en waarom - of waarom geen."""
    id: int | None
    naam: str = ""
    uitleg: str = ""
    keuzes: list = field(default_factory=list)

    def als_dict(self) -> dict:
        return asdict(self)


def _provider_tekst(p: dict) -> str:
    return f"{p.get('id')} ({p.get('name') or 'zonder naam'})"


def kies_provider(client: TandoorClient, ingesteld: str = "") -> Provider:
    """
    TANDOOR_AI_PROVIDER_ID als die er is, anders de standaardprovider van de
    space, anders de enige die er is. Bij meer dan één zonder keuze gokken we
    niet: elke aanroep kost tegoed, en dan hoort het de goede te zijn.
    """
    try:
        providers = client.ai_providers()
    except TandoorError as e:
        return Provider(None, uitleg=f"de AI-providers zijn niet op te vragen: {e.message}")
    keuzes = [{"id": p.get("id"), "naam": p.get("name") or ""} for p in providers]
    op_id = {p.get("id"): p for p in providers}

    ingesteld = (ingesteld or "").strip()
    if ingesteld:
        try:
            gekozen = op_id.get(int(ingesteld))
        except ValueError:
            gekozen = None
        if gekozen:
            return Provider(gekozen["id"], gekozen.get("name") or "",
                            "ingesteld via TANDOOR_AI_PROVIDER_ID", keuzes)
        beschikbaar = ", ".join(_provider_tekst(p) for p in providers) or "geen"
        return Provider(None, uitleg=f"TANDOOR_AI_PROVIDER_ID={ingesteld} bestaat niet in "
                                     f"Tandoor; beschikbaar: {beschikbaar}", keuzes=keuzes)

    try:
        standaard = (client.current_space() or {}).get("ai_default_provider")
    except TandoorError:
        standaard = None
    if isinstance(standaard, dict) and standaard.get("id") in op_id:
        gekozen = op_id[standaard["id"]]
        return Provider(gekozen["id"], gekozen.get("name") or "",
                        "de standaardprovider van de space", keuzes)
    if len(providers) == 1:
        return Provider(providers[0]["id"], providers[0].get("name") or "",
                        "de enige AI-provider in Tandoor", keuzes)
    if not providers:
        return Provider(None, uitleg="er is geen AI-provider ingesteld in Tandoor "
                                     "(Instellingen → AI)", keuzes=keuzes)
    return Provider(None, uitleg="er zijn meer AI-providers en geen standaard; zet "
                                 "TANDOOR_AI_PROVIDER_ID op een van: "
                                 + ", ".join(_provider_tekst(p) for p in providers),
                    keuzes=keuzes)


def tegoed_op(space: dict) -> str | None:
    """
    Waarom de space geen AI mag gebruiken, of None als het wel mag.

    Dezelfde som als can_perform_ai_request() in cookbook/helper/ai_helper.py:
    (maandtegoed over of saldo boven nul) en AI aan.
    """
    if not space.get("ai_enabled"):
        return "AI staat uit voor deze space"
    try:
        maand_over = float(space.get("ai_monthly_credits_used") or 0) < \
            float(space.get("ai_credits_monthly") or 0)
        saldo = float(space.get("ai_credits_balance") or 0)
    except (TypeError, ValueError):
        return None                     # niet te lezen: laat Tandoor het zeggen
    if maand_over or saldo > 0:
        return None
    return ("het AI-tegoed van de space is op (maandtegoed verbruikt, saldo "
            f"{saldo:g})")


# -- recepten --------------------------------------------------------------

def _stappen(recept: dict) -> list:
    return [s for s in (recept.get("steps") or []) if isinstance(s, dict)]


def _ingredienten(stap: dict) -> list:
    return [i for i in (stap.get("ingredients") or []) if isinstance(i, dict)]


def overslaan(recept: dict) -> str | None:
    """Waarom dit recept niet gesorteerd hoeft te worden, of None."""
    stappen = _stappen(recept)
    if not stappen:
        return "geen stappen"
    met = [s for s in stappen if _ingredienten(s)]
    if not met:
        return "geen ingrediënten"
    if len(met) > 1:
        return f"de ingrediënten staan al over {len(met)} stappen verdeeld"
    return None


def ingredient_tekst(ingredient: dict) -> str:
    tekst = (ingredient.get("original_text") or "").strip()
    if tekst:
        return tekst
    delen = []
    if ingredient.get("amount"):
        delen.append(f"{ingredient['amount']:g}" if isinstance(ingredient["amount"], float)
                     else str(ingredient["amount"]))
    for veld in ("unit", "food"):
        waarde = ingredient.get(veld)
        if isinstance(waarde, dict) and waarde.get("name"):
            delen.append(str(waarde["name"]))
    return " ".join(delen) or f"ingrediënt {ingredient.get('id')}"


def momentopname(recept: dict) -> dict:
    """Waaraan we zien of een recept sinds de proefdraai veranderd is."""
    return {
        "updated_at": recept.get("updated_at"),
        "stappen": [[s.get("id"), [i.get("id") for i in _ingredienten(s)]]
                    for s in _stappen(recept)],
    }


# -- wat de AI te zien krijgt ---------------------------------------------

def voor_ai(recept: dict) -> dict:
    """
    Het recept zoals de AI het nodig heeft om te sorteren, en niet meer.

    Tandoor geeft een recept met per ingrediënt de hele food (eigenschappen,
    categorie, overerving, vervangers), omrekeningen, de recepten waarin het
    nog meer voorkomt, en per stap ook de markdown. Een recept van 15
    ingrediënten wordt zo 50 KB. Tandoors prompt zegt "geef het recept terug in
    hetzelfde formaat", dus het model moet dat allemaal opnieuw uittypen. Dat
    duurde drie minuten en werd dan halverwege afgekapt (JSONDecodeError in
    Tandoor), terwijl de nginx in de Tandoor-container na 60 seconden al een
    504 had gegeven.

    Om te sorteren is alleen nodig: de stappen met hun tekst, en per
    ingrediënt waar het om gaat. De ids blijven erin, want daarmee leest
    maak_plan() het antwoord terug. RecipeSerializer(partial=True) keurt dit
    goed: wat ontbreekt, wordt bij partial overgeslagen.
    """
    def naam(waarde):
        return {"name": waarde.get("name") or ""} if isinstance(waarde, dict) else None

    def ingredient(i: dict) -> dict:
        uit = {"food": naam(i.get("food")), "unit": naam(i.get("unit")),
               "amount": i.get("amount") or 0, "note": i.get("note") or ""}
        if i.get("id") is not None:
            uit = {"id": i["id"], **uit}
        if i.get("is_header"):
            uit["is_header"] = True
        return uit

    stappen = []
    for stap in _stappen(recept):
        uit = {"instruction": stap.get("instruction") or "",
               "ingredients": [ingredient(i) for i in _ingredienten(stap)]}
        if stap.get("id") is not None:
            uit = {"id": stap["id"], **uit}
        stappen.append(uit)
    return {"name": recept.get("name") or "", "steps": stappen}


# -- van AI-antwoord naar plan --------------------------------------------

def _uitpakken(antwoord: dict) -> dict:
    """Sommige modellen zetten er {"recipe": ...} omheen."""
    if isinstance(antwoord, dict) and "steps" not in antwoord \
            and isinstance(antwoord.get("recipe"), dict):
        return antwoord["recipe"]
    return antwoord


def _zelfde_inhoud(a: dict, b: dict) -> bool:
    def naam(i, veld):
        waarde = i.get(veld)
        return (waarde.get("name") or "").strip().lower() if isinstance(waarde, dict) else ""

    try:
        zelfde_hoeveelheid = float(a.get("amount") or 0) == float(b.get("amount") or 0)
    except (TypeError, ValueError):
        zelfde_hoeveelheid = False
    return (zelfde_hoeveelheid and naam(a, "food") == naam(b, "food")
            and naam(a, "unit") == naam(b, "unit") and bool(naam(a, "food")))


def maak_plan(origineel: dict, ai_antwoord: dict) -> dict:
    """
    De indeling die de AI voorstelt, vertaald naar ids uit dit recept.

    Geeft een dict terug die zo in de database kan, met de stappen
    ([{id of None, instruction, ingredienten: [ids]}]), de notities over
    wat er bijgesteld moest worden, en de momentopname van het recept.
    """
    ai = _uitpakken(ai_antwoord)
    ai_stappen = _stappen(ai) if isinstance(ai, dict) else []
    if not ai_stappen:
        raise PlanFout("de AI gaf geen stappen terug")

    orig_stappen = {s.get("id"): s for s in _stappen(origineel)}
    orig_ingr = {i.get("id"): i for s in _stappen(origineel) for i in _ingredienten(s)}
    volgorde = [i.get("id") for s in _stappen(origineel) for i in _ingredienten(s)]
    gebruikte_stappen, toegewezen = set(), set()
    notities = []
    vreemd = dubbel = onbekend = 0

    plan = []
    for ai_stap in ai_stappen:
        sid = ai_stap.get("id")
        if sid in orig_stappen and sid not in gebruikte_stappen:
            gebruikte_stappen.add(sid)
        else:
            # Een id dat niet in dit recept staat, of dat de AI op twee
            # stukken van een gesplitste stap zette: dan is het een nieuwe stap.
            sid = None
        instructie = ai_stap.get("instruction")
        if not isinstance(instructie, str):
            instructie = (orig_stappen.get(sid) or {}).get("instruction") or ""
        ids = []
        for ai_ingr in _ingredienten(ai_stap):
            iid = ai_ingr.get("id")
            if iid in orig_ingr:
                if iid in toegewezen:
                    dubbel += 1
                    continue
            else:
                if iid is not None:
                    vreemd += 1
                # Zonder (bruikbaar) id: zoek het op inhoud tussen wat nog over is.
                iid = next((k for k in volgorde if k not in toegewezen
                            and _zelfde_inhoud(ai_ingr, orig_ingr[k])), None)
                if iid is None:
                    onbekend += 1
                    continue
            toegewezen.add(iid)
            ids.append(iid)
        plan.append({"id": sid, "instruction": instructie, "ingredienten": ids})

    # Een stap zonder bruikbaar id krijgt de eerstvolgende stap van het recept
    # die nog niet gebruikt is. Anders zou een AI die alle ids weglaat elke
    # stap opnieuw laten aanmaken, en verloren tijd, bestand en onderliggend
    # recept van de oude stappen meegaan. Pas als er geen ongebruikte meer
    # over zijn - de AI splitste - wordt het een nieuwe stap.
    vrij = [sid for sid in orig_stappen if sid not in gebruikte_stappen]
    for stap in plan:
        if stap["id"] is None and vrij:
            stap["id"] = vrij.pop(0)

    weggelaten = [iid for iid in volgorde if iid not in toegewezen]
    if weggelaten:
        plan[0]["ingredienten"] = weggelaten + plan[0]["ingredienten"]
        namen = ", ".join(ingredient_tekst(orig_ingr[i]) for i in weggelaten[:5])
        notities.append(f"{len(weggelaten)} ingrediënt(en) liet de AI weg; die blijven in "
                        f"stap 1: {namen}")
    if dubbel:
        notities.append(f"{dubbel} ingrediënt(en) zette de AI in meer dan één stap; "
                        f"ze staan alleen in de eerste")
    if vreemd:
        notities.append(f"{vreemd} keer gaf de AI een id dat niet bij dit recept hoort; "
                        f"dat is genegeerd")
    if onbekend:
        notities.append(f"{onbekend} ingrediënt(en) van de AI stonden niet in het recept; "
                        f"genegeerd")
    return {"stappen": plan, "notities": notities, "momentopname": momentopname(origineel)}


def verandert_iets(origineel: dict, plan: dict) -> bool:
    """
    Zou dit plan iets aan het recept veranderen?

    Een recept met één stap waar de AI niets aan splitst, blijft precies
    zoals het was. Dat hoort geen voorstel te zijn: uitvoeren zou alleen
    dezelfde stappen nog eens wegschrijven.
    """
    nu = [(s.get("id"), s.get("instruction") or "", [i.get("id") for i in _ingredienten(s)])
          for s in _stappen(origineel)]
    straks = [(s["id"], s["instruction"] or "", s["ingredienten"]) for s in plan["stappen"]]
    return nu != straks


def samenvatting(origineel: dict, plan: dict) -> dict:
    """Wat de proefdraai laat zien: stappen voor en na, en wat waarheen gaat."""
    orig_ingr = {i.get("id"): i for s in _stappen(origineel) for i in _ingredienten(s)}
    toewijzing = [
        {"ingredient": ingredient_tekst(orig_ingr[iid]), "stap": nummer}
        for nummer, stap in enumerate(plan["stappen"], start=1)
        for iid in stap["ingredienten"] if iid in orig_ingr
    ]
    return {"stappen_voor": len(_stappen(origineel)), "stappen_na": len(plan["stappen"]),
            "toewijzing": toewijzing, "notities": plan.get("notities") or []}


# -- schrijven -------------------------------------------------------------

def _terugzetten(client: TandoorClient, recept_id: int, origineel: dict):
    """De oorspronkelijke stappen en indeling, met alleen bestaande ids."""
    stappen = [{"id": s["id"], "instruction": s.get("instruction") or "",
                "order": s.get("order") or 0,
                "ingredients": [{"id": i["id"], "order": i.get("order") or 0}
                                for i in _ingredienten(s)]}
               for s in _stappen(origineel)]
    client.patch_steps(recept_id, stappen, retry=True)


def _klopt(antwoord: dict, ids: list, plan: list, alle: set) -> str | None:
    """Staat het recept nu zoals bedoeld? Anders: wat er afwijkt."""
    stappen = _stappen(antwoord)
    if [s.get("id") for s in stappen] != ids:
        return "de stappen staan niet zoals bedoeld"
    gezien = []
    for stap, bedoeld in zip(stappen, plan):
        ingredienten = [i.get("id") for i in _ingredienten(stap)]
        if set(ingredienten) != set(bedoeld["ingredienten"]):
            return "een ingrediënt staat niet in de bedoelde stap"
        gezien.extend(ingredienten)
    if len(gezien) != len(set(gezien)):
        return "een ingrediënt staat in meer dan één stap"
    if set(gezien) != alle:
        return "een ingrediënt staat in geen enkele stap meer"
    return None


def voer_uit(client: TandoorClient, recept_id: int, plan: dict) -> str:
    """
    Schrijf een plan uit de proefdraai. Gooit UitvoerFout als het niet lukt,
    en het recept staat dan weer zoals het was.
    """
    origineel = client.get_recipe(recept_id)
    if momentopname(origineel) != plan.get("momentopname"):
        raise UitvoerFout("het recept is sinds de proefdraai veranderd; "
                          "doe eerst een nieuwe proefdraai")
    alle = {i.get("id") for s in _stappen(origineel) for i in _ingredienten(s)}
    orig_ids = [s.get("id") for s in _stappen(origineel)]
    stappen = plan["stappen"]
    ids = [s["id"] for s in stappen]
    nieuw = [n for n, s in enumerate(stappen) if s["id"] is None]

    # Stap A: alleen nieuwe, lege stappen erbij. De bestaande staan er met
    # alleen hun id in, en daar verandert dan niets aan.
    if nieuw:
        verzoek = ([{"id": sid} for sid in orig_ids]
                   + [{"instruction": stappen[n]["instruction"], "name": "",
                       "order": len(orig_ids) + k, "ingredients": []}
                      for k, n in enumerate(nieuw)])
        try:
            antwoord = client.patch_steps(recept_id, verzoek, retry=False)
        except TandoorError as e:
            _probeer_terug(client, recept_id, origineel)
            raise UitvoerFout(f"nieuwe stappen aanmaken mislukte: {e.message}") from e
        erbij = [s.get("id") for s in _stappen(antwoord) if s.get("id") not in orig_ids]
        if len(erbij) != len(nieuw):
            _probeer_terug(client, recept_id, origineel)
            raise UitvoerFout("Tandoor maakte niet het verwachte aantal nieuwe stappen")
        # De nieuwe stappen komen terug op order en dan op id, dus in de
        # volgorde waarin we ze vroegen.
        for n, sid in zip(nieuw, erbij):
            ids[n] = sid

    # Stap B: alles naar z'n plek, in bestaande stappen, met alleen id en
    # volgorde per ingrediënt.
    verzoek = [{"id": ids[n], "instruction": s["instruction"], "order": n,
                "ingredients": [{"id": iid, "order": m}
                                for m, iid in enumerate(s["ingredienten"])]}
               for n, s in enumerate(stappen)]
    try:
        antwoord = client.patch_steps(recept_id, verzoek, retry=True)
    except TandoorError as e:
        _probeer_terug(client, recept_id, origineel)
        raise UitvoerFout(f"indelen mislukte: {e.message}") from e
    if afwijking := _klopt(antwoord, ids, stappen, alle):
        _probeer_terug(client, recept_id, origineel)
        raise UitvoerFout(f"na het schrijven klopte het niet ({afwijking})")
    return (f"{len(orig_ids)} → {len(stappen)} stappen, "
            f"{len(alle)} ingrediënten ingedeeld")


def _probeer_terug(client: TandoorClient, recept_id: int, origineel: dict):
    try:
        _terugzetten(client, recept_id, origineel)
    except TandoorError as e:
        # Dit is het enige geval waarin een recept half kan blijven staan, en
        # dat moet je dan weten: het staat in het logboek én in de reden.
        log.error("recept %s kon niet worden teruggezet: %s", recept_id, e.message)
        raise UitvoerFout(f"schrijven mislukte, en terugzetten ook ({e.message}); "
                          f"kijk recept {recept_id} na in Tandoor") from e


# -- nieuwe recepten -------------------------------------------------------

def _met_tijdelijke_ids(recept: dict) -> dict:
    """Een kopie met volgnummers als id, zodat het AI-antwoord terug te lezen is."""
    kopie = copy.deepcopy(recept)
    teller = 0
    for nummer, stap in enumerate(_stappen(kopie), start=1):
        stap["id"] = nummer
        for ingredient in _ingredienten(stap):
            teller += 1
            ingredient["id"] = teller
    return kopie


def sorteer_nieuw(client: TandoorClient, provider_id: int, recept: dict, *,
                  bij_time_out=None):
    """
    (gesorteerd recept, None, notities), of (None, reden, []) als de
    AI-aanroep mislukt.

    De AI krijgt de slanke vorm (voor_ai) en bepaalt de stappen: hoeveel,
    met welke tekst, en welk ingrediënt waar. De ingrediënten zelf komen uit
    wat Tandoor bij het ontleden maakte - met de oorspronkelijke regel, de
    notitie en de rest - want die stuurt de AI in de slanke vorm niet terug.
    Elk ingrediënt komt precies één keer in het recept, net als bij een
    bestaand recept; wat er daarvoor bijgesteld moest worden, staat in de
    notities.

    bij_time_out wordt aangeroepen als Tandoors nginx het opgaf (504): dan
    loopt de vraag in Tandoor nog, en hoort de volgende even te wachten.
    """
    met_ids = _met_tijdelijke_ids(recept)
    try:
        antwoord = client.ai_step_sort(voor_ai(met_ids), provider_id)
    except TandoorThrottled as e:
        return None, f"AI-limiet bereikt (weer mogelijk over {e.retry_after} s)", []
    except TandoorAiTimeout as e:
        if bij_time_out:
            bij_time_out()
        return None, e.message, []
    except (TandoorAiCredits, TandoorAiError) as e:
        return None, e.message, []
    except TandoorError as e:
        return None, f"AI: {e.message}", []
    try:
        plan = maak_plan(met_ids, antwoord)
    except PlanFout as e:
        return None, str(e), []

    orig_stappen = {s["id"]: s for s in _stappen(met_ids)}
    orig_ingr = {i["id"]: i for s in _stappen(met_ids) for i in _ingredienten(s)}
    eerste = _stappen(met_ids)[0]
    stappen = []
    for nummer, stap in enumerate(plan["stappen"]):
        basis = dict(orig_stappen.get(stap["id"]) or {
            "name": "", "time": 0,
            "show_ingredients_table": eerste.get("show_ingredients_table", True)})
        basis.pop("id", None)
        basis["instruction"] = stap["instruction"]
        basis["order"] = nummer
        basis["ingredients"] = []
        for volgorde, iid in enumerate(stap["ingredienten"]):
            ingredient = dict(orig_ingr[iid])
            ingredient.pop("id", None)
            ingredient["order"] = volgorde
            basis["ingredients"].append(ingredient)
        stappen.append(basis)
    return {**recept, "steps": stappen}, None, plan["notities"]


# -- bij het importeren ----------------------------------------------------

class Sorteerder:
    """
    Sorteren tijdens het importeren: aan of uit, en met welke provider.

    De provider wordt pas opgezocht als hij nodig is, en een kwartier
    onthouden. Zo zet een Tandoor die bij de start van de brug nog niet
    antwoordt het sorteren niet voorgoed uit, en kost niet elk recept twee
    extra aanvragen.
    """

    def __init__(self, client: TandoorClient, *, provider_id: str = "", aan: bool = True,
                 geldig: float = 900, klok=None):
        self.client = client
        self.ingesteld = provider_id
        self.aan = aan
        self._geldig = geldig
        self._klok = klok or time.monotonic
        self._provider: Provider | None = None
        self._sinds = 0.0
        # Na een 504 loopt de vorige AI-vraag in Tandoor nog door. Tot dit
        # moment slaan we sorteren bij het importeren over, anders stapelen
        # de vragen zich daar op.
        self._rust_tot = 0.0

    def provider(self, *, vers: bool = False) -> Provider:
        if vers or self._provider is None or self._klok() - self._sinds > self._geldig:
            self._provider = kies_provider(self.client, self.ingesteld)
            self._sinds = self._klok()
            if self._provider.id is None:
                log.warning("AI-sorteren niet mogelijk: %s", self._provider.uitleg)
            else:
                log.info("AI-sorteren met provider %s (%s): %s", self._provider.id,
                         self._provider.naam, self._provider.uitleg)
        return self._provider

    def sorteer_nieuw(self, recept: dict):
        """
        (gesorteerd, None, notities), (None, reden, []), of (None, None, [])
        als het uitstaat.
        """
        if not self.aan:
            return None, None, []
        if self._klok() < self._rust_tot:
            return None, ("AI even overgeslagen: de vorige vraag liep in Tandoor nog "
                          "na een time-out"), []
        provider = self.provider()
        if provider.id is None:
            return None, provider.uitleg, []
        return sorteer_nieuw(self.client, provider.id, recept,
                             bij_time_out=self.rust_na_time_out)

    def rust_na_time_out(self):
        self._rust_tot = self._klok() + NA_TIME_OUT


# Hoe lang na een 504 er geen nieuwe AI-vraag naar Tandoor gaat. In de logs
# van een echte Tandoor liep zo'n vraag na de 504 nog ruim twee minuten door.
NA_TIME_OUT = 240.0
