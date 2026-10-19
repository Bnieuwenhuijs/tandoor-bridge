"""
Staat alles wat de brug importeert ook echt in de image?

De COPY-regels in de Dockerfile zijn een lijst die met de hand bijgehouden
wordt, en die lijst is al twee keer achtergebleven bij de code: eerst bij de
browser-container, die daardoor in een herstartlus op de NAS belandde, en
daarna bij ah_api.py. Zo'n vergeten regel is onzichtbaar in de gewone tests -
daar liggen alle bestanden immers naast elkaar - en breekt pas bij de start
van de container.

De CI start elk image sindsdien op voor hij het pusht, dus het komt er wel
uit, maar pas na een build van een paar minuten en met een foutmelding waar je
even naar moet kijken. Deze test leest de Dockerfile, loopt de imports na en
noemt het bestand dat mist.
"""

import ast
import pathlib
import re
from fnmatch import fnmatch

import pytest

HIER = pathlib.Path(__file__).parent

# Waar elke image mee begint, en welke bestanden erin horen te zitten. De
# mocks en de rooktest gaan mee omdat de README zegt dat je ze in de container
# kunt draaien; staan ze er niet in, dan klopt die instructie niet.
IMAGES = [
    pytest.param(HIER / "Dockerfile", HIER,
                 ["app", "smoketest", "mock_tandoor", "mock_ah"], id="brug"),
    pytest.param(HIER / "renderer" / "Dockerfile", HIER / "renderer",
                 ["renderer"], id="browser-container"),
]


def _eigen_modules(map: pathlib.Path) -> set:
    """De modules die naast elkaar in deze map liggen; de rest komt uit pip."""
    return {pad.stem for pad in map.glob("*.py")}


def _imports(pad: pathlib.Path) -> set:
    """Welke modules dit bestand importeert, ook de imports in een functie."""
    namen = set()
    for knoop in ast.walk(ast.parse(pad.read_text(encoding="utf-8"))):
        if isinstance(knoop, ast.Import):
            namen.update(alias.name.split(".")[0] for alias in knoop.names)
        elif isinstance(knoop, ast.ImportFrom) and knoop.module and knoop.level == 0:
            namen.add(knoop.module.split(".")[0])
    return namen


def _nodig(map: pathlib.Path, startpunten) -> set:
    """Alle eigen modules die je nodig hebt om de startpunten te importeren."""
    eigen = _eigen_modules(map)
    nodig, te_doen = set(), [naam for naam in startpunten if naam in eigen]
    while te_doen:
        naam = te_doen.pop()
        if naam in nodig:
            continue
        nodig.add(naam)
        te_doen.extend(_imports(map / f"{naam}.py") & eigen - nodig)
    return nodig


def _gekopieerd(dockerfile: pathlib.Path) -> list:
    """De bronnen uit elke COPY-regel; de laatste is de bestemming."""
    bronnen = []
    for regel in dockerfile.read_text(encoding="utf-8").splitlines():
        if gevonden := re.match(r"\s*COPY\s+(?!--)(.+)", regel):
            delen = gevonden.group(1).split()
            bronnen.extend(delen[:-1])
    return bronnen


@pytest.mark.parametrize("dockerfile, map, startpunten", IMAGES)
def test_de_dockerfile_kopieert_alles_wat_er_geimporteerd_wordt(
        dockerfile, map, startpunten):
    bronnen = _gekopieerd(dockerfile)
    # fnmatch, zodat de test ook klopt als iemand de lijst ooit vervangt door
    # COPY *.py - dan is er niets meer te vergeten en hoort dit te blijven
    # werken.
    ontbreekt = sorted(
        naam for naam in _nodig(map, startpunten)
        if not any(fnmatch(f"{naam}.py", bron) for bron in bronnen)
    )
    assert not ontbreekt, (
        f"{dockerfile.relative_to(HIER)} kopieert deze modules niet, terwijl "
        f"ze wel geïmporteerd worden: {', '.join(ontbreekt)}. "
        f"Zonder die COPY start de container niet op "
        f"(ModuleNotFoundError) en komt hij in een herstartlus."
    )


def test_de_test_zou_een_vergeten_copy_ook_echt_zien():
    """
    Een controle op de controle: zonder deze test zou de bovenstaande stil
    kunnen slagen omdat er bijvoorbeeld niets in `_nodig` terechtkomt.
    """
    assert "ah_api" in _nodig(HIER, ["app"])
    assert "fetcher" in _nodig(HIER, ["app"])
    # En een lijst zonder ah_api.py hoort niet te matchen.
    bronnen = ["app.py", "fetcher.py", "importqueue.py", "tandoor.py", "urls.py", "./"]
    assert not any(fnmatch("ah_api.py", bron) for bron in bronnen)
