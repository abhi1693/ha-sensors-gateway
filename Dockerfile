# syntax=docker/dockerfile:1.20
FROM python:3.14.9-alpine3.24@sha256:dbeb11d16bb95c3a7e3c25c842cb9f20cbc8c18f8be0461a4a0dcc861ad386a0

ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="Home Assistant Sensors Gateway" \
      org.opencontainers.image.description="Least-privilege gateway for Home Assistant Companion App sensor webhooks" \
      org.opencontainers.image.source="https://github.com/abhi1693/ha-sensors-gateway" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apk upgrade --no-cache \
    && python3 -m pip uninstall --yes pip \
    && addgroup -g 65532 -S gateway \
    && adduser -S -D -H -u 65532 -G gateway gateway

COPY --chown=65532:65532 src/ /app/src/

USER 65532:65532

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python3", "-m", "ha_sensors_gateway.healthcheck"]

ENTRYPOINT ["python3", "-m", "ha_sensors_gateway"]
