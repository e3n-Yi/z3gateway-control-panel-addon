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

## OTA files

Open **OTA 文件** in the Z3Gateway panel, then drag files into the upload area or choose one or more files. The gateway starts with `-d /data/ota-files`, so its OTA scanner uses the same directory as uploads. After uploading, replacing, or deleting an image, stop and start the gateway to rebuild its image index, then use the existing OTA commands.

Files are persisted under `/data/ota-files`, survive add-on restarts and upgrades, and can be downloaded or deleted from the same panel. The maximum size of one file is 512 MB. Uploading the same filename asks before replacing it.


## Device center (0.2.0)

The default Devices page owns persistent IEEE-based device records. Existing
`devices.json` is backed up as `devices.json.pre-device-center` during migration;
`device-center.json` stores names, endpoint capabilities, Basic attributes and
persistent deletion markers. Back up `/data` before downgrading. Do not restore
an old device database over this file after forced deletion.

After gateway startup, saved short addresses are verified before device controls
are enabled. New joins trigger endpoint discovery and Basic reads. Sleeping or
nonresponding devices can remain partial; use **重新读取资料** after waking them.
Unsupported attributes are distinct from timeouts. Date Code is displayed as
reported. Standard device types and cluster information are advisory, not a
promise of complete device compatibility.

**强制删除设备** deletes host records and cancels queued work without requiring a
radio response. It does not erase remote network settings, remove network keys,
or create a blacklist. Historical logs and neighbor records cannot restore it;
a new accepted trust-center join can register it again. Normal leave requests
remain distinct from confirmed leave events.

Device actions use the chosen IEEE identity and current endpoint. Command
construction and sending are serialized with discovery and raw console commands.
APS delivery and device execution are separate: unconfirmed operations must not
be treated as device success. OTA notifications derive manufacturer, image type
and numeric firmware version from the selected image header; Basic SW Build ID
is not used as an OTA version. Restart the gateway after changing OTA files.

The add-on `build-info.json` records the source commit and packaged ARM64 binary
SHA-256. Hardware join/rejoin, sleeping devices and OTA must also be validated
with a real dongle; simulated tests do not establish radio interoperability.
