# HeatpumpsN observer: ingest, storage, portal, LLM features.
# Built by deploy/docker-compose.yml; all state lives in the /data volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OBSERVER_HOME=/data

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin observer \
 && mkdir -p /data && chown observer:observer /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY observer ./observer
RUN pip install --no-cache-dir . && rm -rf /root/.cache

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

USER observer
VOLUME ["/data"]
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
