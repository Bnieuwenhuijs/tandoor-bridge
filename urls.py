"""
URL's uit geplakte tekst halen en in één vaste vorm brengen.

De invoer is zelden een nette lijst: meestal een WhatsApp-bericht, soms een
hele chat-export, soms een losse link met trackingrommel erachter. Dit
bestand maakt daar een opgeschoonde, ontdubbelde lijst van.

Het haalt nooit een pagina op en weet niets van Tandoor. Dat maakt het in
z'n geheel te testen zonder netwerk.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Links in een bericht staan zelden alleen: er volgt interpunctie, ze staan
# tussen haakjes of tussen aanhalingstekens. De tekenklasse pakt daarom ruim
# en _trim_trailing() knipt achteraf af wat bij de zin hoorde.
URL_RE = re.compile(r"https?://[^\s<>\"'«»“”]+", re.IGNORECASE)

# Tekens die aan het eind van een link vrijwel altijd bij de zin horen.
_TRAILING = ".,;:!?'\"’”>*_"

# Trackingparameters. De exacte namen komen van de deelknoppen van Facebook,
# Google, Instagram en Mailchimp; de voorvoegsels dekken utm_source en
# at_medium en de hele verdere familie.
_TRACKING_EXACT = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "igsh", "igshid",
    "mc_cid", "mc_eid", "ssrc",
})
_TRACKING_PREFIXES = ("utm_", "at_")

# De deelknop van de Albert Heijn-app maakt links als ah.nl/r/1196876. Die
# geven zelf een 404, maar hetzelfde nummer werkt wel als recept-URL.
_AH_SHORT_PATH_RE = re.compile(r"^/r/(\d+)/?$")

# Hetzelfde recept komt langs met en zonder slug achter het nummer:
# /allerhande/recept/R-R921580 en /allerhande/recept/R-R921580/kip-korma.
_AH_RECIPE_PATH_RE = re.compile(r"^/allerhande/recept/(R-R\d+)", re.IGNORECASE)

# mobiel.voedingscentrum.nl stuurt niet door maar geeft een eigen, kalere
# pagina terug waar minder op te vinden is.
_HOST_REWRITES = {
    "mobiel.voedingscentrum.nl": "www.voedingscentrum.nl",
}


def _trim_trailing(url: str) -> str:
    """Interpunctie afknippen die bij de zin hoort, niet bij de link."""
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    # Een sluithaakje hoort er alleen af als het er niet bij hoort: links als
    # /wiki/Kip_(gerecht) moeten heel blijven.
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def _is_tracking(name: str) -> bool:
    name = name.lower()
    return name in _TRACKING_EXACT or name.startswith(_TRACKING_PREFIXES)


def _clean_query(query: str) -> str:
    if not query:
        return ""
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if not _is_tracking(k)]
    return urlencode(kept)


def normalize_url(url: str) -> str:
    """
    Breng een link in de vorm waarin we hem ophalen en opslaan.

    Deze string gaat straks ook als source_url naar Tandoor, en Tandoor
    vergelijkt dubbele recepten op een exacte match. Hier iets veranderen
    betekent dus dat eerder geïmporteerde recepten niet meer herkend worden.
    """
    url = _trim_trailing((url or "").strip())
    if not url:
        return ""

    parts = urlsplit(url)
    host = parts.netloc.lower()
    host = _HOST_REWRITES.get(host, host)
    path, query = parts.path, parts.query

    if host.endswith("ah.nl"):
        if short := _AH_SHORT_PATH_RE.match(path):
            # De korte vorm draagt geen informatie die we willen bewaren, dus
            # de trackingparameters van de deelknop gaan er in hun geheel af.
            path, query = f"/allerhande/recept/R-R{short.group(1)}", ""

    # Het fragment (#ingredienten) wijst naar een stuk van dezelfde pagina en
    # zegt niets over welk recept het is.
    return urlunsplit(("https", host, path, _clean_query(query), ""))


def dedupe_key(url: str) -> str:
    """
    Sleutel waaronder we bijhouden of een recept al geïmporteerd is.

    Twee links die hetzelfde recept aanwijzen moeten dezelfde sleutel geven,
    ook als de ene vorm een slug heeft en de andere niet.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.endswith("ah.nl"):
        if recipe := _AH_RECIPE_PATH_RE.match(parts.path):
            return f"ah:{recipe.group(1).upper()}"

    # www. is voor de server hetzelfde adres; een afsluitende schuine streep
    # ook. Het pad houdt verder z'n hoofdletters: niet elke site behandelt
    # /Recept en /recept als dezelfde pagina.
    host = host.removeprefix("www.")
    key = host + parts.path.rstrip("/")
    return f"{key}?{parts.query}" if parts.query else key


def extract_urls(text: str) -> list[str]:
    """
    Alle links uit een stuk tekst, opgeschoond en ontdubbeld.

    De volgorde van de tekst blijft staan: zo staat de voorbeeldlijst in de
    interface in dezelfde volgorde als het bericht dat eruit geplakt is.
    """
    seen, found = set(), []
    for raw in URL_RE.findall(text or ""):
        url = normalize_url(raw)
        if not url:
            continue
        key = dedupe_key(url)
        if key in seen:
            continue
        seen.add(key)
        found.append(url)
    return found
