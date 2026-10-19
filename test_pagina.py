"""
Tests voor de JavaScript in de pagina, via node.

De pagina heeft geen buildstap en geen framework, dus er is ook geen
testopstelling voor. Deze tests laden het script-blok uit index.html in node
met een minimale nep-DOM, en controleren het gedrag dat niet te zien is door
ernaar te kijken: wat er gebeurt als de brug even niet antwoordt.

Staat er geen node, dan slaan ze over. De pytest-suite blijft dus bruikbaar
zonder node erbij te installeren; in CI (ubuntu-latest) draaien ze wel.
"""

import re
import shutil
import subprocess
import pathlib

import pytest

NODE = shutil.which("node") or shutil.which("nodejs")
PAGINA = pathlib.Path(__file__).parent / "templates" / "index.html"

pytestmark = pytest.mark.skipif(NODE is None, reason="node niet beschikbaar")

SHIM = """
'use strict';
const fs = require('fs');
const vm = require('vm');

function fakeEl(naam) {
  return {
    naam, children: [], textContent: '', innerHTML: '', className: '',
    hidden: false, disabled: false, value: '', files: [],
    style: {}, classList: { add() {}, remove() {} },
    appendChild(kind) { this.children.push(kind); return kind; },
    addEventListener() {}, removeAttribute() {},
    setAttribute(naam, waarde) { this[naam] = waarde; },
  };
}

const elementen = {};
globalThis.document = {
  getElementById(id) { return elementen[id] || (elementen[id] = fakeEl(id)); },
  createElement(tag) { return fakeEl(tag); },
  createTextNode(tekst) { return { textContent: tekst }; },
};
globalThis.FormData = class { append() {} };
globalThis.location = {
  origin: 'http://192.168.1.130:8095',
  href: 'http://192.168.1.130:8095/',
};

const ingepland = [];
const echteSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms) => { ingepland.push(ms); return ingepland.length; };
globalThis.clearTimeout = () => {};

let antwoordGever = () => { throw new Error('geen netwerk'); };
globalThis.fetch = async (pad, opties) => antwoordGever(pad, opties);

const bron = fs.readFileSync(process.argv[2], 'utf8')
  + '\\n;globalThis.__t = { ververs, vraag };';
vm.runInThisContext(bron);

function wacht() {
  return new Promise((klaar) => echteSetTimeout(klaar, 30));
}

(async () => {
  const uitkomst = {};

  // 1. Een fetch die klapt mag geen afgewezen belofte geven.
  const data = await globalThis.__t.vraag('/api/items');
  uitkomst.foutmelding = data.error;

  // 2. En de verversing moet zich opnieuw inplannen, ook na die fout.
  ingepland.length = 0;
  await globalThis.__t.ververs();
  await wacht();
  uitkomst.na_fout_ingepland = ingepland.slice();

  // 3. Het gewone geval: niets onderweg, dus rustig verversen.
  antwoordGever = () => ({
    ok: true,
    json: async () => ({ items: [], counts: { klaar: 2 } }),
  });
  ingepland.length = 0;
  await globalThis.__t.ververs();
  await wacht();
  uitkomst.rustig_ingepland = ingepland.slice();
  uitkomst.statusregel = document.getElementById('statusregel').textContent;

  // 4. Werk onderweg, dus vaker kijken.
  antwoordGever = () => ({
    ok: true,
    json: async () => ({ items: [], counts: { wachtend: 3 } }),
  });
  ingepland.length = 0;
  await globalThis.__t.ververs();
  await wacht();
  uitkomst.druk_ingepland = ingepland.slice();

  // 5. De bookmarklet moet naar deze host wijzen en een formulier posten.
  uitkomst.bookmarklet = elementen['bookmarklet']
    ? (elementen['bookmarklet'].href || '') : '';

  console.log(JSON.stringify(uitkomst));
})();
"""


def draai_pagina(tmp_path) -> dict:
    import json

    js = re.findall(r"<script>(.*?)</script>", PAGINA.read_text(encoding="utf-8"), re.S)
    assert len(js) == 1, "verwacht precies één script-blok in de pagina"
    scriptbestand = tmp_path / "pagina.js"
    scriptbestand.write_text(js[0], encoding="utf-8")
    shim = tmp_path / "shim.cjs"
    shim.write_text(SHIM, encoding="utf-8")

    uit = subprocess.run([NODE, str(shim), str(scriptbestand)],
                         capture_output=True, text=True, timeout=60)
    assert uit.returncode == 0, uit.stderr
    return json.loads(uit.stdout.strip().splitlines()[-1])


def test_javascript_is_geldig(tmp_path):
    js = re.findall(r"<script>(.*?)</script>", PAGINA.read_text(encoding="utf-8"), re.S)
    bestand = tmp_path / "pagina.js"
    bestand.write_text(js[0], encoding="utf-8")
    uit = subprocess.run([NODE, "--check", str(bestand)], capture_output=True, text=True)
    assert uit.returncode == 0, uit.stderr


def test_een_wegvallende_brug_blokkeert_de_knoppen_niet(tmp_path):
    """
    Een afgewezen fetch liet de import- en uploadknoppen voorgoed
    uitgeschakeld staan, omdat de belofte klapte voordat ze weer aan gingen.
    """
    uitkomst = draai_pagina(tmp_path)
    assert uitkomst["foutmelding"] == "geen verbinding met de brug"


def test_de_verversing_stopt_niet_na_een_fout(tmp_path):
    """
    Dit is de nare: één mislukte verversing - de container herstart, jij hebt
    de pagina open - en de lijst bleef voorgoed stilstaan. Dan lijkt de
    wachtrij vast te zitten terwijl hij gewoon doorloopt.
    """
    uitkomst = draai_pagina(tmp_path)
    assert uitkomst["na_fout_ingepland"], "er is niets opnieuw ingepland na een fout"


def test_de_verversing_past_haar_tempo_aan(tmp_path):
    uitkomst = draai_pagina(tmp_path)
    assert uitkomst["rustig_ingepland"] == [15000]   # niets onderweg
    assert uitkomst["druk_ingepland"] == [3000]      # werk in de wachtrij
    assert "2 geïmporteerd" in uitkomst["statusregel"]


def test_de_bookmarklet_post_een_formulier_naar_deze_host(tmp_path):
    """
    Een formulier en geen fetch, en dat is geen smaakkwestie: vanaf een
    https-pagina blokkeert de browser een fetch naar http://<nas> als mixed
    content, terwijl een formulier-submit navigatie is en wel mag. En de
    actie moet naar de host wijzen waarmee je nu verbonden bent, zodat hij
    zowel via het LAN als via Tailscale werkt.
    """
    uitkomst = draai_pagina(tmp_path)
    code = uitkomst["bookmarklet"]

    assert code.startswith("javascript:")
    assert "http://192.168.1.130:8095/bookmarklet" in code
    assert "f.method='POST'" in code
    assert "fetch(" not in code
    # En hij stuurt de pagina mee die de browser zelf ziet.
    assert "document.documentElement.outerHTML" in code
    assert "a('url',location.href)" in code
