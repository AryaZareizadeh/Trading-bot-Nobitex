FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADER_DATA_DIR=/data \
    TRADER_HOST=0.0.0.0 \
    TRADER_PORT=8011

WORKDIR /app
COPY requirements.txt requirements-lock.txt requirements-training.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-lock.txt \
    && useradd --uid 10001 --create-home trader \
    && mkdir /data && chown trader:trader /data
ARG TRAINER_DEVICE=none
# Keep large GPU downloads across interrupted builds, outside the image layers.
RUN --mount=type=cache,id=nobitex-training-pip,target=/root/.cache/pip,sharing=locked \
    if [ "$TRAINER_DEVICE" = cuda ]; then \
        mkdir -p /root/.cache/pip/trainer-wheels; \
        for wheel in /root/.cache/pip/trainer-wheels/*.whl; do \
            if [ -f "$wheel" ]; then pip install --no-deps "$wheel" || exit 1; fi; \
        done; \
        pip install --retries 10 --timeout 60 --find-links /root/.cache/pip/trainer-wheels -r requirements-training.txt; \
    elif [ "$TRAINER_DEVICE" = cpu ]; then pip install --retries 10 --timeout 60 torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu; \
    elif [ "$TRAINER_DEVICE" != none ]; then echo "TRAINER_DEVICE must be none, cuda or cpu" >&2; exit 1; fi
COPY --chown=trader:trader . .
USER trader
EXPOSE 8011
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/healthz', timeout=3)" || exit 1
CMD ["python", "main.py"]
