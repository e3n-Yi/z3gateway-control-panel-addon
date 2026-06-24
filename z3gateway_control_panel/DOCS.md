# Z3Gateway Control Panel Add-on

## Requirements

- Home Assistant OS on `aarch64`.
- A Silicon Labs Zigbee dongle attached to the Home Assistant host.
- The dongle must not be used by ZHA or Zigbee2MQTT at the same time.

## Options

- `serial_port`: serial device path, for example `/dev/ttyUSB0` or a `/dev/serial/by-id/...` path when available.
- `network_index`: z3gateway `-n` value. Current default is `1`.
- `baud_rate`: serial baud rate. Current default is `115200`.

## Access

The add-on enables Home Assistant Ingress. Optional direct port `8765/tcp` is disabled by default.
