# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install . \
    && rm -rf /opt/venv/lib/python3.*/site-packages/pip* \
              /opt/venv/lib/python3.*/site-packages/setuptools* \
              /opt/venv/lib/python3.*/site-packages/pkg_resources \
              /opt/venv/bin/pip*


FROM python:3.13-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DIR=/data \
    HEALTH_PORT=8080

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN rm -rf /usr/local/lib/python3.*/site-packages/pip* \
           /usr/local/lib/python3.*/site-packages/setuptools* \
           /usr/local/lib/python3.*/site-packages/pkg_resources \
           /usr/local/bin/pip*

RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --no-create-home app \
    && mkdir -p /data \
    && chown 1000:1000 /data

COPY --from=builder /opt/venv /opt/venv

USER 1000:1000
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["mealie-gkeep-sync"]
