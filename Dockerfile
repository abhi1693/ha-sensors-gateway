# syntax=docker/dockerfile:1.20
FROM python:3.14.1-alpine3.23@sha256:b80c82b1a282283bd3e3cd3c6a4c895d56d1385879c8c82fa673e9eb4d6d4aa5

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

RUN addgroup -g 65532 -S gateway \
    && adduser -S -D -H -u 65532 -G gateway gateway

COPY --chown=65532:65532 src/ /app/src/

USER 65532:65532

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import os,socket; socket.create_connection(('127.0.0.1', int(os.getenv('PORT', '8080'))), 3).close()"]

ENTRYPOINT ["python3", "-m", "ha_sensors_gateway"]
