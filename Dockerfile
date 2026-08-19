# syntax=docker/dockerfile:1.20
FROM python:3.14.7-alpine3.23@sha256:6b8f06d04d5305c1d1288435388df9165ab41e681fae6439d6349d8053cc3f83

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

RUN python3 -m pip uninstall --yes pip \
    && addgroup -g 65532 -S gateway \
    && adduser -S -D -H -u 65532 -G gateway gateway

COPY --chown=65532:65532 src/ /app/src/

USER 65532:65532

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python3", "-m", "ha_sensors_gateway.healthcheck"]

ENTRYPOINT ["python3", "-m", "ha_sensors_gateway"]
