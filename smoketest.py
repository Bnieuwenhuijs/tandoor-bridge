#!/usr/bin/env python3
"""
Rooktest: één recept van begin tot eind, tegen je echte Tandoor.

Dit is het script om te draaien als je de brug net hebt ingericht, of als er
iets niet werkt. Het doet precies wat de wachtrij doet, maar stap voor stap en
met uitleg erbij, zodat je ziet waar het misgaat.

    export $(grep -v '^#' .env | xargs)      # of: draai hem in de container
    python smoketest.py
    python smoketest.py --url https://www.ah.nl/allerhande/recept/R-R921580
    python smoketest.py --dry-run            # alleen ophalen, niets aanmaken

Je token wordt nergens afgedrukt.
"""

import argparse
import os
import sys

from fetcher import FetchError, describe_page, fetch_image, fetch_page
from tandoor import TandoorClient, TandoorError, TandoorNoRecipe, TandoorThrottled
from urls import normalize_url

STANDAARD_URL = "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie"

GOED, FOUT, INFO = "  ✓", "  ✗", "   ·"


def stap(nummer, tekst):
    print(f"\n{nummer}. {tekst}")


def afbreken(melding, hint=None):
    print(f"{FOUT} {melding}")
    if hint:
        print(f"{INFO} {hint}")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=STANDAARD_URL, help="het recept om te proberen")
    parser.add_argument("--dry-run", action="store_true",
                        help="alleen ophalen en laten ontleden, niets in Tandoor zetten")
    parser.add_argument("--no-impersonate", action="store_true",
                        help="geen tweede poging met curl_cffi")
    args = parser.parse_args()

    tandoor_url = os.environ.get("TANDOOR_URL", "").strip()
    token = os.environ.get("TANDOOR_TOKEN", "").strip()
    publiek = os.environ.get("TANDOOR_PUBLIC_URL", "").strip() or None

    stap(1, "Instellingen")
    if not tandoor_url:
        afbreken("TANDOOR_URL is niet gezet", "zet hem in .env of in je omgeving")
    if not token:
        afbreken("TANDOOR_TOKEN is niet gezet",
                 "maak er een aan in Tandoor onder Instellingen → API, met scope 'read write'")
    print(f"{GOED} Tandoor: {tandoor_url}")
    print(f"{GOED} token: {len(token)} tekens, begint met {token[:4]}")
    if not token.startswith("tda_"):
        print(f"{INFO} let op: Tandoor-tokens beginnen normaal met tda_")

    client = TandoorClient(tandoor_url, token, public_url=publiek)

    stap(2, "Verbinding met Tandoor")
    try:
        aantal = client.check_connection()
    except TandoorError as e:
        afbreken(f"{e.message}",
                 "een 403 betekent een verkeerd token of een token zonder de scope "
                 "'read write'; Tandoor geeft hier nooit een 401")
    print(f"{GOED} verbonden, {aantal} recepten in je ruimte")

    url = normalize_url(args.url)
    stap(3, f"Pagina ophalen: {url}")
    if url != args.url.strip():
        print(f"{INFO} opgeschoond van {args.url.strip()}")
    try:
        pagina = fetch_page(url, impersonate=not args.no_impersonate)
    except FetchError as e:
        afbreken(e.reason,
                 "blokkeert de site ook vanaf de NAS? Zie de README bij "
                 "'een site blokkeert toch'")
    print(f"{GOED} {len(pagina.html) // 1024} KB binnen via {pagina.via} "
          f"(status {pagina.status})")
    if pagina.final_url != url:
        print(f"{INFO} doorgestuurd naar {pagina.final_url}")

    beschrijving = describe_page(pagina.html)
    print(f"{INFO} titel: {beschrijving['titel'] or '(geen)'}")
    print(f"{INFO} ld+json-blokken: {beschrijving['ld_json_blokken']}")
    if beschrijving["heeft_schema"]:
        print(f"{GOED} er staat een recept-schema in de pagina")
    else:
        print(f"{FOUT} geen recept-schema in de pagina")
        if beschrijving["lijkt_cookiemuur"]:
            print(f"{INFO} dit lijkt een cookiemelding en niet het recept")
        elif beschrijving["lijkt_js_pagina"]:
            print(f"{INFO} de pagina bouwt zichzelf met JavaScript op, dus het "
                  f"schema staat niet in de ruwe HTML")
        afbreken("Tandoor kan hier niets mee; zie de README bij "
                 "'een site blokkeert toch'",
                 "Tandoor geeft op zo'n pagina een kale 500, want zijn "
                 "data-tak vangt NoSchemaFoundInWildMode niet af")

    stap(4, "Tandoor laat ontleden")
    try:
        bron = client.recipe_from_source(url, pagina.html)
    except TandoorNoRecipe as e:
        afbreken(f"Tandoor vond geen recept: {e.message}",
                 "de pagina heeft dan geen bruikbare schema.org-gegevens")
    except TandoorThrottled as e:
        afbreken(f"importlimiet bereikt, {e.retry_after} seconden wachten",
                 "zie de README bij 'de limiet van 60 per uur'")
    except TandoorError as e:
        afbreken(e.message)

    recept = bron.recipe
    ingredienten = sum(len(s.get("ingredients") or []) for s in recept.get("steps") or [])
    print(f"{GOED} {recept.get('name')}")
    print(f"{INFO} {ingredienten} ingrediënten, {len(recept.get('steps') or [])} stappen, "
          f"{recept.get('servings')} porties")
    print(f"{INFO} bron volgens Tandoor: {recept.get('source_url') or '(leeg)'}")
    if not recept.get("source_url"):
        print(f"{INFO} leeg betekent dat de canonical niet is aangekomen - meld dit")
    if bron.duplicates:
        namen = ", ".join(str(d.get("name")) for d in bron.duplicates)
        print(f"{INFO} Tandoor kent dit recept al: {namen}")

    if args.dry_run:
        print("\nDry run, dus niets aangemaakt. Tot hier werkt alles.")
        return

    stap(5, "Recept aanmaken")
    try:
        aangemaakt = client.create_recipe(recept, source_url=url)
    except TandoorError as e:
        afbreken(e.message)
    recipe_id = aangemaakt.get("id")
    print(f"{GOED} recept {recipe_id} aangemaakt")
    print(f"{INFO} source_url staat op {aangemaakt.get('source_url') or '(leeg)'}")

    stap(6, "Foto erbij")
    foto_url = recept.get("image_url") or (bron.images[0] if bron.images else None)
    if not foto_url:
        print(f"{INFO} geen foto op de pagina gevonden, dat is geen fout")
    else:
        try:
            inhoud, content_type = fetch_image(foto_url, referer=pagina.final_url)
            client.set_image(recipe_id, inhoud, content_type=content_type)
            print(f"{GOED} {len(inhoud) // 1024} KB geüpload ({content_type})")
        except (FetchError, TandoorError) as e:
            melding = getattr(e, "reason", None) or getattr(e, "message", str(e))
            print(f"{FOUT} foto mislukte: {melding}")
            print(f"{INFO} het recept staat er wel; alleen de foto ontbreekt")

    print(f"\nKlaar. Bekijk het hier: {client.recipe_url(recipe_id)}")
    print("Staan de ingrediënten in de goede supermarktcategorieën, dan doen je "
          "automations hun werk.")


if __name__ == "__main__":
    main()
