#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH=/data/options.json

option() {
  local key="$1"
  local fallback="$2"
  python3 - "$CONFIG_PATH" "$key" "$fallback" <<'PY'
import json
import sys
path, key, fallback = sys.argv[1:4]
try:
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
value = data.get(key, fallback)
if value is None:
    value = fallback
print(value)
PY
}

CONFIGURED_SERIAL_PORT="$(option serial_port /dev/ttyUSB0)"
SERIAL_PORT="$CONFIGURED_SERIAL_PORT"
if [ -n "$CONFIGURED_SERIAL_PORT" ] && [ -e "$CONFIGURED_SERIAL_PORT" ]; then
  if ln -sfn "$CONFIGURED_SERIAL_PORT" /dev/z3gw; then
    SERIAL_PORT=/dev/z3gw
  else
    printf '[z3gateway-control-panel] warning: failed to create /dev/z3gw alias for %s\n' "$CONFIGURED_SERIAL_PORT"
  fi
fi
NETWORK_INDEX="$(option network_index 1)"
BAUD_RATE="$(option baud_rate 115200)"

export Z3_PANEL_GATEWAY_ROOT=/opt/z3gateway-control-panel
export Z3_PANEL_ALLOWED_ROOT=/opt/z3gateway-control-panel
export Z3_PANEL_DEFAULT_EXECUTABLE=/opt/z3gateway-control-panel/build/debug/zigbee_z3_gateway
export Z3_PANEL_DATA_DIR=/data
export Z3_PANEL_HOST=0.0.0.0
export Z3_PANEL_PORT=8765
export Z3_PANEL_CONFIGURED_SERIAL_PORT="$CONFIGURED_SERIAL_PORT"
export Z3_PANEL_DEFAULT_SERIAL_PORT="$SERIAL_PORT"
export Z3_PANEL_DEFAULT_NETWORK_INDEX="$NETWORK_INDEX"
export Z3_PANEL_DEFAULT_BAUD_RATE="$BAUD_RATE"
export PYTHONUNBUFFERED=1

mkdir -p /data/logs

printf '[z3gateway-control-panel] configured_serial_port=%s\n' "$CONFIGURED_SERIAL_PORT"
printf '[z3gateway-control-panel] runtime_serial_port=%s\n' "$SERIAL_PORT"
printf '[z3gateway-control-panel] network_index=%s baud_rate=%s\n' "$NETWORK_INDEX" "$BAUD_RATE"
printf '[z3gateway-control-panel] executable=%s\n' "$Z3_PANEL_DEFAULT_EXECUTABLE"
printf '[z3gateway-control-panel] data_dir=%s\n' "$Z3_PANEL_DATA_DIR"
printf '[z3gateway-control-panel] starting web server on %s:%s\n' "$Z3_PANEL_HOST" "$Z3_PANEL_PORT"

cd /opt/z3gateway-control-panel
exec python3 -u server.py
