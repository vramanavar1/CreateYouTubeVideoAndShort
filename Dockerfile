# One image, two workloads. The scheduled Job runs `ytshort run`; the review app
# runs `ytshort review --serve`. No CMD is baked in -- each Container Apps
# resource supplies its own command, so there is a single artifact to build,
# scan, and promote.
#
# ffmpeg comes from Debian rather than a vendored static build: the distro
# package is patched by the security team, and it keeps a GPL binary out of the
# repository.

# Pin by digest at build time (deployment.md step 21 shows how to resolve it).
# The tag is kept here for readability; CI substitutes the digest.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a source-only change does not reinstall the world.
# --no-dev keeps pytest, ruff and friends out of the runtime image.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra azure

COPY src/ ./src/
COPY assets/ ./assets/
RUN uv sync --frozen --no-dev --extra azure

# Non-root. /data is the Azure Files mount and is the only writable path the app
# needs, so the image itself can be run with a read-only root filesystem.
RUN useradd --create-home --uid 10001 ytshort \
    && mkdir -p /data \
    && chown -R ytshort:ytshort /app /data
USER ytshort

ENV YTSHORT_DATA_DIR=/data/var \
    YTSHORT_LOG_FORMAT=json \
    YTSHORT_LOG_TO_FILE=false \
    YTSHORT_REVIEW_HOST=0.0.0.0 \
    YTSHORT_REVIEW_PORT=8080

EXPOSE 8080

# Fails the build if the entry point is broken, which is cheaper than finding out
# from a CrashLoopBackOff.
RUN ytshort --help > /dev/null
