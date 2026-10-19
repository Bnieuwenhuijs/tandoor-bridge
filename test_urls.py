"""
Tests voor het opschonen en ontdubbelen van geplakte links.

De voorbeelden zijn de vormen die daadwerkelijk in de groep "Recepten dump"
langskomen: de deelknop van de AH-app, een link met een Facebook-id erachter,
en hele stukken chat-export in twee verschillende tijdnotaties.
"""

from urls import dedupe_key, extract_urls, normalize_url


def test_losse_link_blijft_heel():
    assert extract_urls("https://www.leukerecepten.nl/recepten/couscoussalade/") == [
        "https://www.leukerecepten.nl/recepten/couscoussalade/"
    ]


def test_whatsapp_bericht_met_meerdere_links():
    bericht = (
        "Lieke: deze moeten we maken! https://www.ah.nl/allerhande/recept/"
        "R-R921580/kip-korma-met-spinazie en die lasagnesoep van vorige week "
        "ook nog: https://www.leukerecepten.nl/recepten/lasagnesoep/ 😋"
    )
    assert extract_urls(bericht) == [
        "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie",
        "https://www.leukerecepten.nl/recepten/lasagnesoep/",
    ]


def test_whatsapp_export_beide_tijdnotaties():
    export = (
        "[3:23 PM, 8/9/2026] Bart: https://www.ah.nl/r/1197620\n"
        "8-9-2026 15:24 - Lieke: en deze https://www.voedingscentrum.nl/recept/pasta/\n"
        "[3:25 PM, 8/9/2026] Bart: 👍\n"
    )
    assert extract_urls(export) == [
        "https://www.ah.nl/allerhande/recept/R-R1197620",
        "https://www.voedingscentrum.nl/recept/pasta/",
    ]


def test_interpunctie_na_link_gaat_eraf():
    assert extract_urls("kijk hier: https://www.leukerecepten.nl/recepten/soep/.") == [
        "https://www.leukerecepten.nl/recepten/soep/"
    ]
    assert extract_urls("(https://www.leukerecepten.nl/recepten/soep/)") == [
        "https://www.leukerecepten.nl/recepten/soep/"
    ]


def test_haakje_in_de_link_blijft_staan():
    url = "https://nl.wikipedia.org/wiki/Kip_(gerecht)"
    assert extract_urls(url) == [url]


def test_tracking_verdwijnt():
    rommel = (
        "https://www.leukerecepten.nl/recepten/soep/"
        "?fbclid=IwAR123&utm_source=whatsapp&utm_medium=social&igsh=abc"
    )
    assert normalize_url(rommel) == "https://www.leukerecepten.nl/recepten/soep/"


def test_ah_deelparameters_verdwijnen():
    url = "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma?at_medium=app&at_campaign=share"
    assert normalize_url(url) == "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma"


def test_echte_parameters_blijven():
    url = "https://www.voedingscentrum.nl/recept?id=42&portie=4"
    assert normalize_url(url) == url


def test_ah_korte_link_wordt_herschreven():
    assert normalize_url("https://www.ah.nl/r/1197620") == (
        "https://www.ah.nl/allerhande/recept/R-R1197620"
    )
    assert normalize_url("https://ah.nl/r/1197620/") == (
        "https://ah.nl/allerhande/recept/R-R1197620"
    )
    assert normalize_url("https://www.ah.nl/r/1197620?at_medium=app") == (
        "https://www.ah.nl/allerhande/recept/R-R1197620"
    )


def test_mobiele_voedingscentrum_wordt_de_gewone_site():
    assert normalize_url("https://mobiel.voedingscentrum.nl/recept/pasta/") == (
        "https://www.voedingscentrum.nl/recept/pasta/"
    )


def test_http_wordt_https():
    assert normalize_url("http://www.leukerecepten.nl/recepten/soep/") == (
        "https://www.leukerecepten.nl/recepten/soep/"
    )


def test_fragment_verdwijnt():
    assert normalize_url("https://www.leukerecepten.nl/recepten/soep/#ingredienten") == (
        "https://www.leukerecepten.nl/recepten/soep/"
    )


def test_opschonen_is_herhaalbaar():
    """
    De opgeschoonde vorm gaat als source_url naar Tandoor, die dubbele
    recepten op een exacte match vergelijkt. Een tweede ronde door
    normalize_url() mag er dus niets meer aan veranderen.
    """
    for rauw in [
        "http://mobiel.voedingscentrum.nl/recept/pasta/?utm_source=x#top",
        "https://www.ah.nl/r/1197620?at_medium=app",
        "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie",
        "https://www.voedingscentrum.nl/recept?id=42&portie=4",
    ]:
        eenmaal = normalize_url(rauw)
        assert normalize_url(eenmaal) == eenmaal


def test_dezelfde_ah_link_in_drie_vormen_is_een_recept():
    bericht = (
        "https://www.ah.nl/r/921580 "
        "https://www.ah.nl/allerhande/recept/R-R921580 "
        "https://www.ah.nl/allerhande/recept/R-R921580/kip-korma-met-spinazie"
    )
    gevonden = extract_urls(bericht)
    assert len(gevonden) == 1
    # De eerste vorm uit het bericht wint, zodat de voorbeeldlijst de volgorde
    # van het bericht aanhoudt.
    assert gevonden == ["https://www.ah.nl/allerhande/recept/R-R921580"]


def test_sleutel_negeert_www_en_afsluitende_streep():
    assert dedupe_key("https://www.leukerecepten.nl/recepten/soep/") == (
        dedupe_key("https://leukerecepten.nl/recepten/soep")
    )


def test_sleutel_houdt_verschillende_recepten_apart():
    assert dedupe_key("https://www.ah.nl/allerhande/recept/R-R921580/kip") != (
        dedupe_key("https://www.ah.nl/allerhande/recept/R-R921581/kip")
    )


def test_lege_invoer():
    assert extract_urls("") == []
    assert extract_urls(None) == []
    assert extract_urls("geen enkele link hier") == []
    assert normalize_url("") == ""
