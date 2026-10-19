"""
Een nagebouwde Tandoor-API, om de brug te testen zonder je echte Tandoor.

Zelf draaien:

    python mock_tandoor.py                  # op http://127.0.0.1:8099
    python mock_tandoor.py --port 9000 --throttle-after 2

en dan de brug starten met TANDOOR_URL=http://127.0.0.1:8099 en een
TANDOOR_TOKEN die met tda_ begint. Je echte Tandoor blijft onaangeroerd.

Wat hier nagebouwd is, is bewust niet "iets dat JSON teruggeeft" maar het
gedrag dat in tag 2.6.9 van TandoorRecipes/recipes staat, inclusief de drie
dingen die stil kunnen mislukken:

* unquote() over het veld 'data', zodat een brug die z'n HTML niet beschermt
  hier dezelfde verminkte pagina aanbiedt als bij het echte Tandoor;
* de bron-URL uit <link rel="canonical"> met urljoin() tegen
  'https://urlnotfound.none', zodat een ontbrekende canonical hier net zo
  goed een leeg source_url oplevert;
* is_file_type_allowed(): een upload wordt op de bestandsnaam gekeurd, niet
  op de inhoud.

Wat hier niet nagebouwd is: het echte ontleden van ingrediënten. De
hoeveelheid/eenheid-splitsing hieronder is een grove benadering, genoeg om
antwoorden van de juiste vorm te geven. De brug raakt ingrediënten toch niet
aan - dat is precies het punt.
"""

import argparse
import json
import re
from urllib.parse import unquote, urljoin

from flask import Flask, jsonify, request

# Zoals cookbook/helper/image_processing.py, image_only=True.
ALLOWED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# De org_url die RecipeUrlImportView meegeeft zodra er data is.
URL_NOT_FOUND = "https://urlnotfound.none"

_LD_JSON_RE = re.compile(
    r"""<script[^>]*type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.I | re.S,
)
_CANONICAL_RE = re.compile(r"""<link\b[^>]*\brel\s*=\s*["']?canonical\b["']?[^>]*>""", re.I)
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']*)["']""", re.I)
_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]*property\s*=\s*["']og:image["'][^>]*content\s*=\s*["']([^"']+)["']""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

_EENHEDEN = (
    "gram", "g", "kg", "ml", "l", "liter", "el", "tl", "eetlepel", "theelepel",
    "stuk", "stuks", "teen", "tenen", "snuf", "snufje", "blikje", "blik", "pak",
)
_HOEVEELHEID_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(" + "|".join(_EENHEDEN) + r")?\b\s*(.*)$", re.I)


class State:
    """Alles wat de nepserver onthoudt. Eén instantie per app."""

    def __init__(self, *, token_prefix="tda_", throttle_after=0, retry_after=5):
        self.recipes = {}
        self.next_id = 1
        self.token_prefix = token_prefix
        self.throttle_after = throttle_after
        self.retry_after = retry_after
        self.import_calls = 0
        self.last_data_decoded = None   # om byte-gelijkheid te kunnen nakijken
        self.last_data_raw = None
        self.images = {}

    def reset(self):
        self.recipes.clear()
        self.images.clear()
        self.next_id = 1
        self.import_calls = 0
        self.last_data_decoded = None
        self.last_data_raw = None


def _ld_json_recipes(html):
    """Alle ld+json-blokken die een Recipe zijn, inclusief @graph."""
    for blok in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(blok.strip())
        except ValueError:
            continue
        kandidaten = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            kandidaten = data["@graph"]
        for kandidaat in kandidaten:
            if not isinstance(kandidaat, dict):
                continue
            soort = kandidaat.get("@type") or ""
            soorten = soort if isinstance(soort, list) else [soort]
            if any(str(s).lower() == "recipe" for s in soorten):
                yield kandidaat


def _canonical_url(html, org_url):
    """recipe_scrapers' canonical_url(): de canonical, tegen org_url aan geplakt."""
    if tag := _CANONICAL_RE.search(html):
        if href := _HREF_RE.search(tag.group(0)):
            return urljoin(org_url, href.group(1).strip())
    return org_url


def _ingredient(regel):
    """
    Grove benadering van Tandoors ingrediëntparser; alleen de vorm telt.

    Twee details zijn wél letterlijk overgenomen, omdat een client er precies
    daarop struikelt: 'order' is in het antwoord altijd null (het veld is
    allow_null en get_from_scraper zet het nooit), en 'unit' is null zodra er
    geen eenheid herkend wordt. Teruggepost op POST /api/recipe/ geeft die
    order een 400.
    """
    match = _HOEVEELHEID_RE.match(str(regel))
    if not match:
        return {"amount": 0.0, "food": {"name": str(regel).strip()}, "unit": None,
                "note": "", "order": None, "original_text": str(regel)}
    hoeveelheid, eenheid, rest = match.groups()
    return {
        "amount": float(hoeveelheid.replace(",", ".")),
        "food": {"name": (rest or "").strip()},
        "unit": {"name": eenheid.lower()} if eenheid else None,
        "note": "",
        "order": None,
        "original_text": str(regel),
    }


def _minuten(waarde):
    """ISO 8601-duur (PT30M) naar minuten, zoals get_minutes() dat doet."""
    if not waarde:
        return 0
    match = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(waarde), re.I)
    if not match:
        return 0
    uren, minuten = match.groups()
    return int(uren or 0) * 60 + int(minuten or 0)


