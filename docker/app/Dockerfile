# syntax=docker/dockerfile:1
#
# gmat-m8 application image (plan §7.1). One image runs `web`, `worker` and `migrate`
# (docker/app/entrypoint.sh). Stages: assets -> builder -> runtime, plus a `dev` target.
#
# Every external reference is pinned by digest. The Python base moves in lock-step with
# fa-auth-m8's images, never alone; Dependabot's `docker` ecosystem proposes the updates.

ARG PYTHON_IMAGE=python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff

FROM ${UV_IMAGE} AS uv

# ---------- assets: Tailwind CSS (runs once, on the build host's architecture) ----------
# The CSS does not depend on the CPU architecture, so it is built once per build, never
# under emulation. The standalone CLI is verified against a pinned sha256.
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS assets

ARG TAILWIND_VERSION=v4.3.3
ARG TAILWIND_SHA256_X64=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
ARG TAILWIND_SHA256_ARM64=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195

RUN set -eu; \
    case "$(uname -m)" in \
        x86_64)  asset=tailwindcss-linux-x64;   sha256="${TAILWIND_SHA256_X64}" ;; \
        aarch64) asset=tailwindcss-linux-arm64; sha256="${TAILWIND_SHA256_ARM64}" ;; \
        *) echo "unsupported build architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request as u; u.urlretrieve(sys.argv[1], '/usr/local/bin/tailwindcss')" \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"; \
    echo "${sha256}  /usr/local/bin/tailwindcss" | sha256sum --check --strict -; \
    chmod 0755 /usr/local/bin/tailwindcss

WORKDIR /app
# Only what Tailwind scans (templates, partials inside apps, the small JS file) and the entry CSS.
COPY static/src ./static/src
COPY static/js ./static/js
COPY templates ./templates
COPY apps ./apps
RUN tailwindcss --input static/src/app.css --output static/dist/app.css --minify

# ---------- locale: compiled message catalogs (the .mo files are not committed) ----------
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS locale

# Build-only stage: nothing from it but the .mo files reaches the runtime image, and the base
# image is pinned by digest, so the gettext version is not pinned separately.
# hadolint ignore=DL3008
RUN apt-get update     && apt-get install -y --no-install-recommends gettext     && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY locale ./locale
RUN find locale -name '*.po' -exec sh -c 'msgfmt --check-format -o "${1%.po}.mo" "$1"' _ {} \;

# ---------- builder: locked, hash-verified Python dependencies ----------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
# `--frozen`: install exactly uv.lock (hashes included), never re-resolve.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Same lock, plus the `dev` dependency group. Only the `dev` target uses it.
FROM builder AS builder-dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# ---------- runtime ----------
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="gmat-m8" \
      org.opencontainers.image.description="Materials app for electrical-installation companies" \
      org.opencontainers.image.source="https://github.com/mano8/gmat-m8" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod \
    APP_VERSION=${APP_VERSION}

# Debian patch layer, the form shared with fa-auth-m8: every Debian package self-heals on
# each build through `apt-get upgrade`; nothing is exact-pinned and nothing is installed that
# the base does not already ship. Exact `=` pins would freeze the image on the next advisory.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# uid/gid 1000 matches the `deploy` user on the VPS, so bind-mounted directories line up.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

# Source is owned by root and read-only to the app user; nothing writes into /app at run time.
COPY manage.py ./
COPY config ./config
COPY apps ./apps
COPY templates ./templates
COPY static ./static
COPY --from=locale /app/locale ./locale
COPY --from=assets /app/static/dist ./static/dist
COPY --chmod=0755 docker/app/entrypoint.sh docker/app/gunicorn.conf.py ./docker/app/

# collectstatic needs neither a database nor a secret (config.settings.build). WhiteNoise
# serves the result; bytecode is compiled now because the root filesystem is read-only.
RUN DJANGO_SETTINGS_MODULE=config.settings.build python manage.py collectstatic --noinput \
    && python -m compileall -q /app/apps /app/config /app/manage.py

# pip is dead weight at run time (the release set is installed, hash-locked, in the builder)
# and its vendored dependency tree would surface as findings that are not ours. Removing it
# deletes the code instead of ignoring the report.
RUN /usr/local/bin/python -m pip uninstall -y pip     && rm -rf "$(/usr/local/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/pip"     && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["/app/docker/app/entrypoint.sh"]
CMD ["web"]

# ---------- dev: runtime + dev dependencies, runs runserver ----------
# Used only by docker-compose.dev.yml, which bind-mounts the source over /app.
FROM runtime AS dev
COPY --from=builder-dev /opt/venv /opt/venv
ENV DJANGO_SETTINGS_MODULE=config.settings.dev
CMD ["runserver"]

# The default target is the last stage, so make the runtime image explicit.
FROM runtime AS final
