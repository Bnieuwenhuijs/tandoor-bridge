"""
Een wachttijd die op de pagina staat, maar niet in Tandoor terechtkomt.

Tandoor zet prepTime in de werktijd en cookTime in de wachttijd
(get_from_scraper in cookbook/helper/recipe_url_import.py). Veel sites geven
geen cookTime maar wel een bereidingstijd en een totale tijd: 15 minuten
bezig, 1 uur in totaal. Dan blijft de wachttijd in Tandoor 0, terwijl hij op
de pagina gewoon staat - als het verschil.

Alleen uit velden, niet uit de tekst van de stappen: "25 min. in de oven"
lezen is raden, en de werktijd en de wachttijd in Tandoor horen te kloppen.
En alleen als het eenduidig is: Tandoor nam de bereidingstijd als werktijd,
er was geen cookTime, en de totale tijd is langer. In elk ander geval laten
we staan wat Tandoor ervan maakte.
"""

from __future__ import annotations

import json
import re

_LD_JSON_RE = re.compile(
    r"""<script[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.I | re.S)
_DUUR_RE = re.compile(
    r"^P(?:(\d+(?:[.,]\d+)?)D)?(?:T(?:(\d+(?:[.,]\d+)?)H)?(?:(\d+(?:[.,]\d+)?)M)?"
    r"(?:(\d+(?:[.,]\d+)?)S)?)?$", re.I)


def minuten(waarde) -> int:
    """Een ISO 8601-duur (PT1H30M, P0DT45M) of een kaal getal, in hele minuten."""
    if isinstance(waarde, list):
        waarde = waarde[0] if waarde else None
    if isinstance(waarde, (int, float)):
        return max(int(waarde), 0)
    tekst = str(waarde or "").strip().replace(" ", "")
    if tekst.isdigit():
        return int(tekst)
    gevonden = _DUUR_RE.match(tekst)
    if not tekst or not gevonden:
        return 0
    dagen, uren, mins, secs = (float((d or "0").replace(",", ".")) for d in gevonden.groups())
    return int(round(dagen * 1440 + uren * 60 + mins + secs / 60))


def _recepten(data):
    """Alle schema.org-Recipes in een JSON-LD-blok, ook in @graph en lijsten."""
    if isinstance(data, list):
        for deel in data:
            yield from _recepten(deel)
    elif isinstance(data, dict):
        soort = data.get("@type")
        soorten = soort if isinstance(soort, list) else [soort]
        if any(str(s).lower() == "recipe" for s in soorten):
            yield data
        if "@graph" in data:
            yield from _recepten(data["@graph"])


def tijden_op_pagina(html: str) -> dict | None:
    """{"prep", "cook", "total"} in minuten uit het eerste recept, of None."""
    for blok in _LD_JSON_RE.findall(html or ""):
        try:
            data = json.loads(blok.strip(), strict=False)
        except ValueError:
            continue
        for recept in _recepten(data):
            return {"prep": minuten(recept.get("prepTime")),
                    "cook": minuten(recept.get("cookTime")),
                    "total": minuten(recept.get("totalTime"))}
    return None


def ontbrekende_wachttijd(recept: dict, html: str) -> int | None:
    """
    De wachttijd die Tandoor liet liggen, of None als er niets aan te vullen is.

    recept is wat recipe-from-source teruggaf; html de pagina die we stuurden.
    """
    try:
        if int(recept.get("waiting_time") or 0) > 0:
            return None
        werktijd = int(recept.get("working_time") or 0)
    except (TypeError, ValueError):
        return None
    tijden = tijden_op_pagina(html)
    if not tijden or tijden["cook"] or not tijden["prep"] or not tijden["total"]:
        return None
    # Tandoor nam de bereidingstijd als werktijd; anders weten we niet waar
    # de werktijd vandaan kwam, en is het verschil geen wachttijd.
    if werktijd != tijden["prep"]:
        return None
    wachten = tijden["total"] - tijden["prep"]
    return wachten if wachten > 0 else None
