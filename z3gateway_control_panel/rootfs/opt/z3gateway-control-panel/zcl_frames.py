"""Pure ZCL frame and attribute-record decoding for @Z3 rx/tx events.

No I/O and no gateway state: the automated test runner and its unit tests use
these helpers to read reports, read/write/configure responses and default
responses from the hexadecimal frame data printed by app.c.
"""

from __future__ import annotations

import struct
from typing import Any

CMD_READ_ATTRIBUTES = 0x00
CMD_READ_ATTRIBUTES_RESPONSE = 0x01
CMD_WRITE_ATTRIBUTES = 0x02
CMD_WRITE_ATTRIBUTES_RESPONSE = 0x04
CMD_CONFIGURE_REPORTING = 0x06
CMD_CONFIGURE_REPORTING_RESPONSE = 0x07
CMD_REPORT_ATTRIBUTES = 0x0A
CMD_DEFAULT_RESPONSE = 0x0B

FC_CLUSTER_SPECIFIC = 0x01
FC_MANUFACTURER_SPECIFIC = 0x04
FC_DIRECTION_SERVER_TO_CLIENT = 0x08
FC_DISABLE_DEFAULT_RESPONSE = 0x10

# Fixed-size scalar types: type id -> (size in bytes, signed).
_SCALAR_TYPES: dict[int, tuple[int, bool]] = {0x10: (1, False), 0x30: (1, False), 0x31: (2, False),
                                              0x38: (2, False), 0x39: (4, False), 0x3A: (8, False),
                                              0xE0: (4, False), 0xE1: (4, False), 0xE2: (4, False),
                                              0xE8: (2, False), 0xE9: (2, False), 0xEA: (4, False),
                                              0xF0: (8, False), 0xF1: (16, False)}
for _type in range(0x08, 0x10):
    _SCALAR_TYPES[_type] = (_type - 0x07, False)  # data8..data64
for _type in range(0x18, 0x20):
    _SCALAR_TYPES[_type] = (_type - 0x17, False)  # map8..map64
for _type in range(0x20, 0x28):
    _SCALAR_TYPES[_type] = (_type - 0x1F, False)  # uint8..uint64
for _type in range(0x28, 0x30):
    _SCALAR_TYPES[_type] = (_type - 0x27, True)  # int8..int64


class ZclDecodeError(ValueError):
    pass


def parse_zcl_frame(data: bytes | str) -> dict[str, Any] | None:
    """Split a ZCL frame into header fields and payload; None if too short."""
    if isinstance(data, str):
        try:
            data = bytes.fromhex(data)
        except ValueError:
            return None
    if not data:
        return None
    fc = data[0]
    offset = 1
    manufacturer = None
    if fc & FC_MANUFACTURER_SPECIFIC:
        if len(data) < 5:
            return None
        manufacturer = int.from_bytes(data[1:3], "little")
        offset = 3
    if len(data) < offset + 2:
        return None
    return {
        "frame_control": fc,
        "cluster_specific": bool(fc & FC_CLUSTER_SPECIFIC),
        "manufacturer_code": manufacturer,
        "server_to_client": bool(fc & FC_DIRECTION_SERVER_TO_CLIENT),
        "disable_default_response": bool(fc & FC_DISABLE_DEFAULT_RESPONSE),
        "seq": data[offset],
        "command_id": data[offset + 1],
        "payload": data[offset + 2:],
    }


def decode_value(data_type: int, buf: bytes, offset: int = 0) -> tuple[Any, int]:
    """Decode one attribute value; returns (value, next offset)."""
    if data_type in _SCALAR_TYPES:
        size, signed = _SCALAR_TYPES[data_type]
        if len(buf) < offset + size:
            raise ZclDecodeError(f"type 0x{data_type:02X} needs {size} bytes")
        raw = buf[offset:offset + size]
        if data_type == 0x39:
            value: Any = struct.unpack("<f", raw)[0]
        elif data_type == 0x3A:
            value = struct.unpack("<d", raw)[0]
        elif data_type in (0xF0, 0xF1):
            value = raw[::-1].hex().upper()
        else:
            value = int.from_bytes(raw, "little", signed=signed)
        return value, offset + size
    if data_type in (0x41, 0x42, 0x43, 0x44):
        width = 1 if data_type in (0x41, 0x42) else 2
        if len(buf) < offset + width:
            raise ZclDecodeError("string length missing")
        length = int.from_bytes(buf[offset:offset + width], "little")
        offset += width
        if length == (0xFF if width == 1 else 0xFFFF):
            return None, offset
        if len(buf) < offset + length:
            raise ZclDecodeError("string truncated")
        raw = buf[offset:offset + length]
        text = raw.decode("utf-8", errors="replace") if data_type in (0x42, 0x44) else raw.hex().upper()
        return text, offset + length
    raise ZclDecodeError(f"unsupported type 0x{data_type:02X}")


