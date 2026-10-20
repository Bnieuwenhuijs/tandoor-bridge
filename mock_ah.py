"""
Een nagebouwde Albert Heijn-API om tegen te testen.

Niet alleen de vormen, maar ook het gedrag waar ah_api.py op rekent:

  * een anoniem token, met expires_in, dat je nodig hebt voor /graphql;
  * een GraphQL-endpoint dat de gevraagde velden echt nakijkt en op een
    onbekend veld een validatiefout teruggeeft zoals graphql-java die
    schrijft - zonder "data", met het pad in de foutcode;
  * teksten met HTML-entiteiten en losse <a>-tags erin, want zo komt het
    er bij AH ook uit.

Het schema is instelbaar, zodat de tests kunnen laten zien wat er gebeurt als
AH een veld niet (meer) kent: de brug moet dan een kaler recept leveren, geen
foutmelding. Zelf draaien:

    python mock_ah.py            # luistert op 8098
    AH_API_URL=http://127.0.0.1:8098 python smoketest.py --ah-only ...
"""

from __future__ import annotations

import re

from flask import Flask, jsonify, request

# Welke velden welk type heeft. Dit is de optelsom van wat twee openbare
# clients van api.ah.nl gebruiken; welke daarvan er vandaag echt zijn weten we
# niet, en dat is precies waarom ah_api.py velden kan laten vallen.
SCHEMA = {
    "Recipe": {
        "id": None, "title": None, "description": None, "cookTime": None,
        "href": None, "nutriScore": None, "publishedAt": None,
        "courses": None, "cuisines": None,
        "servings": "Servings", "rating": "Rating", "images": "Image",
        "ingredients": "RecipeIngredient", "preparation": "Preparation",
        "tips": "Tip",
    },
    "Servings": {"number": None, "type": None},
    "Rating": {"average": None, "count": None},
    "Image": {"url": None, "width": None, "height": None, "rendition": None},
    "RecipeIngredient": {"id": None, "quantity": None, "text": None,
                         "name": "IngredientName"},
    "IngredientName": {"singular": None, "plural": None},
    "Preparation": {"steps": None, "summary": None},
    "Tip": {"type": None, "value": None},
}

RECEPT = {
    "id": 1194169,
    "title": "Traybake met kip, paprika, zwarte bonen en chipotle",
    "description": "Alles op &eacute;&eacute;n plaat. Lees ook onze "
                   '<a href=https://www.ah.nl/allerhande/traybake>tips</a>.',
    "cookTime": 20,
    "href": "/allerhande/recept/R-R1194169/traybake-met-kip",
    "nutriScore": "B",
    "publishedAt": "2024-03-01T00:00:00Z",
    "courses": ["hoofdgerecht"],
    "cuisines": ["mexicaans"],
    "servings": {"number": 4, "type": "PERSON"},
    "rating": {"average": 4.3, "count": 118},
    "images": [
        {"url": "https://static.ah.nl/dam/traybake.jpg?width=400",
         "width": 400, "height": 300, "rendition": "small"},
        {"url": "https://static.ah.nl/dam/traybake.jpg?width=1200",
         "width": 1200, "height": 900, "rendition": "large"},
    ],
    "ingredients": [
        {"id": 1, "quantity": 600, "text": "600 g kipfilet",
         "name": {"singular": "kipfilet", "plural": None}},
        {"id": 2, "quantity": 2, "text": "2 rode paprika&#39;s",
         "name": {"singular": "rode paprika", "plural": "rode paprika's"}},
        {"id": 3, "quantity": 1, "text": "1&#189; el chipotlepasta",
         "name": {"singular": "chipotlepasta", "plural": None}},
        {"id": 4, "quantity": 400, "text": "400 g zwarte bonen",
         "name": {"singular": "zwarte boon", "plural": "zwarte bonen"}},
    ],
    "preparation": {
        "steps": [
            "Verwarm de oven voor op 200 &deg;C.",
            "Meng de kip met de chipotlepasta en schep de paprika erdoor.",
            "Bak 30 minuten in de oven en schep de bonen er het laatste "
            "kwartier door.",
        ],
        "summary": ["Verwarm de oven, meng alles en bak 30 minuten."],
    },
    "tips": [
        {"type": "algemeen", "value": "Lekker met een lik zure room."},
    ],
}

# Velden, accolades en namen; argumenten tussen ronde haken slaan we over.
_TOKEN_RE = re.compile(r"\(.*?\)|[{}]|[A-Za-z_][A-Za-z0-9_]*", re.S)


def _parse_selectie(tokens: list, i: int) -> tuple:
    """Lees een { ... } blok; geeft een boom {veld: subboom of None} terug."""
    boom = {}
    while i < len(tokens):
        token = tokens[i]
        if token == "}":
            return boom, i + 1
        if token == "{":
            i += 1                                   # losse accolade, overslaan
            continue
        naam = token
        if i + 1 < len(tokens) and tokens[i + 1] == "{":
            sub, i = _parse_selectie(tokens, i + 2)
            boom[naam] = sub
        else:
            boom[naam] = None
            i += 1
    return boom, i


def _gevraagde_velden(query: str) -> dict:
    """De boom onder recipe(...) { ... }, of {} als die er niet staat."""
    tokens = [t for t in _TOKEN_RE.findall(query) if not t.startswith("(")]
    boom, _ = _parse_selectie(tokens, 0)
    # query Recept { recipe { ... } } -> twee niveaus naar beneden zoeken.
    for waarde in list(boom.values()) + [boom]:
        if isinstance(waarde, dict) and isinstance(waarde.get("recipe"), dict):
            return waarde["recipe"]
    return {}


