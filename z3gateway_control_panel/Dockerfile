FROM debian:bookworm-slim

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL   io.hass.version="${BUILD_VERSION}"   io.hass.type="app"   io.hass.arch="${BUILD_ARCH}"

RUN apt-get update   && apt-get install -y --no-install-recommends bash ca-certificates python3   && rm -rf /var/lib/apt/lists/*

COPY rootfs /

RUN chmod a+x /run.sh   && chmod a+x /opt/z3gateway-control-panel/build/debug/zigbee_z3_gateway

CMD ["/run.sh"]
