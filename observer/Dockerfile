# HeatpumpsN observer: ingest, storage, portal, LLM features.
# Built by deploy/docker-compose.yml; all state lives in the /data volume.
# native/ (the firmware's MPC core) comes with the release bundle; in a
# repository checkout create it first with scripts/native.sh.

# The controller's MPC core for the simulations (observer/sim/fwcore.py).
FROM python:3.12-slim AS mpc
LABEL org.heatpumpsn.component=observer
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
COPY native /src
COPY observer/sim/mpc_shim.cpp /src/
RUN g++ -std=c++17 -O2 -fPIC -shared -I/src/include /src/src/mpc_core.cpp /src/mpc_shim.cpp -o /libmpc_core.so

FROM python:3.12-slim
LABEL org.heatpumpsn.component=observer

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OBSERVER_HOME=/data

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin observer \
 && mkdir -p /data && chown observer:observer /data

# The agent sandbox: bubblewrap and the sandbox's own Python environment. It
# works only when the container may create namespaces (docker-compose.sandbox.yml).
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv /opt/sandbox/venv \
 && /opt/sandbox/venv/bin/pip install --no-cache-dir numpy pandas scipy matplotlib pytest pyyaml \
 && rm -rf /root/.cache
ENV OBSERVER_SANDBOX_VENV=/opt/sandbox/venv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY observer ./observer
RUN pip install --no-cache-dir . && rm -rf /root/.cache

COPY --from=mpc /libmpc_core.so /opt/mpc/libmpc_core.so
ENV OBSERVER_MPC_LIB=/opt/mpc/libmpc_core.so

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

USER observer
VOLUME ["/data"]
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
