FROM ghcr.io/home-assistant/base-debian:trixie

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=unknown

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="UniFi Talk Alarm Add-on" \
    org.opencontainers.image.description="Local SIP and audio service for UniFi Talk alarm calls" \
    org.opencontainers.image.source="https://github.com/RAFd3v-HA/HACS-Unifi-Talk-Alarm-Addon"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        baresip-core \
        ca-certificates \
        espeak-ng \
        ffmpeg \
        python3 \
        python3-aiohttp \
    && command -v baresip >/dev/null \
    && command -v espeak-ng >/dev/null \
    && command -v ffmpeg >/dev/null \
    && command -v python3 >/dev/null \
    && for module in account.so aufile.so ctrl_tcp.so g711.so menu.so; do \
        test -f "/usr/lib/baresip/modules/${module}"; \
    done \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY app /app
COPY rootfs /

RUN chmod 0755 /etc/services.d/unifi-talk-alarm/run \
    && python3 -m compileall -q /app

WORKDIR /app
