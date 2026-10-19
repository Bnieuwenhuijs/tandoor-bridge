# Tandoor Bridge

Een importbrug voor recepten die [Tandoor](https://tandoor.dev) zelf niet
binnenkrijgt. Draait als container op de NAS, is alleen bereikbaar via het
LAN en Tailscale, en doet verder niets wat Tandoor al kan.

## Het probleem

Importeer je in Tandoor een recept via een URL, dan haalt **de Tandoor-server**
de pagina op. Bij Albert Heijn gaat dat mis:

```
URL:   https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie
Fout:  The requested page could not be loaded. The server returned 403.
```

Tandoor stuurt bij dat verzoek alleen een `User-Agent` en een `Accept`-header
mee — dat staat vast in `RecipeUrlImportView` en is niet in te stellen. De
botbeveiliging van AH (Akamai) weigert dat.

Een tussenservice op het LAN die Tandoor aanroept lost het niet op: Tandoor
haalt alles op via `safe_request`, met een SSRF-filter dat privé-adressen
blokkeert. Tandoor kan de NAS dus niet als proxy gebruiken.

**De oplossing is de richting omdraaien.** De brug haalt de pagina zelf op,
met een volledige set browserheaders, en biedt de **HTML** aan bij
`POST /api/recipe-from-source/`. Tandoor ontleedt die zelf en hoeft de site
nooit te benaderen. Daarna maakt de brug het recept aan en uploadt hij de
foto.

Alles wat met ingrediënten te maken heeft — ontleden, categorieën,
synoniemen, voorraad — blijft bij Tandoor. De brug geeft de HTML onbewerkt
door, dus de bestaande Food Alias-, Unit Alias-, Never Unit- en Food
Replace-automations doen hun werk precies zoals bij een gewone import.

## Hoe het werkt

Per recept drie aanroepen:

1. `POST /api/recipe-from-source/` met `{"url": ..., "data": <html>}`
2. `POST /api/recipe/` met het `recipe`-object uit stap 1, plus `source_url`
3. `PUT /api/recipe/{id}/image/` met de foto als multipart

Elke URL krijgt een eigen regel in SQLite met een eigen status
(`wachtend → ophalen → naar Tandoor → klaar`, of `mislukt` met de reden, of
`bestaat al`). Eén mislukking stopt de rest nooit — precies wat de
lijst-import van Tandoor zelf verkeerd doet. Wat onderweg was toen de
container stopte, gaat bij de start terug in de rij.

## Installeren op een Ugreen NAS (UGOS)

Je hebt nodig: Docker (zit in UGOS), een map op de NAS, en een Tandoor-token.

1. Maak een map, bijvoorbeeld `/volume1/docker/tandoor-bridge`.
2. Zet daar `docker-compose.yml` en `example.env` uit deze repo in.
3. Kopieer `example.env` naar `.env` en vul `TANDOOR_URL` en `TANDOOR_TOKEN`
   in. De `.env` komt niet in git.
4. Start het, via **Docker → Project** in UGOS, of op de commandoregel:

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

5. Open `http://<ip-van-je-nas>:8095`.

Updaten gaat daarna met:

```bash
docker compose pull && docker compose up -d
```

De geschiedenis staat in `./data` en overleeft dat.

### Als het image privé is

Staat deze repo op privé, dan is het GHCR-image dat ook en moet de NAS
inloggen:

```bash
docker login ghcr.io -u <je-github-naam>   # wachtwoord: een PAT met read:packages
```

Je kunt ook alléén het package openbaar maken en de repo privé laten: op
GitHub bij het package → *Package settings* → *Change visibility*. Dan pullt
de NAS zonder inloggen.

Zelf bouwen in plaats van pullen kan ook: haal de regel `image:` uit
`docker-compose.yml` en zet er `build: .` voor in de plaats.

## Installeren op TrueNAS

TrueNAS draait sinds versie 24.10 (Electric Eel) op Docker in plaats van
Kubernetes, en heeft dan **Apps → Discover Apps → Custom App → Install via
YAML**: een editor waarin je een compose-bestand plakt. `truenas-app.yaml` in
deze repo is daarvoor klaargemaakt.

De `docker-compose.yml` hierboven werkt daar *niet* rechtstreeks: `env_file:
.env` heeft geen bestand om te lezen, en het relatieve pad `./data` bestaat
niet in die context. In `truenas-app.yaml` staan de variabelen daarom direct
in de YAML en het volume als absoluut pad.

1. **Maak eerst de dataset aan**, bijvoorbeeld
   `/mnt/<pool>/apps/tandoor-bridge/data`. TrueNAS kan de wizard niet
   halverwege verlaten om nog opslag te maken.
2. Geef die dataset aan de apps-gebruiker (uid/gid **568**). Dat is op TrueNAS
   de gebruiker waaronder apps draaien, en `truenas-app.yaml` zet
   `user: "568:568"` zodat de brug als diezelfde gebruiker draait.
3. Plak `truenas-app.yaml`, pas het pad, `TANDOOR_URL` en `TANDOOR_TOKEN` aan,
   en installeer.

> Kies de YAML-route en niet het formulier: een Custom App die je via het
> formulier installeert, is daarna alleen nog via dat formulier te bewerken.
> Met de YAML-route houd je de editor.

**Gaat het bij de eerste start mis op de dataset,** dan zegt de brug precies
wat eraan te doen is, inclusief de uid waaronder hij draait:

```
RuntimeError: kan niet schrijven in /data (deze container draait als uid 568,
gid 568): Permission denied. Geef die map eigendom aan deze gebruiker
(chown -R 568:568 <pad op de host>), of laat de container als de eigenaar van
die map draaien met user: "<uid>:<gid>" in docker-compose.yml, of gebruik een
Docker-volume in plaats van een pad op de host.
```

Een Docker-volume in plaats van een pad op de host kan ook. Dan hoef je niets
te chownen — Docker neemt het eigendom over uit het image — maar dan moet de
regel `user:` weg, en staat de map buiten je snapshots.

## Een Tandoor-token maken

In Tandoor: **Instellingen → API → nieuw token**, met scope `read write`. Het
token begint met `tda_`.

> Kopieer het meteen. Tandoor laat een token maar vijftien seconden in
> leesbare vorm zien; daarna krijg je alleen nog
> `tda_************_******_***********abcd` te zien.

Zet hem in `.env` bij `TANDOOR_TOKEN`. De brug drukt je token nergens af, ook
niet in het logboek.

## Gebruiken

**De pagina.** Plak een WhatsApp-bericht, een hele chat-export of losse links
(één per regel). *Bekijk wat erin zit* laat per link zien wat er gaat
gebeuren: nieuw, al geïmporteerd (met een link naar het recept in Tandoor), al
in de wachtrij, of eerder mislukt. Daarna *Importeren*. Op de telefoon werkt
`ctrl`/`cmd` + `enter` ook.

AH-deellinks als `https://www.ah.nl/r/1197620` worden onderweg herschreven
naar de echte recept-URL, en trackingparameters (`utm_*`, `at_*`, `fbclid`,
`igsh`) gaan eraf. Dezelfde link in verschillende vormen telt als één recept.

**Een lijst uit een bestand.** Een `.txt` met URL's, één per regel. Regels die
geen link zijn worden overgeslagen.

**De geschiedenis** filtert op status. Mislukte regels hebben een
*Opnieuw*-knop, en er is één knop voor alle mislukte tegelijk.

### De API

```bash
# Eén link
curl -X POST http://192.168.1.130:8095/api/import \
     -H 'Content-Type: application/json' \
     -d '{"url": "https://www.ah.nl/allerhande/recept/R-R921580"}'

# Een heel bericht
curl -X POST http://192.168.1.130:8095/api/import \
     -H 'Content-Type: application/json' \
     -d '{"text": "Lieke: deze! https://... en https://..."}'
```

| endpoint | doet |
|---|---|
| `POST /api/preview` | wat zou er gebeuren, zonder iets te doen |
| `POST /api/import` | neemt `text`, `url`, `urls`, een formulierveld of `?url=` |
| `POST /api/upload` | een `.txt` met URL's (multipart, veld `file`) |
| `GET /api/items?status=mislukt` | de geschiedenis |
| `POST /api/items/<id>/retry` | één item opnieuw |
| `POST /api/retry-failed` | alle mislukte opnieuw |
| `GET /api/status` | tellers en of de worker draait |
| `GET /api/check` | werkt de verbinding met Tandoor |
| `GET /healthz` | voor de healthcheck van Docker |

### HTTP Shortcuts op Android (delen)

Zo kun je vanuit de AH-app of je browser een recept naar de brug delen.

1. Installeer [HTTP Shortcuts](https://http-shortcuts.rmy.ch/).
2. Nieuwe shortcut → **Method** `POST`, **URL**
   `http://192.168.1.130:8095/api/import`.
3. Bij **Request body** → *Content type* `application/json`, body:

   ```json
   {"url": "{{shared_text}}"}
   ```

   Die variabele kies je in de app via het variabelen-knopje naast het
   bodyveld → *Shared text*; typ hem niet met de hand, want HTTP Shortcuts
   zet er zelf een intern id in.
4. Bij **Trigger & Execution** → zet *Show in share menu* aan.
5. Wil je het ook onderweg: gebruik het Tailscale-adres van de NAS in de URL.

Delen vanuit een app geeft soms een hele zin in plaats van alleen een link.
Dat is geen probleem: de brug haalt er zelf de URL's uit.

### Home Assistant

```yaml
rest_command:
  recept_importeren:
    url: "http://192.168.1.130:8095/api/import"
    method: post
    content_type: "application/json"
    payload: '{"url": "{{ url }}"}'
```

## De limiet van 60 per uur

Alleen de importroute van Tandoor is afgeknepen:
`DRF_THROTTLE_RECIPE_URL_IMPORT`, standaard `60/hour`. Het aanmaken van een
recept en het uploaden van een foto zijn niet afgeknepen.

De brug gaat daar netjes mee om: bij een `429` wacht hij de `Retry-After` af
en gaat daarna door, en het item blijft in de rij staan. Een lijst van 233
recepten duurt op de standaardlimiet dus ongeveer vier uur, maar hij loopt
zonder toezicht door en overleeft een herstart.

Wil je zo'n lijst sneller binnenhalen, zet dan tijdelijk een hogere limiet in
de omgeving van je **Tandoor**-container:

```
DRF_THROTTLE_RECIPE_URL_IMPORT=600/hour
```

Daarna kun je hem gewoon terugzetten. Let op: de teller zit in Django's
standaardcache, en dat is per proces `LocMemCache` tenzij `REDIS_HOST` gezet
is. Draait je Tandoor met meerdere gunicorn-workers zonder Redis, dan is de
werkelijke limiet ongeveer 60 × het aantal workers.

## Als een site toch blokkeert

De brug probeert het in deze volgorde, en stopt bij de eerste die lukt:

1. **`requests` met minimale headers** - alleen een User-Agent en
   `Accept-Language`. Dit is de set uit `recipe_parser`, die zich bewezen
   heeft, en hij staat vooraan om een reden: de uitgebreidere set hieronder
   bleek bij AH juist een 403 op te leveren. Akamai beoordeelt de combinatie,
   en Chrome-headers zonder bijpassende TLS-vingerafdruk zijn verdachter dan
   een kale User-Agent.
2. **`requests` met de volledige set** - `Sec-Fetch-*`, `Sec-Ch-Ua` en de
   rest, voor sites die juist die headers willen zien.
3. **`curl_cffi` met `impersonate="chrome"`** - voor sites die naar de
   TLS-vingerafdruk kijken.
4. **De browser-container**, als die er is. Zie hieronder.

Een 404 stopt de rij meteen: die pagina is er niet, en daar helpt geen andere
poging aan. In het logboek staat welke poging het was en wat er terugkwam:

```
poging mislukt (minimale headers) voor https://...: geblokkeerd (status 403)
opgehaald via volledige headers: https://... (status 200)
```

Wat de meldingen betekenen:

| in het logboek | wat het is |
|---|---|
| `geblokkeerd (status 403)` | de site weigert de aanvraag |
| `blokkadepagina met status 200` | een blokkade vermomd als gewone pagina (Akamai, Cloudflare) |
| `pagina bestaat niet meer (status 404)` | het recept is weg; geen tweede poging |
| `Tandoor vond geen recept: No usable data could be found.` | de pagina heeft geen bruikbare schema.org-gegevens |

Draait `curl_cffi` niet op jouw machine, dan blijft de brug gewoon werken met
de overige pogingen.

**Weet je niet waarom een site niet lukt**, open dan dit in je browser:

```
http://<ip-van-je-nas>:8095/api/diagnose?url=https://www.ah.nl/allerhande/recept/R-R921580
```

Die probeert alle manieren náást elkaar en zegt per poging of de aanvraag
doorkwam én of er een recept-schema in de pagina staat - dat laatste is wat
Tandoor nodig heeft. Tandoor komt er niet aan te pas, dus het kost ook geen
plaats in zijn importlimiet.

## De browser-container

Albert Heijn weigert **elke** server-side aanvraag met een 403: met de
minimale headers, met de volledige set, en met `curl_cffi` inclusief
TLS-vingerafdruk. Gemeten, alle drie. De reden is dat Akamai z'n
sensor-cookies (`_abck`, `bm_sz`) pas zet nadat het JavaScript op de pagina
gedraaid en gecontroleerd is. In je browser heb je die cookies; een client die
dat script niet uitvoert, krijgt ze nooit.

Daarom is er een tweede, optionele container: `renderer/`, met Chromium via
Playwright. De brug vraagt hem om de pagina wanneer de gewone pogingen falen,
én wanneer wat er binnenkomt geen recept-schema bevat - een cookiemuur komt
namelijk met een nette 200 terug.

```
RENDERER_URL=http://tandoor-bridge-renderer:8096
```

Wat het kost: het image is ongeveer 1,2 GB, want een echte browser is groot.
Draaiend kost het niets zolang er niets te importeren valt: de browser start
bij het eerste verzoek en sluit zichzelf af na `BROWSER_IDLE_SECONDS` (120)
zonder werk. Tijdens een import is het kortdurend een paar honderd MB.

Importeer je geen AH, haal dan de service en `RENDERER_URL` weg. De brug
werkt dan gewoon door met de overige pogingen en zegt bij de start:
`geen browser-container (RENDERER_URL is leeg)`.

> Zet die container **niet** open naar buiten en geef hem geen `ports`. Hij
> haalt elke URL op die je hem geeft, dus binnen het Docker-netwerk is de
> enige plek waar hij hoort.

In `/api/status` zie je onder `browser` of hij is ingesteld, en
`/api/diagnose` neemt hem mee als vierde poging.

## De rooktest

Werkt iets niet, draai dan dit — het doet wat de wachtrij doet, maar stap voor
stap en met uitleg:

```bash
docker compose exec tandoor-bridge python smoketest.py
docker compose exec tandoor-bridge python smoketest.py --url https://www.ah.nl/allerhande/recept/R-R921580
docker compose exec tandoor-bridge python smoketest.py --dry-run   # niets aanmaken
```

## Testen zonder je echte Tandoor

`mock_tandoor.py` is een nagebouwde Tandoor-API die je los kunt draaien:

```bash
python mock_tandoor.py --throttle-after 2      # op 127.0.0.1:8099
```

Elk token dat met `tda_` begint werkt. Hij bootst niet alleen de
responsevormen na maar ook het gedrag dat stil kan mislukken, dus je kunt de
brug ertegen aanzetten zonder risico:

```bash
TANDOOR_URL=http://127.0.0.1:8099 TANDOOR_TOKEN=tda_test DATA_DIR=./data python app.py
```

Wat de mock **niet** doet: echt ingrediënten ontleden (de
hoeveelheid/eenheid-splitsing is een grove benadering), een rechtenmodel
verder dan de `tda_`-prefix, en de AI-endpoints.

## Wat de brug niet doet

- Geen eigen ingrediëntenparser, geen categorieën, geen Bring.
- Niets wijzigen aan bestaande recepten in Tandoor. Alleen `source_url` en de
  foto van recepten die hij zelf net heeft aangemaakt.
- Niets verwijderen.
- Geen voedingswaarden meesturen (standaard uit, zie `SEND_PROPERTIES`).
  Tandoor schrijft daarbij de naam uit het verzoek op het bestaande
  `PropertyType` in je ruimte, en dat is een wijziging aan de inrichting van
  je ruimte.

## Ontwikkelen

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Geen enkele test raakt het internet: het ophalen gaat tegen een HTTP-server op
localhost, en Tandoor tegen `mock_tandoor.py`.

| bestand | taak |
|---|---|
| `urls.py` | links uit tekst halen, opschonen, AH-vormen, dubbel-sleutel |
| `fetcher.py` | ophalen met browserheaders, `curl_cffi`-fallback, blokkadeherkenning |
| `tandoor.py` | de API-client |
| `importqueue.py` | SQLite-wachtrij en de worker-thread |
| `app.py` | Flask-routes en de JSON-API |
| `mock_tandoor.py` | nagebouwde Tandoor-API voor de tests |
| `smoketest.py` | één recept stap voor stap tegen je echte Tandoor |
| `renderer/` | de losse browser-container, voor sites die alles weigeren |

Het heet `importqueue.py` en niet `queue.py`, omdat een `queue.py` in de
projectmap de `queue` uit de standaardbibliotheek schaduwt — `urllib3` haalt
daar `LifoQueue` uit, dus met `queue.py` ernaast importeert `requests` niet
eens meer.

Gunicorn draait met **precies één worker**. De wachtrij-thread leeft in het
webproces, dus twee workers zijn twee threads die dezelfde rij leegtrekken en
elk recept dubbel importeren. Meer verkeer aankunnen gaat via `--threads`.

## Wat in de Tandoor-broncode bleek te staan

Nagekeken in tag `2.6.9`. Dit staat in geen documentatie en elk punt kan stil
mislukken, dus het staat hier voor de volgende keer:

- **Het veld `data` gaat door `urllib.parse.unquote`.** Ruwe HTML met
  `%XX`-reeksen wordt verminkt: `%2F` wordt `/`, `%20` wordt een spatie. Omdat
  het `unquote` is en niet `unquote_plus`, is het procentteken het enige dat
  bescherming nodig heeft; `escape_for_data()` vervangt alleen `%` door `%25`,
  wat de pagina niet groter maakt. Dat laatste telt: Tandoor zet geen eigen
  `DATA_UPLOAD_MAX_MEMORY_SIZE`, dus Django's grens van 2,5 MB geldt.
- **Daarvóór draait een DRF `CharField`** met `trim_whitespace`, dus de
  witruimte aan begin en eind gaat eraf, en een NUL-teken of losse surrogate
  wordt met een 400 geweigerd.
- **Sturen we `url` én `data`, dan parseert Tandoor onze data** (de
  url-tak vereist `not data`) **en vult hij `duplicates` wél** — die controle
  staat buiten de url/data-splitsing. Gratis tweede dubbelcontrole.
- **In die tak krijgt de scraper `org_url='https://urlnotfound.none'`,** dus
  de bron-URL komt uit `<link rel="canonical">`. Ontbreekt die, dan is
  `source_url` bij het parsen leeg, komt er geen domein-trefwoord bij, en
  vuren Food Replace-automations met een bron-regex niet. De brug zet daarom
  één absolute canonical vooraan in de `<head>`, met een string-insertie, zodat
  de rest van de pagina byte voor byte intact blijft.
- **`ingredients[].order` is in het antwoord altijd `null`,** maar
  `Ingredient.order` is `IntegerField(default=0)` zonder `null=True`. Het
  antwoord onbewerkt terugposten geeft dus
  `400 {"steps":[{"ingredients":[{"order":["This field may not be null."]}]}]}`.
  Hetzelfde geldt voor `servings` bij een pagina zonder `recipeYield`.
- **`source_url` is schrijfbaar bij het aanmaken,** dus een losse `PATCH`
  achteraf is niet nodig.
- **Het afbeeldingsendpoint keurt op bestandsnaam,** niet op inhoud:
  `is_file_type_allowed()` laat alleen `.png`, `.jpg`, `.jpeg`, `.gif` en
  `.webp` door. Een naam zonder achtervoegsel geeft een **500**, geen nette
  400. `avif` staat niet op die lijst, dus de brug vraagt er ook niet om.
- **De API geeft nooit een 401.** `SessionAuthentication` staat vooraan in
  `DEFAULT_AUTHENTICATION_CLASSES` en z'n `authenticate_header()` geeft
  `None`, waarna DRF elke 401 tot een 403 herschrijft. Een verkeerd token en
  een token zonder de juiste scope zijn dus niet van elkaar te onderscheiden.
- **De `image`-URL in het antwoord van dat endpoint is relatief,** anders dan
  bij de overige recept-endpoints.
- **Een lege `PUT` naar het afbeeldingsendpoint wist de foto.** Er is geen
  manier om die aanroep niets te laten doen.

## Later, niet in deze versie

Tandoor heeft een AI-functie die receptstappen opsplitst en ingrediënten aan
de juiste stap koppelt (`POST /api/ai-step-sort/`, in de interface
"Automatisch sorteren"). Dat vraagt een geconfigureerde AI-provider en is
beperkt tot ongeveer 60 aanroepen per uur. De plek ervoor zit er al: het zou
een vinkje "ook stappen koppelen" worden, tussen stap 1 en stap 2 van de
importflow.
