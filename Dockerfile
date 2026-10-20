FROM python:3.12-slim

# HOME is er voor gunicorn: die zet z'n control socket in $HOME, en de
# gebruiker waaronder we draaien heeft geen eigen map. Zonder deze regel staat
# er bij elke start een "Control server error: Permission denied" in het
# logboek - een fout die niets kapotmaakt maar wel verkeerd doet vermoeden.
# (Het commentaar staat bewust vóór de instructie en niet tussen de
# regelvoortzettingen, want daar is niet elke Dockerfile-parser blij mee.)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8095 \
    HOME=/tmp

WORKDIR /app

# Eerst alleen de requirements, zodat een codewijziging deze laag niet
# ongeldig maakt. curl_cffi heeft een abi3-wheel voor manylinux x86_64, dus
# er wordt hier niets gecompileerd en er zijn geen build-pakketten nodig.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ah_api.py fetcher.py importqueue.py sorteerronde.py sorteren.py tandoor.py urls.py ./
COPY mock_tandoor.py mock_opslag.py mock_ah.py smoketest.py ./
COPY templates/ ./templates/

# Deze lijst is met de hand bijgehouden en is al twee keer achtergebleven bij
# de code; een vergeten regel breekt pas bij de start van de container.
# test_image.py leest de COPY-regels en loopt de imports na, en dit is de
# tweede vangst: hier valt de build om, met de naam van de module erbij, in
# plaats van dat er een image uitkomt die niet opstart.
RUN python -c "import app, smoketest, mock_tandoor, mock_ah"

# Niet als root draaien, en /data moet van de brug zijn want daar komt de
# SQLite-database in.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin brug \
 && mkdir -p /data \
 && chown brug:brug /data
USER brug

EXPOSE 8095
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8095') + '/healthz', timeout=3)"

# Eén worker, en dat is geen zuinigheid: de wachtrij-thread leeft in dit
# proces. Twee workers zijn twee threads die dezelfde rij leegtrekken, en dan
# wordt elk recept dubbel geïmporteerd. Meer verkeer aankunnen gaat via
# --threads, niet via --workers.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT} --error-logfile - 'app:create_app()'"]
