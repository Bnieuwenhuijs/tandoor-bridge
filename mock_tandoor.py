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
import copy
import json
import re
from urllib.parse import unquote, urljoin

from mock_opslag import Fout, Opslag
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

# Breuken zoals Tandoors parse_amount ze leest: "1½" is anderhalf.
_BREUKEN = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75}


class State:
    """Alles wat de nepserver onthoudt. Eén instantie per app."""

    def __init__(self, *, token_prefix="tda_", throttle_after=0, retry_after=5,
                 ai_providers=None, ai_default_provider=None, ai_enabled=True,
                 ai_tegoed=None, ai_gedrag="volgens-tekst", ai_limiet=0,
                 ai_max_invoer=0):
        self.opslag = Opslag()
        self.token_prefix = token_prefix
        # De AI-kant. ai_tegoed=None is onbeperkt; ai_limiet=0 is geen limiet.
        self.ai_providers = list(ai_providers if ai_providers is not None
                                 else [{"id": 1, "name": "Mock-AI", "model_name": "mock"}])
        self.ai_default_provider = ai_default_provider
        self.ai_enabled = ai_enabled
        self.ai_tegoed = ai_tegoed
        self.ai_gedrag = ai_gedrag
        self.ai_limiet = ai_limiet
        # Groter dan dit (in tekens JSON), en het model is niet binnen de
        # 60 seconden van Tandoors nginx klaar: 504. 0 is geen grens.
        self.ai_max_invoer = ai_max_invoer
        self.laatste_ai_body = None
        self.ai_calls = 0
        # AiEndpointThrottle en RecipeImportThrottle zijn allebei een
        # UserRateThrottle zonder eigen scope, dus allebei de cachesleutel
        # throttle_user_<id>: één geschiedenis voor twee limieten. Dit is die
        # geschiedenis, als teller.
        self.gedeelde_teller = 0
        self.throttle_after = throttle_after
        self.retry_after = retry_after
        self.import_calls = 0
        self.last_data_decoded = None   # om byte-gelijkheid te kunnen nakijken
        self.last_data_raw = None
        self.images = {}

    @property
    def recipes(self):
        """Voor wie de recepten als woordenboeken wil, zoals vroeger."""
        return {rid: self.opslag.recept(rid) for rid in self.opslag.recepten}

    def reset(self):
        self.opslag.reset()
        self.images.clear()
        self.import_calls = 0
        self.ai_calls = 0
        self.gedeelde_teller = 0
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


def _hoeveelheid(token):
    """Het getal waar de regel mee begint, of None als het geen getal is."""
    breuk = 0.0
    if token and token[-1] in _BREUKEN:
        breuk, token = _BREUKEN[token[-1]], token[:-1]
        if not token:
            return breuk
    try:
        return float(token.replace(",", ".")) + breuk
    except ValueError:
        return None


def _eten(tokens):
    """parse_food: een laatste stuk tussen haakjes is de notitie."""
    if len(tokens) > 1 and tokens[-1].endswith(")"):
        for i in range(len(tokens) - 1, 0, -1):
            if tokens[i].startswith("("):
                return " ".join(tokens[:i]), " ".join(tokens[i:])[1:-1]
    return " ".join(tokens), ""


