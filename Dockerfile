# One image, two workloads. The scheduled Job runs `ytshort run`; the review app
# runs `ytshort review --serve`. No CMD is baked in -- each Container Apps
# resource supplies its own command, so there is a single artifact to build,
# scan, and promote.
#
# ffmpeg comes from Debian rather than a vendored static build: the distro
# package is patched by the security team, and it keeps a GPL binary out of the
# repository.
#
# Two stages. The build stage owns uv and the compiler-adjacent work; the runtime
# stage receives only the finished virtualenv, so no package manager ships to
# production. Anything with a shell in the running container is attack surface.

# Pinned by tag here and by digest in CI (deployment.md step 21 shows how to
# resolve one). Both are ARGs so the pipeline can substitute
# `python@sha256:...` / a digest-pinned uv without editing this file.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
# Never `:latest`. A mutable tag on an image we copy a *binary* out of means
# whoever controls that tag controls this build.
#
# Keep this in step with the uv that writes uv.lock -- an older uv may not read a
# lockfile written by a newer one, and the failure surfaces as a confusing
# resolution error inside the build rather than at `uv lock` time.
ARG UV_VERSION=0.9.18

# A named stage, because `COPY --from` does not expand variables -- only `FROM`
# does. This is the documented workaround for pinning the uv image by ARG.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uvbin

FROM ${PYTHON_IMAGE} AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=uvbin /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a source-only change does not reinstall the world.
# --no-dev keeps pytest, ruff and friends out of the runtime image.
# ccol is a workspace member, so its manifest has to be present for the
# dependency-only resolve, and --no-install-workspace replaces
# --no-install-project now that the workspace has more than one member.
COPY pyproject.toml uv.lock README.md ./
COPY libs/ccol/pyproject.toml libs/ccol/README.md ./libs/ccol/
RUN uv sync --frozen --no-dev --no-install-workspace --extra azure --extra observability

COPY src/ ./src/
COPY libs/ ./libs/
COPY assets/ ./assets/
RUN uv sync --frozen --no-dev --extra azure --extra observability


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root. /data is the Azure Files mount and is the only writable path the app
# needs, so the image itself can be run with a read-only root filesystem.
RUN useradd --create-home --uid 10001 ytshort \
    && mkdir -p /data \
    && chown -R ytshort:ytshort /app /data

COPY --from=build --chown=ytshort:ytshort /opt/venv /opt/venv
COPY --from=build --chown=ytshort:ytshort /app/src /app/src
COPY --from=build --chown=ytshort:ytshort /app/libs /app/libs
COPY --from=build --chown=ytshort:ytshort /app/assets /app/assets

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