def _build_recipe(schema, html, source_url):
    """Het recipe-object in de vorm van SourceImportRecipeSerializer."""
    naam = schema.get("name") or ""
    if isinstance(naam, list):
        naam = naam[0] if naam else ""
    if not naam and (titel := _TITLE_RE.search(html)):
        naam = re.sub(r"\s+", " ", titel.group(1)).strip()

    instructies = schema.get("recipeInstructions") or []
    if isinstance(instructies, str):
        stappen_tekst = [instructies]
    else:
        stappen_tekst = [
            (s.get("text") or "") if isinstance(s, dict) else str(s)
            for s in instructies
        ]
    stappen_tekst = [s for s in stappen_tekst if s.strip()]

    ingredienten = [_ingredient(r) for r in (schema.get("recipeIngredient") or [])]

    # Tandoor hangt alle ingrediënten aan de eerste stap.
    stappen = []
    for i, tekst in enumerate(stappen_tekst or [""]):
        stappen.append({
            "instruction": tekst,
            "ingredients": ingredienten if i == 0 else [],
            "show_ingredients_table": True,
        })

    porties = schema.get("recipeYield")
    if isinstance(porties, list):
        porties = porties[0] if porties else None
    aantal = re.search(r"\d+", str(porties or ""))

    afbeelding = schema.get("image")
    if isinstance(afbeelding, list):
        afbeelding = afbeelding[0] if afbeelding else None
    if isinstance(afbeelding, dict):
        afbeelding = afbeelding.get("url")
    if not afbeelding and (og := _OG_IMAGE_RE.search(html)):
        afbeelding = og.group(1)

    # get_from_scraper() voegt het domein van de bron-URL als trefwoord toe.
    trefwoorden = []
    if source_url:
        trefwoorden.append({"id": None, "name": source_url.split("//")[-1].split("/")[0]})

    return {
        "steps": stappen,
        "internal": True,
        "source_url": source_url,
        "name": naam[:128],
        "description": (schema.get("description") or "")[:256],
        "servings": int(aantal.group()) if aantal else None,
        "servings_text": "" if porties else "None",
        "working_time": _minuten(schema.get("prepTime")),
        "waiting_time": _minuten(schema.get("cookTime")),
        "image_url": afbeelding,
        "keywords": trefwoorden,
        "properties": [],
    }


def _from_source_response(**velden):
    """De defaults van RecipeFromSourceResponseSerializer."""
    antwoord = {"recipe": None, "recipe_id": None, "images": [],
                "error": False, "msg": "", "duplicates": []}
    antwoord.update(velden)
    return antwoord


