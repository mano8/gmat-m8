# syntax=docker/dockerfile:1
#
# gmat-m8 backup image (plan §7.1, §9 "Backups and recovery points").
# pg_dump / pg_restore / psql (same PostgreSQL 18 as `db`), restic and a cron daemon, all as
# uid 1000 on a read-only root filesystem. Published, signed and deployed by digest like the
# application image: the two are released together.
#
# The base moves with the `db` service's image (same digest in docker-compose.yml).

ARG POSTGRES_IMAGE=postgres:18-alpine@sha256:10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5
FROM ${POSTGRES_IMAGE}

ARG APP_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="gmat-m8-backup" \
      org.opencontainers.image.description="Snapshot-consistent PostgreSQL backups with append-only offsite upload" \
      org.opencontainers.image.source="https://github.com/mano8/gmat-m8" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

# bash for the scripts, jq for the manifest, restic for the offsite repository, curl for the
# heartbeat. Upgraded on each build so Alpine fixes arrive without exact pins.
RUN apk upgrade --no-cache \
    && apk add --no-cache bash jq restic curl coreutils \
    && rm -f /usr/local/bin/gosu
# gosu (the base image's privilege-dropping helper, for its own entrypoint) is not used here
# and ships an old Go standard library; removing it deletes the findings instead of ignoring them.

# supercronic: a container-native cron that runs as any uid (busybox crond needs root to
# switch users), logs every job to stdout and passes the container environment to its jobs.
# Pinned release, verified against the published sha256 of each architecture.
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.51
ARG SUPERCRONIC_SHA256_AMD64=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123
ARG SUPERCRONIC_SHA256_ARM64=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
        amd64) sha256="${SUPERCRONIC_SHA256_AMD64}" ;; \
        arm64) sha256="${SUPERCRONIC_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}"; \
    echo "${sha256}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic

# uid/gid 1000 matches the `deploy` user on the VPS, so ./backups lines up on the host.
RUN addgroup -g 1000 backup \
    && adduser -D -u 1000 -G backup -H -s /sbin/nologin backup \
    && mkdir -p /backups /scratch \
    && chown backup:backup /backups /scratch \
    && chmod 0700 /backups /scratch

COPY --chmod=0755 docker/backup/backup.sh docker/backup/restore.sh docker/backup/common.sh /usr/local/lib/gmat-backup/
COPY docker/backup/manifest.sql /usr/local/lib/gmat-backup/manifest.sql
RUN ln -s /usr/local/lib/gmat-backup/backup.sh /usr/local/bin/backup-now \
    && ln -s /usr/local/lib/gmat-backup/backup.sh /usr/local/bin/backup.sh \
    && ln -s /usr/local/lib/gmat-backup/restore.sh /usr/local/bin/restore \
    && ln -s /usr/local/lib/gmat-backup/restore.sh /usr/local/bin/restore.sh

COPY --chmod=0644 docker/backup/crontab /etc/gmat-backup.crontab

ENV BACKUP_DIR=/backups \
    BACKUP_SCRATCH_DIR=/scratch \
    RESTIC_CACHE_DIR=/tmp/restic-cache \
    APP_VERSION=${APP_VERSION}

USER 1000:1000
WORKDIR /backups
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["daemon"]
