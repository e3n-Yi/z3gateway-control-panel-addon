# Z3Gateway Control Panel Add-on

## Requirements

- Home Assistant OS on `aarch64`.
- A Silicon Labs Zigbee dongle attached to the Home Assistant host.
- The dongle must not be used by ZHA or Zigbee2MQTT at the same time.

## Options

- `serial_port`: serial device path for the Z3Gateway dongle, for example `/dev/ttyUSB0` or a `/dev/serial/by-id/...` path when available.
- `calibration_serial_port`: optional serial device path for the zero-cross calibration instrument. It is opened at 9600 baud only while automatic calibration is running.
- `network_index`: z3gateway `-n` value. Current default is `1`.
- `baud_rate`: serial baud rate. Current default is `115200`.

## Access

The add-on enables Home Assistant Ingress. Optional direct port `8765/tcp` is disabled by default.

## Zero-cross calibration

Configure `calibration_serial_port`, start the gateway, open a device detail drawer, then use **开始自动校准**. The add-on toggles the target device, reads `55 AA` instrument frames, and sends `raw 0xEEEE` calibration commands when a measured value is greater than 500 us.
