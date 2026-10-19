FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8096 \
    HOME=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/browsers

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps haalt de systeempakketten op die Chromium nodig heeft. Dit maakt
# het image groot (~1,2 GB); dat is de prijs van een echte browser, en de reden
# dat dit een aparte container is in plaats van onderdeel van de brug.
RUN playwright install --with-deps chromium \
 && chmod -R a+rX /browsers

RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin renderer
USER renderer

EXPOSE 8096

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8096') + '/healthz', timeout=3)"

# Eén worker en één thread: de browserthread binnen dit proces bezit Playwright,
# en de sync-API mag niet vanuit een andere thread gebruikt worden.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 1 --timeout 180 --bind 0.0.0.0:${PORT} --error-logfile - 'app:create_app()'"]