def _ingredient(regel):
    """
    De kern van Tandoors ingrediëntparser, cookbook/helper/ingredient_parser.py.

    Niet de hele parser, wel de regel waar een client op kan stuklopen: bij
    drie of meer woorden wordt het tweede woord de eenheid, wat het ook is -
    tenzij het op een komma eindigt. Er is geen lijst met bekende eenheden.
    "400 zwarte bonen" wordt dus 400, eenheid "zwarte", ingrediënt "bonen".
    Een grovere nabootsing met een vaste lijst eenheden liet precies dat
    gedrag weg, en daarmee een fout in de brug die er wel was.

    Ook letterlijk overgenomen, omdat een client er precies daarop struikelt:
    'order' is in het antwoord altijd null (het veld is allow_null en
    get_from_scraper zet het nooit), en 'unit' is null zodra er geen eenheid
    is. Teruggepost op POST /api/recipe/ geeft die order een 400.
    """
    tekst = str(regel).strip()
    tokens = tekst.split()
    hoeveelheid = _hoeveelheid(tokens[0]) if tokens else None
    eenheid, notitie = None, ""
    if hoeveelheid is None or len(tokens) == 1:
        naam, notitie = _eten(tokens)
        hoeveelheid = 0.0
    elif len(tokens) == 2:
        naam = tokens[1]
    elif tokens[1].endswith(","):
        naam, notitie = _eten(tokens[1:])
    else:
        eenheid = tokens[1]
        naam, notitie = _eten(tokens[2:])
    return {
        "amount": float(hoeveelheid),
        "food": {"name": naam.strip()},
        "unit": {"name": eenheid} if eenheid else None,
        "note": notitie,
        "order": None,
        "original_text": tekst,
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


def _ai_sortering(recept, gedrag, opslag):
    """
    Wat de nagebootste AI van een recept maakt.

    Standaard ("volgens-tekst") zet hij elk ingrediënt in de eerste stap
    waarin de naam van de food voorkomt, en anders in de eerste stap. Met de
    andere waarden van gedrag doet hij wat een taalmodel ook echt kan doen:

      splitsen            een stap met meer zinnen wordt een stap per zin; de
                          nieuwe stappen hebben geen id
      splitsen-zelfde-id  idem, maar met het id van de oorspronkelijke stap
                          op álle stukken - gekopieerd, zoals een model dat doet
      dubbel              het eerste ingrediënt staat in twee stappen
      weglaten            het laatste ingrediënt verdwijnt
      zonder-ids          geen enkel id meer, alleen de inhoud
      vreemd-id           één ingrediënt krijgt het id van een ingrediënt uit
                          een ander recept
      ander-bedrag        de hoeveelheid en de naam van het eerste ingrediënt
                          veranderen
      inpakken            het antwoord komt als {"recipe": {...}}
    """
    r = copy.deepcopy(recept)
    stappen = [s for s in (r.get("steps") or []) if isinstance(s, dict)]
    if not stappen:
        return r
    if gedrag in ("splitsen", "splitsen-zelfde-id"):
        nieuw = []
        for stap in stappen:
            zinnen = [z.strip() for z in re.split(r"(?<=[.!?])\s+", stap.get("instruction") or "")
                      if z.strip()]
            if len(zinnen) <= 1:
                nieuw.append(stap)
                continue
            for k, zin in enumerate(zinnen):
                stuk = {**stap, "instruction": zin,
                        "ingredients": stap.get("ingredients") if k == 0 else []}
                if k and gedrag == "splitsen":
                    stuk.pop("id", None)
                nieuw.append(stuk)
        stappen = nieuw
    alle = [i for stap in stappen for i in (stap.get("ingredients") or [])]
    for stap in stappen:
        stap["ingredients"] = []
    for ingredient in alle:
        naam = ((ingredient.get("food") or {}).get("name") or "").strip().lower()
        doel = next((stap for stap in stappen
                     if naam and naam in (stap.get("instruction") or "").lower()), stappen[0])
        doel["ingredients"].append(ingredient)
    if gedrag == "dubbel" and alle and len(stappen) > 1:
        tweede = next(stap for stap in stappen if alle[0] not in stap["ingredients"])
        tweede["ingredients"].append(copy.deepcopy(alle[0]))
    if gedrag == "weglaten" and alle:
        for stap in stappen:
            if alle[-1] in stap["ingredients"]:
                stap["ingredients"].remove(alle[-1])
    if gedrag == "zonder-ids":
        for stap in stappen:
            stap.pop("id", None)
            for ingredient in stap["ingredients"]:
                ingredient.pop("id", None)
    if gedrag == "vreemd-id" and alle:
        eigen = {i.get("id") for i in alle}
        vreemd = next((iid for iid in sorted(opslag.ingredienten, reverse=True)
                       if iid not in eigen), None)
        if vreemd is not None:
            alle[0]["id"] = vreemd
    if gedrag == "ander-bedrag" and alle:
        alle[0]["amount"] = (alle[0].get("amount") or 1) * 10
        if isinstance(alle[0].get("food"), dict):
            alle[0]["food"] = {**alle[0]["food"], "name": "iets anders"}
    r["steps"] = stappen
    return r


def create_app(*, throttle_after=0, retry_after=5, token_prefix="tda_", **ai):
    """
    ai: ai_providers, ai_default_provider, ai_enabled, ai_tegoed, ai_gedrag en
    ai_limiet - zie State. Tijdens een test te veranderen via POST /__mock/ai.
    """
    app = Flask(__name__)
    state = State(throttle_after=throttle_after, retry_after=retry_after,
                  token_prefix=token_prefix, **ai)
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
        state.gedeelde_teller += 1

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
                for r in state.opslag.recepten.values()
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
        # Die 400 komt uit de opslag (mock_opslag.py), net als bij Tandoor uit
        # de geneste serializer.
        try:
            recept_id = state.opslag.maak_recept(body)
        except Fout as e:
            return jsonify(e.body), e.status
        return jsonify(state.opslag.recept(recept_id)), 201

    @app.get("/api/recipe/")
    def list_recipes():
        # RecipePagination: standaard 25, hooguit 100 per pagina.
        try:
            grootte = min(max(int(request.args.get("page_size", 25)), 1), 100)
            pagina = max(int(request.args.get("page", 1)), 1)
        except ValueError:
            return jsonify({"detail": "Invalid page."}), 404
        ids = sorted(state.opslag.recepten)
        stuk = ids[(pagina - 1) * grootte: pagina * grootte]
        if not stuk and pagina > 1:
            return jsonify({"detail": "Invalid page."}), 404
        volgende = None
        if pagina * grootte < len(ids):
            volgende = f"{request.base_url}?page={pagina + 1}&page_size={grootte}"
        return jsonify({"count": len(ids), "next": volgende, "previous": None,
                        "results": [state.opslag.overzicht(i) for i in stuk]}), 200

    @app.get("/api/recipe/<int:recipe_id>/")
    def get_recipe(recipe_id):
        if recipe_id not in state.opslag.recepten:
            return jsonify({"detail": "No Recipe matches the given query."}), 404
        return jsonify(state.opslag.recept(recipe_id)), 200

    @app.patch("/api/recipe/<int:recipe_id>/")
    def patch_recipe(recipe_id):
        if recipe_id not in state.opslag.recepten:
            return jsonify({"detail": "No Recipe matches the given query."}), 404
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(state.opslag.werk_bij(recipe_id, body, partial=True)), 200
        except Fout as e:
            return jsonify(e.body), e.status

    @app.put("/api/recipe/<int:recipe_id>/image/")
    def set_image(recipe_id):
        if recipe_id not in state.opslag.recepten:
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
        state.opslag.zet_afbeelding(recipe_id, f"/media/recipes/{recipe_id}{naam[naam.rfind('.'):]}")
        return jsonify(state.opslag.recept(recipe_id)), 200

    # ------------------------------------------------ AI

    def _provider_uit(provider):
        # AiProviderSerializer: api_key is write_only en komt nooit terug.
        return {"id": provider["id"], "name": provider.get("name", ""),
                "description": provider.get("description", ""),
                "model_name": provider.get("model_name", ""), "url": provider.get("url"),
                "log_credit_cost": True, "space": provider.get("space", 1),
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}

    def _kan_ai():
        """can_perform_ai_request() uit cookbook/helper/ai_helper.py."""
        return state.ai_enabled and (state.ai_tegoed is None or state.ai_tegoed > 0)

    @app.get("/api/space/current/")
    def space_current():
        standaard = next((p for p in state.ai_providers
                          if p["id"] == state.ai_default_provider), None)
        return jsonify({
            "id": 1, "name": "Thuis", "ai_enabled": state.ai_enabled,
            "ai_credits_monthly": 0,
            "ai_credits_balance": 1000 if state.ai_tegoed is None else state.ai_tegoed,
            "ai_monthly_credits_used": 0,
            "ai_default_provider": _provider_uit(standaard) if standaard else None,
        }), 200

    @app.get("/api/ai-provider/")
    def ai_providers():
        return jsonify({"count": len(state.ai_providers), "next": None, "previous": None,
                        "results": [_provider_uit(p) for p in state.ai_providers]}), 200

    @app.post("/api/ai-step-sort/")
    def ai_step_sort():
        # De volgorde van AiStepSortView.post: eerst de throttle (die telt een
        # toegelaten aanvraag mee, ook als er daarna een 400 volgt), dan de
        # serializer, dan provider, dan tegoed, en pas dan het model.
        if state.ai_limiet and state.gedeelde_teller >= state.ai_limiet:
            antwoord = jsonify({"detail": f"Request was throttled. Expected available "
                                          f"in {state.retry_after} seconds."})
            antwoord.status_code = 429
            antwoord.headers["Retry-After"] = str(state.retry_after)
            return antwoord
        state.gedeelde_teller += 1

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"non_field_errors": ["Invalid data."]}), 400
        state.laatste_ai_body = body
        te_groot = state.ai_max_invoer and len(json.dumps(body)) > state.ai_max_invoer
        if state.ai_gedrag == "traag" or te_groot:
            # Wat een echte Tandoor met een groot recept doet: de nginx in de
            # container wacht 60 seconden en geeft dan zijn eigen 504-pagina.
            # Het model rekent intussen door, en dat kost tegoed.
            state.ai_calls += 1
            if state.ai_tegoed is not None:
                state.ai_tegoed -= 1
            return ("<html><head><title>504 Gateway Time-out</title></head><body>"
                    "<center><h1>504 Gateway Time-out</h1></center><hr><center>nginx"
                    "</center></body></html>", 504, {"Content-Type": "text/html"})
        # RecipeSerializer(partial=True): dezelfde nulls die create weigert.
        if "servings" in body and body["servings"] is None:
            return jsonify({"servings": ["This field may not be null."]}), 400
        for stap in body.get("steps") or []:
            for ingredient in (stap or {}).get("ingredients") or []:
                if isinstance(ingredient, dict) and "order" in ingredient \
                        and ingredient["order"] is None:
                    return jsonify({"steps": [{"ingredients": [
                        {"order": ["This field may not be null."]}]}]}), 400

        provider = request.args.get("provider", "")
        if not provider.isdigit():
            return jsonify({"error": True, "msg": "You must select an AI provider "
                                                  "to perform your request."}), 400
        if not _kan_ai():
            return jsonify({"error": True, "msg": "You don't have any credits remaining "
                                                  "to use AI or AI features are not "
                                                  "enabled for your space."}), 400
        if not any(p["id"] == int(provider) for p in state.ai_providers):
            # De view zoekt de provider op met .first() en gebruikt hem
            # ongecontroleerd: None.api_key, en dat wordt de algemene 500.
            return jsonify({"error": True, "msg": "An unexpected error occurred while "
                                                  "processing your AI request. \n\n"
                                                  "'NoneType' object has no attribute "
                                                  "'api_key'"}), 500

        gedrag = state.ai_gedrag
        if gedrag == "fout":
            return jsonify({"error": True, "msg": "The AI could not process your "
                                                  "request. \n\nRequest timed out"}), 400
        if gedrag == "kapot":
            return jsonify({"error": True, "msg": "An unexpected error occurred while "
                                                  "processing your AI request. \n\n"
                                                  "Expecting value: line 1 column 1"}), 500
        state.ai_calls += 1
        if state.ai_tegoed is not None:
            state.ai_tegoed -= 1
        # Het antwoord van het model gaat ongecontroleerd terug: in de view
        # staat "# TODO validate by loading/dumping using serializer ?".
        resultaat = _ai_sortering(body, gedrag, state.opslag)
        if gedrag == "inpakken":
            resultaat = {"recipe": resultaat}
        return jsonify(resultaat), 200

    @app.post("/__mock/ai")
    def mock_ai():
        """Verander het AI-gedrag midden in een test."""
        body = request.get_json(silent=True) or {}
        for sleutel in ("ai_gedrag", "ai_tegoed", "ai_enabled", "ai_limiet",
                        "ai_default_provider", "ai_providers", "ai_max_invoer"):
            if sleutel in body:
                setattr(state, sleutel, body[sleutel])
        if body.get("teller_terug"):
            state.gedeelde_teller = 0
        return jsonify({"ok": True}), 200

    # ------------------------------------------------ inkijk voor tests

    @app.get("/__mock/state")
    def mock_state():
        return jsonify({
            "recipes": [state.opslag.recept(i) for i in sorted(state.opslag.recepten)],
            "tellingen": state.opslag.tellingen(),
            "ai_calls": state.ai_calls,
            "laatste_ai_body": state.laatste_ai_body,
            "ai_tegoed": state.ai_tegoed,
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
