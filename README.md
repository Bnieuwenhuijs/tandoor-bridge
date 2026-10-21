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

Bij Albert Heijn lukt zelfs dat niet: die pagina komt op **geen enkele**
manier binnen vanaf een server. Daarvoor is er een eigen route, die het
recept bij de app-API van AH ophaalt en er zelf een pagina van maakt. Zie
[Albert Heijn: de app-API](#albert-heijn-de-app-api).

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

### Werktijd en wachttijd

Tandoor zet `prepTime` van de pagina in de werktijd en `cookTime` in de
wachttijd. Veel sites geven geen `cookTime`, maar wel een bereidingstijd en
een totale tijd ("15 min bezig, 1 uur totaal"). Dan blijft de wachttijd 0.
De brug vult hem in dat geval aan met totaal min bereiding, maar alleen als
het eenduidig is: er staat geen `cookTime`, Tandoor nam de bereidingstijd als
werktijd, en de totale tijd is langer. Alleen uit de velden van de pagina,
niet uit de tekst van de stappen ("25 min in de oven" lezen is raden). Dit
geldt voor nieuwe imports; recepten die al in Tandoor staan blijven zoals ze
zijn.

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
| `GET /api/status` | tellers, of de worker draait, of de AH-route aanstaat |
| `GET /api/diagnose?url=...` | probeert één pagina op alle manieren, zonder Tandoor |
| `POST /bookmarklet` | waar de bookmarklet een pagina heen stuurt (formulier: `url`, `html`) |
| `GET /api/sorteren` | de sorteerronde: fase, voortgang, AI-provider |
| `GET /api/sorteren/items?status=` | per recept: `voorstel`, `overgeslagen`, `gesorteerd`, `mislukt`, `te_bekijken` |
| `POST /api/sorteren/proefdraai` | proefdraai starten of hervatten; schrijft niets |
| `POST /api/sorteren/uitvoeren` | de voorstellen schrijven |
| `POST /api/sorteren/stop` | stoppen na het recept waar hij mee bezig is |
| `GET /api/check` | werkt de verbinding met Tandoor |
| `GET /api/ah?url=...` | wat de AH-app-API van dit recept geeft, zonder Tandoor |
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

## Ingrediënten bij de juiste stap (AI)

Tandoor zet bij een import alle ingrediënten in de eerste stap. Tandoor kan
ze ook zelf verdelen, met AI: dat is de knop **Automatisch sorteren** in de
recepteditor, en achter die knop zit `POST /api/ai-step-sort/`. De brug
gebruikt precies die functie, op twee plekken.

Je hebt er een AI-provider in Tandoor voor nodig (Instellingen → AI), en
AI-tegoed in je space.

### Bij het importeren

Staat er een provider, dan gaat elk nieuw recept eerst langs de sortering en
wordt het dan aangemaakt zoals Tandoor het teruggeeft. Mislukt de AI-aanroep
(een fout, een time-out, de limiet, het tegoed), dan wordt het recept gewoon
ongesorteerd aangemaakt, en staat de reden in de geschiedenis:
`ongesorteerd: het AI-tegoed van de space is op`. Weigert Tandoor wat de AI
teruggaf, dan ook: ongesorteerd, met de reden.

**Welke provider:** `TANDOOR_AI_PROVIDER_ID` in de `.env` als je die zet.
Anders de standaardprovider van je space, en anders de enige die er is. Zijn
er meer en is er geen standaard, dan sorteert de brug niet en zegt hij welke
ids er zijn: elke aanroep kost tegoed, dus hij gokt niet. Welke hij gebruikt,
staat in het tabblad **Sorteren** en bij de start in het logboek. Uitzetten
voor nieuwe recepten: `AI_SORT_IMPORTS=0`.

### Voor recepten die er al staan: het tabblad Sorteren

> **Maak eerst een back-up van je Tandoor-database.** Uitvoeren schrijft in
> je bestaande recepten. Op TrueNAS is een snapshot van de dataset waarin de
> database van Tandoor staat het snelst, en het snelst terug te zetten. Anders
> een dump vanuit de databasecontainer:
>
> ```bash
> docker exec <postgres-container> pg_dump -U <gebruiker> <database> > tandoor-voor-sorteren.sql
> ```
>
> De gebruiker en de database staan in de omgeving van je Tandoor-app
> (`POSTGRES_USER`, `POSTGRES_DB`).

Twee knoppen, met een moment ertussen om te kijken:

1. **Proefdraai** haalt elk recept op en schrijft niets. Hij slaat over:
   recepten waarvan de ingrediënten al over meer stappen verdeeld zijn,
   recepten zonder ingrediënten of zonder stappen, en recepten waar de AI
   niets aan verandert. Voor de rest vraagt hij de AI om een indeling, en
   laat per recept zien: het aantal stappen voor en na, en welk ingrediënt
   naar welke stap gaat.
2. **Uitvoeren** schrijft precies die voorstellen. Geen nieuwe AI-vraag, want
   die kan iets anders opleveren dan wat je net zag. Is een recept sinds de
   proefdraai veranderd, dan slaat hij het over (mislukt, met de reden); de
   volgende proefdraai bekijkt het opnieuw.

Het draait op de achtergrond op de NAS: je kunt de pagina dichtdoen. Er is
een stopknop, en na een herstart van de container gaat de ronde verder waar
hij was. Een nieuwe proefdraai na "stop" gaat ook verder, en vraagt niet
opnieuw wat al bekeken is - dat heeft tegoed gekost. Alleen wat mislukte,
wordt opnieuw bekeken. Wat al gesorteerd is, blijft met rust.

Tussen twee AI-vragen wacht de ronde de tijd die bij `AI_RATELIMIT` hoort
(standaard `60/hour`, dus een minuut; zelfde schrijfwijze als in Tandoor). Is
het tegoed van je space op, dan stopt de ronde met die melding en blijven de
overige recepten wachten. Vul het aan en druk weer op Proefdraai.

**Wat er bij een bestaand recept verandert:** de stappen (de AI mag ze
splitsen) en welk ingrediënt bij welke stap hoort. Verder niets: de
hoeveelheid, eenheid en food van een ingrediënt schrijft de brug niet, ook
niet als de AI ze anders teruggeeft. Er wordt niets verwijderd. Een stap die
de AI samenvoegt, wordt van het recept losgekoppeld maar blijft in de
database staan.

**Hoe dat veilig gaat** volgt uit hoe Tandoor een recept bijwerkt (zie
[hieronder](#wat-in-de-tandoor-broncode-bleek-te-staan)):

- Alleen ids uit dít recept. Tandoor zoekt een id in de hele database op, dus
  een id dat de AI verzint of verwisselt, zou een stap van een ander recept
  bijwerken.
- Elk ingrediënt in precies één stap. Zet de AI er een in twee stappen, dan
  telt de eerste. Laat de AI er een weg, dan blijft het in stap 1. Dat staat
  dan bij het voorstel.
- Per ingrediënt alleen id en volgorde, zodat er geen food wordt beschreven -
  die hoort bij al je recepten.
- Nieuwe stappen eerst leeg aanmaken, dan alles in één keer verplaatsen. Geen
  van beide aanvragen kan halverwege op validatie stuklopen, en dat moet, want
  Tandoor gebruikt geen transactie.
- Daarna wordt nagekeken of het recept staat zoals bedoeld. Zo niet, of mislukt
  een aanvraag, dan zet de brug de oude indeling terug.

### Eén minuut per AI-vraag

De nginx in de Tandoor-container wacht **60 seconden** op een antwoord, en
dat is niet in te stellen (geen `proxy_read_timeout` in Tandoors eigen
nginx-configuratie). Duurt de AI langer, dan krijgt de brug een 504, terwijl
de vraag in Tandoor gewoon doorloopt en tegoed kost.

Daarom krijgt de AI van de brug alleen wat hij nodig heeft om te sorteren:
per stap de tekst, per ingrediënt de food, eenheid, hoeveelheid en notitie,
en de ids. Niet het hele recept zoals Tandoor het geeft - met per ingrediënt
de complete food, omrekeningen en waar het nog meer in staat is dat snel
50 KB, en Tandoor vraagt het model om dat allemaal terug te typen. Dat duurde
drie minuten en werd dan afgekapt. Bij een nieuw recept komt de inhoud van de
ingrediënten daarom uit Tandoors eigen ontleding, en bepaalt de AI de stappen
en de indeling.

Komt er toch een 504, dan wacht de brug vier minuten met de volgende
AI-vraag, zodat Tandoor de vorige eerst afmaakt. Zonder die pauze stapelen de
vragen zich op in Tandoor, en reageert Tandoor ook in je browser niet meer.

### De limiet geldt voor import én AI samen

In Tandoor tellen de AI en de import mee in **dezelfde** teller (allebei een
`UserRateThrottle` zonder eigen scope, dus dezelfde cachesleutel). Elke import
is één aanvraag, elke sortering nog een. Bij `AI_RATELIMIT=60/hour` en een
grote lijst raakt de AI dus na ongeveer dertig recepten per uur op z'n
limiet, ook als jij de importlimiet hoger hebt gezet. Die recepten worden
ongesorteerd aangemaakt, met de reden erbij.

Twee manieren om daarmee om te gaan:

- **Eerst importeren, later sorteren.** Laat de lijst gewoon lopen en draai
  daarna het tabblad Sorteren: dat pakt alle ongesorteerde recepten op, in
  z'n eigen tempo.
- **Alles meteen sorteren.** Zet `IMPORT_DELAY_SECONDS=130` zolang de lijst
  loopt: dan blijven import en AI samen onder de 60 per uur (120 is precies
  60, en dan zit je er net op).

## De limiet van 60 per uur

De importroute van Tandoor is afgeknepen: `DRF_THROTTLE_RECIPE_URL_IMPORT`,
standaard `60/hour`. De AI-sortering ook (`AI_RATELIMIT`), en die twee delen
één teller - zie [hierboven](#de-limiet-geldt-voor-import-én-ai-samen). Het
aanmaken van een recept, het bijwerken ervan en het uploaden van een foto
zijn niet afgeknepen.

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

1. **De AH-app-API**, alleen bij een Albert Heijn-receptlink. Zie de
   volgende paragraaf.
2. **`requests` met minimale headers** - alleen een User-Agent en
   `Accept-Language`. Dit is de set uit `recipe_parser`, die zich bewezen
   heeft, en hij staat vooraan om een reden: de uitgebreidere set hieronder
   bleek bij AH juist een 403 op te leveren. Akamai beoordeelt de combinatie,
   en Chrome-headers zonder bijpassende TLS-vingerafdruk zijn verdachter dan
   een kale User-Agent.
3. **`requests` met de volledige set** - `Sec-Fetch-*`, `Sec-Ch-Ua` en de
   rest, voor sites die juist die headers willen zien.
4. **`curl_cffi` met `impersonate="chrome"`** - voor sites die naar de
   TLS-vingerafdruk kijken.
5. **De browser-container**, als die er is. Zie hieronder.

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

## Albert Heijn: de app-API

`www.ah.nl` weigert **elke** server-side aanvraag met een 403: met de minimale
headers, met de volledige set, met `curl_cffi` inclusief TLS-vingerafdruk, en
ook vanuit een headless Chromium. Gemeten, alle vier.

Maar `api.ah.nl` — waar de Appie-app mee praat — weigert niets. Een anoniem
token is genoeg, en daarmee geeft hij het recept gewoon terug:

```
POST https://api.ah.nl/mobile-auth/v1/auth/token/anonymous   {"clientId": "appie-ios"}
POST https://api.ah.nl/graphql    { recipe(id: 1194169) { ... } }
```

Dus doet de brug dat voor elke AH-receptlink, vóór alle andere pogingen, en
bouwt hij van het antwoord een pagina met het recept als JSON-LD erin. Die
pagina gaat daarna door precies dezelfde route als elke andere:
`POST /api/recipe-from-source/`. **Tandoor ontleedt dus nog steeds alles.**

Dat laatste is de hele reden voor die omweg. AH geeft de ingrediëntregel als
hele tekst mee, inclusief eenheid:

```
800 g zwarte bonen in blik
100 ml La Morena chipotlesaus
2 el arachideolie
```

Die regels gaan onbewerkt naar Tandoor, die er `800` + `g` + `zwarte bonen in
blik` van maakt. Food Alias, Unit Alias, Never Unit en Food Replace doen
daarna hun werk, net als bij elk ander recept. De brug ontleedt zelf niets.

Wat er meekomt: titel, beschrijving, porties, bereidingstijd, oven- en
wachttijd, de ingrediëntregels, de stappen, de foto in de grootste maat, en de tips. Die
laatste komen als extra stap(pen) ná de echte, met `Tip:` ervoor. Niet in de
beschrijving: die knipt Tandoor af op 512 tekens, en boven de 256 zet hij hem
ook nog eens voor stap 1, zodat je hem twee keer leest. Het domein-trefwoord `www.ah.nl` komt er via de
canonical in, dus Food Replace-regels met een bron-regex vuren ook.

Trefwoorden: de menugang en de keuken, plus van de AH-tags alleen de groepen
"speciale wensen" en "soort gerecht" (vegetarisch, gezond, salade, soep). De
andere tags, zoals "wat eten we vandaag", zouden alleen nietszeggende nieuwe
trefwoorden in Tandoor opleveren.

De oven- en wachttijd staan niet bij het recept zelf in de app-API, alleen in
de zoekresultaten (`recipeSearch` → `time { cook oven wait }`). De brug zoekt
daarom één keer op de titel en neemt de tijd van het resultaat met hetzelfde
recept-ID. Oven plus wachten wordt `cookTime`, dus Tandoors wachttijd; de
bereidingstijd blijft de werktijd. Vindt de zoekvraag het recept niet of
faalt hij, dan komt het recept gewoon binnen, zonder wachttijd.

Werkt het? Kijk zonder Tandoor erbij:

```
http://<ip-van-je-nas>:8095/api/ah?url=https://www.ah.nl/allerhande/recept/R-R1194169/traybake
```

of vanaf de opdrachtregel:

```
python smoketest.py --ah-only --url https://www.ah.nl/allerhande/recept/R-R1194169/traybake
```

Je ziet dan welke ingrediëntregels Tandoor straks te ontleden krijgt.

**Het schema van AH is niet gedocumenteerd** en introspectie staat uit, dus de
veldnamen komen uit openbare clients die dezelfde API gebruiken. Daarom vraagt
de brug alles buiten de kern *optioneel*: zegt AH over een veld "is
undefined", dan laat hij dat veld vallen (of vraagt een kalere vorm) en
probeert het opnieuw. Welke vraag werkte blijft daarna een dag in het
geheugen staan, dus dat kost één keer een extra ronde en niet bij elk recept.
Na die dag begint hij weer bij de volledige vraag: zo blijft een veld dat AH
één keer niet kende niet wekenlang weg, en komt een veld dat AH erbij zet er
vanzelf bij. Verandert AH
morgen iets, dan levert de brug een kaler recept in plaats van een foutmelding
— en staat in het logboek welk veld verdwenen is:

```
AH kent images niet (meer); vraag aangepast
```

Zet AH de API helemaal dicht, dan zet je `AH_API=0`; dan blijft voor AH
alleen de bookmarklet over. De browser-container helpt daar niet: die krijgt
bij `www.ah.nl` ook een 403 (zie hieronder). Weigert AH ons omdat de app-versie te oud is, zet
dan `AH_CLIENT_VERSION` (en eventueel `AH_USER_AGENT`) op de versie die nu in
de App Store staat. Dat kan zonder nieuwe image.

## De browser-container

Voor Albert Heijn is de container niet meer nodig: die gaat nu via de API
hierboven. Hij blijft er voor andere sites die hun recept pas met JavaScript
opbouwen, of die een blokkade als gewone pagina teruggeven.

De reden dat `www.ah.nl` niets doorlaat, is dat Akamai z'n sensor-cookies
(`_abck`, `bm_sz`) pas zet nadat het JavaScript op de pagina gedraaid en
gecontroleerd is. In je browser heb je die cookies; een client die dat script
niet uitvoert, krijgt ze nooit.

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

Heb je hem niet nodig, haal dan de service en `RENDERER_URL` weg. De brug
werkt dan gewoon door met de overige pogingen en zegt bij de start:
`geen browser-container (RENDERER_URL is leeg)`. AH blijft werken: dat gaat
via de app-API, en de startregel daarvoor is `Albert Heijn gaat via de
app-API`.

> Zet die container **niet** open naar buiten en geef hem geen `ports`. Hij
> haalt elke URL op die je hem geeft, dus binnen het Docker-netwerk is de
> enige plek waar hij hoort.

In `/api/status` zie je onder `browser` of hij is ingesteld en onder
`browser_bereikbaar` of hij ook antwoordt - dat zijn twee verschillende dingen.
Antwoordt hij niet, dan zegt `browser_diagnose` welke van de drie oorzaken het
is: de naam lost niet op (de tweede service staat niet in dezelfde YAML), de
poort is dicht (de container valt om - kijk in z'n logboek), of `/healthz` is
stil (hij start nog op). `/api/diagnose` neemt hem mee als vierde poging.

### Zo min mogelijk herkenbaar

Een botdetectie kijkt niet alleen naar headers. Gemeten met `/fingerprint` op
de renderer zelf:

| signaal | standaard Playwright | hier |
|---|---|---|
| `navigator.userAgent` | bevat `HeadlessChrome` | gewone Chrome-UA |
| `navigator.webdriver` | `true` | `undefined` |
| browser-binary | de kalere "headless shell" | volledig Chromium |
| `navigator.languages` | `en-US` | `nl-NL` |

Daarvoor zorgen `--disable-blink-features=AutomationControlled`,
`ignore_default_args=["--enable-automation"]`, een eigen `USER_AGENT` en
`BROWSER_CHANNEL=chromium`. Die laatste is nodig omdat Playwright standaard
een aparte, kalere build gebruikt die makkelijker te herkennen is; bij een
onbekende channel valt de renderer zelf terug op de standaard, zodat een
verkeerde waarde de container niet sloopt.

Wat blijft staan: de WebGL-renderer meldt SwiftShader, want er zit geen GPU in
een container. Dat is een echt signaal en daar is binnen een container weinig
aan te doen.

**En dan nog kan het niet genoeg zijn.** Bij Albert Heijn is het dat ook niet:
alle vier de pogingen krijgen een 403, inclusief deze geharde browser. Akamai
weegt tientallen signalen, waaronder het ontbreken van een echt beeldscherm en
van de sensor-cookies die hun script zet. Voor zulke sites is de bookmarklet
hieronder de route die wél werkt.

## De bookmarklet

Voor sites die geen enkele aanvraag van de NAS toelaten. Jouw browser komt er
wél door - die heeft de cookies en voert hun JavaScript uit - dus laat je die
de pagina opsturen.

Open de brug in je browser; onder "Voor sites die de brug weigeren" staat een
knop. **Sleep die naar je bladwijzerbalk** (klikken op de pagina zelf doet
niets zinnigs). Sta je daarna op een receptpagina, klik de bladwijzer aan: er
opent een tabblad dat zegt of het gelukt is, met een link naar het recept in
Tandoor.

Alles ná het ophalen is identiek aan een gewone import, dus de
dubbelcontrole, de bron-URL, de foto, je automations en de geschiedenis van de
brug werken precies zo. In de geschiedenis staat `bookmarklet` als bron.

Twee dingen die eraan vastzitten:

- Het is een **formulier-submit en geen `fetch`**, en dat is geen
  smaakkwestie: vanaf een https-pagina blokkeert je browser een `fetch` naar
  `http://<nas>` als mixed content, terwijl navigatie via een formulier wel
  mag. Zo doet Tandoors eigen bookmarklet het ook.
- De bookmarklet wijst naar de host waarmee je hem opende, dus maak hem aan
  via het adres dat je ook onderweg gebruikt - via Tailscale werkt hij dan ook
  buiten huis.

Eén klik per recept, dus niet voor een lijst van 233 - maar dat hoeft ook
niet meer: AH-recepten lopen via de app-API gewoon automatisch door de
wachtrij. De bookmarklet is er voor de sites die de NAS helemaal weigeren, en
als uitwijk voor AH als je `AH_API=0` hebt moeten zetten.

## De rooktest

Werkt iets niet, draai dan dit — het doet wat de wachtrij doet, maar stap voor
stap en met uitleg:

```bash
docker compose exec tandoor-bridge python smoketest.py
docker compose exec tandoor-bridge python smoketest.py --url https://www.ah.nl/allerhande/recept/R-R921580
docker compose exec tandoor-bridge python smoketest.py --dry-run   # niets aanmaken
docker compose exec tandoor-bridge python smoketest.py --ah-only   # alleen de AH-route
```

`--ah-only` heeft geen token nodig en raakt Tandoor niet aan: dat is de test
voor de vraag of `api.ah.nl` het vanaf jouw NAS doet.

## Testen zonder je echte Tandoor

`mock_tandoor.py` is een nagebouwde Tandoor-API die je los kunt draaien, en
`mock_ah.py` doet hetzelfde voor de Albert Heijn-API:

```bash
python mock_tandoor.py --throttle-after 2      # op 127.0.0.1:8099
python mock_ah.py --zonder images tips         # op 127.0.0.1:8098
```

Met `--zonder` doet die AH alsof hij een veld niet kent, zodat je kunt zien wat
de brug dan nog oplevert. Wijs hem aan met `AH_API_URL`:

```bash
AH_API_URL=http://127.0.0.1:8098 python smoketest.py --ah-only \
    --url https://www.ah.nl/allerhande/recept/R-R1194169/traybake
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

- Geen eigen ingrediëntenparser, geen categorieën, geen Bring. Ook op de
  AH-route niet: daar gaat de hele ingrediëntregel zoals AH hem geeft naar
  Tandoor, die hem ontleedt. Alleen als AH die regel ooit niet meegeeft,
  maakt de brug er zelf een van getal en naam, en dan zo dat Tandoor er geen
  eenheid uit kan halen: bij een naam van meer woorden zou Tandoor het tweede
  woord als eenheid lezen (`400 zwarte bonen` → eenheid `zwarte`), dus komt het
  getal dan tussen haakjes achter de naam en in de notitie.
- Niets wijzigen aan bestaande recepten in Tandoor. Alleen `source_url` en de
  foto van recepten die hij zelf net heeft aangemaakt - en de stappen en de
  ingrediëntindeling, maar alleen als jij in het tabblad Sorteren op
  **Uitvoeren** drukt.
- Niets verwijderen. Ook niet bij het sorteren: een stap die de AI samenvoegt,
  wordt losgekoppeld en blijft in de database.
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
| `ah_api.py` | de Albert Heijn-route: app-API in, pagina met JSON-LD uit |
| `tandoor.py` | de API-client |
| `importqueue.py` | SQLite-wachtrij en de worker-thread |
| `sorteren.py` | AI-sortering: provider kiezen, plan maken, veilig schrijven |
| `sorteerronde.py` | de sorteerronde voor bestaande recepten, als achtergrondtaak |
| `app.py` | Flask-routes en de JSON-API |
| `mock_tandoor.py` | nagebouwde Tandoor-API voor de tests |
| `mock_opslag.py` | de recepten van die mock, bijgewerkt zoals Tandoor geneste stappen bijwerkt |
| `mock_ah.py` | nagebouwde AH-API, met een instelbaar schema |
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
- **De AI-limiet en de importlimiet delen één teller.** `AiEndpointThrottle` en
  `RecipeImportThrottle` zijn allebei een `UserRateThrottle` zonder eigen
  `scope`, dus allebei `throttle_user_<id>`. Elke import telt mee voor de AI.
- **`ai-step-sort` slaat niets op, en controleert het antwoord van het model
  niet** (`# TODO validate` staat in de view). Een verkeerd provider-id geeft
  een 500 in plaats van een 400, en de melding over op-zijnde tegoed is tekst
  zonder foutcode.
- **Geneste stappen en ingrediënten bijwerken** gaat via
  `drf-writable-nested` 0.7.2, en dat doet vier dingen die je moet weten
  voordat je een bestaand recept aanraakt: een id wordt in de hele database
  opgezocht, niet in het recept; wat uit een lijst valt wordt losgekoppeld,
  niet verwijderd; een nieuw genest object is nooit partial, ook niet voor de
  bestaande ingrediënten eronder; en een food die met een id meekomt, wordt
  zelf ook bijgewerkt.
- **Er is geen transactie om een aanvraag.** Geen `ATOMIC_REQUESTS`: valt de
  derde stap om, dan zijn de eerste twee al opgeslagen.
- **De nginx in de container wacht 60 seconden.** `http.d/Recipes.conf.template`
  zet geen `proxy_read_timeout`, dus de standaard geldt. Een langzame aanroep
  geeft een 504, terwijl gunicorn erachter gewoon doorwerkt.