def _controleer(boom: dict, typenaam: str, pad: str, fouten: list, schema: dict):
    velden = schema.get(typenaam) or {}
    for naam, sub in boom.items():
        if naam == "__typename":
            continue
        if naam not in velden:
            fouten.append((f"{pad}/{naam}", naam, typenaam))
            continue
        if isinstance(sub, dict):
            _controleer(sub, velden[naam], f"{pad}/{naam}", fouten, schema)


def _snij(waarde, boom: dict):
    """Geef alleen de gevraagde velden terug, net als een echte GraphQL-server."""
    if isinstance(waarde, list):
        return [_snij(deel, boom) for deel in waarde]
    if not isinstance(waarde, dict):
        return waarde
    uit = {}
    for naam, sub in boom.items():
        if naam not in waarde:
            uit[naam] = None
        elif isinstance(sub, dict):
            uit[naam] = _snij(waarde[naam], sub)
        else:
            uit[naam] = waarde[naam]
    return uit


def create_app(*, schema=None, recept=None, token_leeft=7199,
               oude_foutvorm=False, geen_token=False, fout_status=400,
               suggesties=False, weigering_als_graphql=False,
               half_antwoord=False, leeg_antwoord=False) -> Flask:
    """
    schema          : welke velden bestaan (standaard: alles uit SCHEMA)
    oude_foutvorm   : 'Cannot query field "x" on type "Y"' in plaats van de
                      graphql-java-vorm
    geen_token      : het tokenendpoint geeft een antwoord zonder access_token
    fout_status     : status bij een afgewezen vraag; AH stuurt 400, de meeste
                      GraphQL-servers 200
    suggesties      : zet er 'Did you mean "description"?' achter, zoals
                      graphql-js doet - met een veld dat wél bestaat
    weigering_als_graphql
                    : een geweigerd token krijgt 401 met {"errors": [...]},
                      de gewone vorm van een GraphQL-gateway, in plaats van
                      {"message": ...}
    half_antwoord   : het recept komt, maar met een melding in "errors" die
                      niet over een veld gaat (een resolver die faalde)
    leeg_antwoord   : /graphql antwoordt {} - geen data, geen errors
    """
    schema = schema or SCHEMA
    recept = recept if recept is not None else RECEPT
    app = Flask(__name__)
    app.config["gezien"] = []

    @app.post("/mobile-auth/v1/auth/token/anonymous")
    def anoniem():
        app.config["gezien"].append(("token", request.get_json(silent=True) or {}))
        if geen_token:
            return jsonify({"refresh_token": "r"}), 200
        if (request.get_json(silent=True) or {}).get("clientId") is None:
            return jsonify({"error": "clientId is required"}), 400
        return jsonify({"access_token": "mock-anoniem-token",
                        "refresh_token": "mock-refresh",
                        "expires_in": token_leeft})

    @app.post("/graphql")
    def graphql():
        if request.headers.get("Authorization") != "Bearer mock-anoniem-token":
            # AH geeft 401 als het token ontbreekt of verlopen is.
            if weigering_als_graphql:
                return jsonify({"errors": [{"message": "Unauthorized",
                                            "extensions": {"code": "UNAUTHENTICATED"}}]}), 401
            return jsonify({"message": "Unauthorized"}), 401
        body = request.get_json(silent=True) or {}
        query = str(body.get("query") or "")
        app.config["gezien"].append(("graphql", query))
        if leeg_antwoord:
            return jsonify({})
        gevraagd = _gevraagde_velden(query)
        if not gevraagd:
            return jsonify({"errors": [{"message": "no recipe selection"}]})

        fouten = []
        _controleer(gevraagd, "Recipe", "recipe", fouten, schema)
        if fouten:
            # Zo doet AH het echt: status 400, geen "data", en de uitleg in
            # "errors". De meeste GraphQL-servers antwoorden hier met 200, dus
            # een client die alleen naar de status kijkt gooit die uitleg weg.
            return jsonify({"errors": [_fout(pad, naam, typenaam, oude_foutvorm, suggesties)
                                       for pad, naam, typenaam in fouten]}), fout_status

        nummer = (body.get("variables") or {}).get("id")
        if nummer != recept.get("id"):
            return jsonify({"data": {"recipe": None}})
        antwoord = {"data": {"recipe": _snij(recept, gevraagd)}}
        if half_antwoord:
            antwoord["errors"] = [{"message": "Exception while fetching data "
                                              "(/recipe/rating) : timeout",
                                   "path": ["recipe", "rating"]}]
        return jsonify(antwoord)

    @app.get("/__mock/gezien")
    def gezien():
        return jsonify(app.config["gezien"])

    return app


def _fout(pad: str, naam: str, typenaam: str, oude_vorm: bool,
         suggesties: bool = False) -> dict:
    if oude_vorm:
        tekst = f'Cannot query field "{naam}" on type "{typenaam}".'
        if suggesties:
            tekst += ' Did you mean "description" or "steps"?'
        return {"message": tekst}
    return {
        "message": (f"Validation error (FieldUndefined@[{pad}]) : Field "
                    f"'{naam}' in type '{typenaam}' is undefined"),
        "locations": [{"line": 2, "column": 3}],
        "extensions": {"classification": "ValidationError"},
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Nagebouwde AH-API voor tests")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--zonder", nargs="*", default=[],
                        help="velden die deze AH niet kent, bijvoorbeeld: "
                             "--zonder images tips")
    args = parser.parse_args()

    schema = {naam: dict(velden) for naam, velden in SCHEMA.items()}
    for veld in args.zonder:
        for velden in schema.values():
            velden.pop(veld, None)
    app = create_app(schema=schema)
    print(f"Nagebouwde AH-API op http://{args.host}:{args.port} "
          f"(recept {RECEPT['id']})")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