def parse_report_records(payload: bytes) -> list[dict[str, Any]]:
    """Report Attributes (0x0A): repeated attribute id, type, value."""
    records: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) < offset + 3:
            raise ZclDecodeError("report record truncated")
        attribute = int.from_bytes(payload[offset:offset + 2], "little")
        data_type = payload[offset + 2]
        try:
            value, offset = decode_value(data_type, payload, offset + 3)
        except ZclDecodeError:
            # Keep what was decoded; an unsupported type ends the record list.
            records.append({"attribute": attribute, "type": data_type, "raw": payload[offset + 3:].hex().upper()})
            break
        records.append({"attribute": attribute, "type": data_type, "value": value})
    return records


def parse_read_records(payload: bytes) -> list[dict[str, Any]]:
    """Read Attributes Response (0x01): attribute id, status, [type, value]."""
    records: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) < offset + 3:
            raise ZclDecodeError("read record truncated")
        attribute = int.from_bytes(payload[offset:offset + 2], "little")
        status = payload[offset + 2]
        offset += 3
        record: dict[str, Any] = {"attribute": attribute, "status": status}
        if status == 0:
            if len(payload) <= offset:
                raise ZclDecodeError("read record type missing")
            data_type = payload[offset]
            record["type"] = data_type
            try:
                record["value"], offset = decode_value(data_type, payload, offset + 1)
            except ZclDecodeError:
                record["raw"] = payload[offset + 1:].hex().upper()
                records.append(record)
                break
        records.append(record)
    return records


def parse_status_records(payload: bytes, *, with_direction: bool) -> list[dict[str, Any]]:
    """Write (0x04) or Configure Reporting (0x07) response status records.

    A single status byte means every attribute succeeded (or one shared status).
    """
    if not payload:
        raise ZclDecodeError("status payload missing")
    if len(payload) == 1:
        return [{"status": payload[0]}]
    size = 4 if with_direction else 3
    records = []
    for offset in range(0, len(payload) - size + 1, size):
        record = {"status": payload[offset]}
        if with_direction:
            record["direction"] = payload[offset + 1]
            record["attribute"] = int.from_bytes(payload[offset + 2:offset + 4], "little")
        else:
            record["attribute"] = int.from_bytes(payload[offset + 1:offset + 3], "little")
        records.append(record)
    return records


def parse_default_response(payload: bytes) -> dict[str, int]:
    if len(payload) < 2:
        raise ZclDecodeError("default response truncated")
    return {"command_id": payload[0], "status": payload[1]}


def decode_global(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Decode the payload of a profile-wide (global) command, if supported."""
    if frame["cluster_specific"]:
        return None
    command = frame["command_id"]
    payload = frame["payload"]
    if command == CMD_REPORT_ATTRIBUTES:
        return {"kind": "report", "records": parse_report_records(payload)}
    if command == CMD_READ_ATTRIBUTES_RESPONSE:
        return {"kind": "read-response", "records": parse_read_records(payload)}
    if command == CMD_WRITE_ATTRIBUTES_RESPONSE:
        return {"kind": "write-response", "records": parse_status_records(payload, with_direction=False)}
    if command == CMD_CONFIGURE_REPORTING_RESPONSE:
        return {"kind": "configure-response", "records": parse_status_records(payload, with_direction=True)}
    if command == CMD_DEFAULT_RESPONSE:
        return {"kind": "default-response", **parse_default_response(payload)}
    return None
