"""Tests voor wachttijd.py: de wachttijd die Tandoor op de pagina liet liggen."""

import json

import pytest

from wachttijd import minuten, ontbrekende_wachttijd, tijden_op_pagina


def pagina(*blokken):
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>'
                      for b in blokken)
    return f"<html><head>{scripts}</head></html>"


@pytest.mark.parametrize("waarde, verwacht", [
    ("PT45M", 45), ("PT1H30M", 90), ("PT1.5H", 90), ("PT1H 30M", 90),
    ("P0DT1H", 60), ("P1D", 1440), ("PT90S", 2), ("45", 45), (30, 30),
    (["PT10M"], 10), ("", 0), (None, 0), ("ongeveer een uur", 0), ("P", 0),
])
def test_minuten(waarde, verwacht):
    assert minuten(waarde) == verwacht


def test_tijden_ook_uit_een_graph():
    html = pagina({"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage"},
        {"@type": ["Recipe", "Thing"], "prepTime": "PT10M", "totalTime": "PT40M"},
    ]})
    assert tijden_op_pagina(html) == {"prep": 10, "cook": 0, "total": 40}


def test_kapotte_json_wordt_overgeslagen():
    html = ('<script type="application/ld+json">{kapot</script>'
            + pagina({"@type": "Recipe", "prepTime": "PT5M"}))
    assert tijden_op_pagina(html)["prep"] == 5


def test_geen_recept_op_de_pagina():
    assert tijden_op_pagina(pagina({"@type": "Organization"})) is None
    assert tijden_op_pagina("") is None


GOED = pagina({"@type": "Recipe", "prepTime": "PT15M", "totalTime": "PT1H"})


def test_totaal_min_bereiding():
    assert ontbrekende_wachttijd({"working_time": 15, "waiting_time": 0}, GOED) == 45


@pytest.mark.parametrize("recept, html", [
    # Tandoor vulde de wachttijd al.
    ({"working_time": 15, "waiting_time": 30}, GOED),
    # De werktijd komt niet uit prepTime: onduidelijk waar die vandaan kwam.
    ({"working_time": 60, "waiting_time": 0}, GOED),
    # Er staat een cookTime: die had Tandoor al moeten gebruiken.
    ({"working_time": 15, "waiting_time": 0},
     pagina({"@type": "Recipe", "prepTime": "PT15M", "cookTime": "PT20M",
             "totalTime": "PT1H"})),
    # Geen totale tijd, of geen bereidingstijd.
    ({"working_time": 15, "waiting_time": 0}, pagina({"@type": "Recipe", "prepTime": "PT15M"})),
    ({"working_time": 0, "waiting_time": 0}, pagina({"@type": "Recipe", "totalTime": "PT1H"})),
    # Totaal niet langer dan de bereiding.
    ({"working_time": 15, "waiting_time": 0},
     pagina({"@type": "Recipe", "prepTime": "PT15M", "totalTime": "PT10M"})),
    # Rommel in het recept zelf.
    ({"working_time": "veel", "waiting_time": 0}, GOED),
])
def test_in_twijfel_niets_aanvullen(recept, html):
    assert ontbrekende_wachttijd(recept, html) is None
