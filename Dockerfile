# syntax=docker/dockerfile:1.7

# Immutable multi-platform index for Docker Official Image
# python:3.12.14-slim-bookworm, recorded 2026-09-06.  Keeping the patch and
# digest together makes the image identity a reviewable input to later
# measured-image attestation rather than a moving tag.
FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_PROGRESS=1

WORKDIR /build/creek-tools
COPY creek-tools/pyproject.toml creek-tools/uv.lock ./

# Install the committed production resolution directly from uv.lock into a
# relocatable venv. The source tree is copied only in the final stage, so
# dependency reuse never risks retaining operator files.
RUN python -m pip install --no-cache-dir "uv==0.11.16" \
    && uv sync --locked --no-dev --no-install-project

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

LABEL org.opencontainers.image.source="https://github.com/Geoffe-Ga/Creek-Vault" \
      org.opencontainers.image.title="Creek single-vault runtime" \
      org.opencontainers.image.description="One authenticated /v1 consumer over one mounted Creek vault"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/opt/creek/creek-tools" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=dependencies /opt/venv /opt/venv
COPY creek-tools/creek /opt/creek/creek-tools/creek
COPY creek-tools/creek_mcp /opt/creek/creek-tools/creek_mcp
COPY docs/Ontology /opt/creek/docs/Ontology

# A newly-created named volume inherits /vault's ownership. Bind mounts must
# grant uid/gid 10001 access before startup; the entry point never escalates.
RUN groupadd --gid 10001 creek \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin creek \
    && mkdir --mode=0750 /vault \
    && chown 10001:10001 /vault

EXPOSE 8823
STOPSIGNAL SIGTERM
USER 10001:10001

# Do not declare VOLUME: Docker would silently supply an anonymous volume and
# defeat the entry point's "operator explicitly mounted durable storage" gate.
HEALTHCHECK --interval=10s --timeout=4s --start-period=20s --retries=3 CMD ["python", "-m", "creek_mcp.container_health"]
ENTRYPOINT ["python", "-m", "creek_mcp.container_runtime"]
