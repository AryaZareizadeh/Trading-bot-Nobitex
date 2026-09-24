FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADER_DATA_DIR=/data \
    TRADER_HOST=0.0.0.0 \
    TRADER_PORT=8011

WORKDIR /app
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-lock.txt \
    && useradd --uid 10001 --create-home trader \
    && mkdir /data && chown trader:trader /data
COPY --chown=trader:trader . .
USER trader
EXPOSE 8011
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/', timeout=3)" || exit 1
CMD ["python", "main.py"]