def create_app(*, throttle_after=0, retry_after=5, token_prefix="tda_"):
    app = Flask(__name__)
    state = State(throttle_after=throttle_after, retry_after=retry_after,
                  token_prefix=token_prefix)
    app.state = state

    def auth_fout():
        """
        Let op de statuscode: de echte Tandoor geeft nooit een 401.

        SessionAuthentication staat vooraan in
        DEFAULT_AUTHENTICATION_CLASSES en z'n authenticate_header() geeft
        None, waarna DRF elke NotAuthenticated tot een 403 herschrijft. Een
        ontbrekend token, een verkeerd token en een token zonder de juiste
        scope zijn voor een client dus niet van elkaar te onderscheiden.
        """
        kop = request.headers.get("Authorization", "")
        if not kop.startswith("Bearer "):
            return jsonify({"detail": "Authentication credentials were not provided."}), 403
        token = kop[len("Bearer "):].strip()
        if not token.startswith(state.token_prefix):
            return jsonify({"detail": "Authentication credentials were not provided."}), 403
        return None

    @app.before_request
    def _controleer_token():
        if request.path.startswith("/__mock"):
            return None
        return auth_fout()

    # ------------------------------------------------ recipe-from-source

    @app.post("/api/recipe-from-source/")
    def recipe_from_source():
        state.import_calls += 1
        if state.throttle_after and state.import_calls > state.throttle_after:
            antwoord = jsonify({
                "detail": f"Request was throttled. Expected available in {state.retry_after} seconds."
            })
            antwoord.status_code = 429
            antwoord.headers["Retry-After"] = str(state.retry_after)
            return antwoord

        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        state.last_data_raw = body.get("data") or ""

        # Het serializerveld draait vóór de unquote, en doet twee dingen die
        # een client makkelijk over het hoofd ziet.
        if "\x00" in state.last_data_raw:
            return jsonify({"data": ["Null characters are not allowed."]}), 400
        try:
            state.last_data_raw.encode("utf-8")
        except UnicodeEncodeError:
            return jsonify({"data": ["Surrogate characters are not allowed."]}), 400
        if len(url) > 4096:
            return jsonify({"url": ["Ensure this field has no more than 4096 characters."]}), 400

        # trim_whitespace staat standaard aan op een DRF CharField.
        gestript = state.last_data_raw.strip()
        # En dit is wat de echte view doet; de reden dat de brug % beschermt.
        data = unquote(gestript)
        state.last_data_decoded = data

        if not url and not data:
            return jsonify(_from_source_response(error=True, msg="Nothing to do.")), 400

        if url and not data:
            # De brug hoort hier nooit te komen: dan haalt Tandoor zelf op.
            return jsonify(_from_source_response(
                error=True,
                msg="The requested page could not be loaded. The server returned 403.",
            )), 400

        schemas = list(_ld_json_recipes(data))
        if not schemas:
            return jsonify(_from_source_response(
                error=True, msg="No usable data could be found.")), 400

        # De data-tak geeft de scraper altijd urlnotfound.none mee; alleen als
        # er géén url is meegestuurd wordt er opnieuw gescrapet met de url uit
        # het schema.
        org_url = URL_NOT_FOUND
        if not url and (gevonden := schemas[0].get("url")):
            org_url = gevonden

        bron = _canonical_url(data, org_url)
        if bron == URL_NOT_FOUND or not bron:
            bron = ""

        recept = _build_recipe(schemas[0], data, bron)

        # Deze controle staat in de echte view buiten de url/data-splitsing,
        # dus hij werkt ook als we onze eigen HTML meesturen.
        duplicates = []
        if url:
            duplicates = [
                {"id": r["id"], "name": r["name"]}
                for r in state.recipes.values()
                if (r.get("source_url") or "") == url
            ]

        afbeeldingen = [recept["image_url"]] if recept.get("image_url") else []
        return jsonify(_from_source_response(
            recipe=recept, images=afbeeldingen, duplicates=duplicates)), 200

    # ------------------------------------------------ recepten

    @app.post("/api/recipe/")
    def create_recipe():
        body = request.get_json(silent=True) or {}
        if not (body.get("name") or "").strip():
            return jsonify({"name": ["This field is required."]}), 400

        # Recipe.servings is IntegerField(default=1), niet nullable - terwijl
        # recipe-from-source hier null teruggeeft als de pagina geen porties
        # noemt.
        if "servings" in body and body["servings"] is None:
            return jsonify({"servings": ["This field may not be null."]}), 400

        # Ingredient.order is IntegerField(default=0), ook niet nullable,
        # terwijl het antwoord van recipe-from-source er altijd null in zet.
        # Dit is de 400 die je krijgt als je dat antwoord onbewerkt terugpost.
        for stap in body.get("steps") or []:
            for ingredient in (stap or {}).get("ingredients") or []:
                if "order" in ingredient and ingredient["order"] is None:
                    return jsonify({"steps": [{"ingredients": [
                        {"order": ["This field may not be null."]}]}]}), 400

        recept = {sleutel: waarde for sleutel, waarde in body.items()
                  if sleutel not in ("id", "image", "created_by", "created_at",
                                     "food_properties", "image_url")}
        recept["id"] = state.next_id
        recept["image"] = None
        recept.setdefault("source_url", "")
        state.recipes[state.next_id] = recept
        state.next_id += 1
        return jsonify(recept), 201

    @app.get("/api/recipe/")
    def list_recipes():
        recepten = list(state.recipes.values())
        return jsonify({"count": len(recepten), "next": None, "previous": None,
                        "results": recepten[:int(request.args.get("page_size", 25))]}), 200

    @app.get("/api/recipe/<int:recipe_id>/")
    def get_recipe(recipe_id):
        if recipe_id not in state.recipes:
            return jsonify({"detail": "No Recipe matches the given query."}), 404
        return jsonify(state.recipes[recipe_id]), 200

    @app.patch("/api/recipe/<int:recipe_id>/")
    def patch_recipe(recipe_id):
        if recipe_id not in state.recipes:
            return jsonify({"detail": "No Recipe matches the given query."}), 404
        body = request.get_json(silent=True) or {}
        for sleutel, waarde in body.items():
            if sleutel not in ("id", "image", "created_by", "created_at"):
                state.recipes[recipe_id][sleutel] = waarde
        return jsonify(state.recipes[recipe_id]), 200

    @app.put("/api/recipe/<int:recipe_id>/image/")
    def set_image(recipe_id):
        if recipe_id not in state.recipes:
            return jsonify({"detail": "No Recipe matches the given query."}), 404

        # parser_classes=[MultiPartParser]: een JSON-body komt hier niet in.
        if not request.content_type or "multipart/form-data" not in request.content_type:
            return jsonify({
                "detail": f'Unsupported media type "{request.content_type}" in request.'
            }), 415

        bestand = request.files.get("image")
        if bestand is None:
            if request.form.get("image_url"):
                # De echte server haalt die zelf op via safe_request. Daar is
                # de brug juist voor; hij hoort deze tak nooit te gebruiken.
                return jsonify({"detail": "mock: image_url wordt niet opgehaald"}), 400
            return jsonify({"image": ["No file was submitted."]}), 400

        naam = (bestand.filename or "").lower()
        if not naam.endswith(ALLOWED_IMAGE_SUFFIXES):
            # is_file_type_allowed() keurt op naam. De serializer geeft dan
            # None terug uit update(), en daar rekent de view niet op: het
            # resultaat is een 500, geen nette 400. Een upload met een naam
            # zonder achtervoegsel kost dus een serverfout.
            return jsonify({"detail": "A server error occurred."}), 500

        state.images[recipe_id] = (bestand.filename, bestand.read())
        state.recipes[recipe_id]["image"] = f"/media/recipes/{recipe_id}{naam[naam.rfind('.'):]}"
        return jsonify(state.recipes[recipe_id]), 200

    # ------------------------------------------------ inkijk voor tests

    @app.get("/__mock/state")
    def mock_state():
        return jsonify({
            "recipes": list(state.recipes.values()),
            "import_calls": state.import_calls,
            "images": {str(k): v[0] for k, v in state.images.items()},
            "last_data_decoded": state.last_data_decoded,
            "last_data_raw": state.last_data_raw,
        }), 200

    @app.post("/__mock/reset")
    def mock_reset():
        state.reset()
        return jsonify({"ok": True}), 200

    return app


def main():
    parser = argparse.ArgumentParser(description="Nagebouwde Tandoor-API voor tests")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--throttle-after", type=int, default=0,
                        help="aantal imports voordat 429 komt (0 = nooit)")
    parser.add_argument("--retry-after", type=int, default=5,
                        help="seconden in de Retry-After-kop bij een 429")
    args = parser.parse_args()
    app = create_app(throttle_after=args.throttle_after, retry_after=args.retry_after)
    print(f"Nagebouwde Tandoor op http://{args.host}:{args.port} "
          f"(elk token dat met tda_ begint werkt)")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
