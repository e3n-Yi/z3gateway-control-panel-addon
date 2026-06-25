#!/usr/bin/env python3
"""Local web control panel for the Zigbee Z3 gateway host.

This server intentionally uses only the Python standard library so it can run
on a VM without installing packages first.
"""

from __future__ import annotations

import glob
import json
import mimetypes
import os
import pty
import queue
import re
import select
import signal
import subprocess
import termios
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_DIR = APP_DIR / "config"
DATA_DIR = Path(os.environ.get("Z3_PANEL_DATA_DIR", str(APP_DIR / "data"))).expanduser().resolve()
LOG_DIR = DATA_DIR / "logs"
COMMANDS_FILE = CONFIG_DIR / "commands.json"
DEVICES_FILE = DATA_DIR / "devices.json"
INFO_COMMAND = "info"
INFO_START_DELAY_SECONDS = 1.2
NEIGHBOR_TABLE_COMMAND = "plugin stack-diagnostics neighbor-table"
NEIGHBOR_TABLE_JOIN_DELAY_SECONDS = 2.0
NEIGHBOR_TABLE_COOLDOWN_SECONDS = 5.0
GATEWAY_PROMPT = "zigbee_z3_gateway>"
GATEWAY_COMMAND_SEQUENCE_DELAY_SECONDS = float(os.environ.get("Z3_PANEL_COMMAND_SEQUENCE_DELAY_SECONDS", "0.6"))
GATEWAY_COMMAND_PROMPT_TIMEOUT_SECONDS = float(os.environ.get("Z3_PANEL_COMMAND_PROMPT_TIMEOUT_SECONDS", "1.2"))

def resolve_gateway_root() -> Path:
    env_root = os.environ.get("Z3_PANEL_GATEWAY_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    if APP_DIR.parent.name == "tools":
        return APP_DIR.parent.parent.resolve()

    legacy_root = Path("/home/e3n/SimplicityStudio/v6_workspace/zigbee_z3_gateway")
    if legacy_root.exists():
        return legacy_root.resolve()

    return APP_DIR.resolve()


GATEWAY_ROOT = resolve_gateway_root()
ALLOWED_ROOT = Path(os.environ.get("Z3_PANEL_ALLOWED_ROOT", str(GATEWAY_ROOT))).expanduser().resolve()
DEFAULT_EXECUTABLE = Path(
    os.environ.get(
        "Z3_PANEL_DEFAULT_EXECUTABLE",
        str(GATEWAY_ROOT / "build" / "debug" / "zigbee_z3_gateway"),
    )
).expanduser().resolve()
DEFAULT_HOST = os.environ.get("Z3_PANEL_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("Z3_PANEL_PORT", "8765"))
DEFAULT_SERIAL_PORT = os.environ.get("Z3_PANEL_DEFAULT_SERIAL_PORT", "")
CONFIGURED_SERIAL_PORT = os.environ.get("Z3_PANEL_CONFIGURED_SERIAL_PORT", DEFAULT_SERIAL_PORT)
DEFAULT_NETWORK_INDEX = os.environ.get("Z3_PANEL_DEFAULT_NETWORK_INDEX", "1")
DEFAULT_BAUD_RATE = os.environ.get("Z3_PANEL_DEFAULT_BAUD_RATE", "115200")
CALIBRATION_SERIAL_PORT = os.environ.get("Z3_PANEL_CALIBRATION_SERIAL_PORT", "")
CALIBRATION_BAUD_RATE = 9600
ZERO_CROSS_HALF_CYCLE_US = 10000
ZERO_CROSS_SUCCESS_WINDOW_US = 500
ZERO_CROSS_MAX_ROUNDS = 20
ZERO_CROSS_MEASUREMENT_TIMEOUT_SECONDS = 8.0
ZERO_CROSS_SWITCH_INTERVAL_SECONDS = 3.0
ZERO_CROSS_CALIBRATION_SETTLE_SECONDS = 2.0
ZERO_CROSS_MAX_CONSECUTIVE_TIMEOUTS = 3


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def resolve_runtime_serial_port(serial_port: str) -> str:
    serial_port = str(serial_port or "").strip()
    if not serial_port:
        return ""
    real = os.path.realpath(serial_port) if os.path.exists(serial_port) else serial_port
    if re.fullmatch(r"/dev/tty(?:USB|ACM)\d+", real):
        return real
    return serial_port


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def read_json_file(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return fallback


def infer_workdir(executable: Path) -> Path:
    """Use the project root for Simplicity Studio build outputs."""
    parent = executable.parent
    if parent.name in {"debug", "release"} and parent.parent.name == "build":
        return parent.parent.parent
    return parent


def clean_log_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def normalize_node_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"0x[0-9A-Fa-f]{1,4}", str(value))
    if not match:
        return None
    return f"0x{int(match.group(0), 16):04X}"


def uint16_hex_bytes(value: int) -> str:
    value = max(0, min(0xFFFF, int(value)))
    return f"{(value >> 8) & 0xFF:02X} {value & 0xFF:02X}"


class ZigbeeDeviceRegistry:
    TC_HANDLER_RE = re.compile(
        r"Trust Center Join Handler:\s*status\s*=\s*(.*?),.*?shortid\s+(0x[0-9A-Fa-f]{4})"
    )
    LEAVE_COMMAND_RE = re.compile(r"^\s*>?\s*zdo\s+leave\s+(0x[0-9A-Fa-f]{1,4})\s+\d+\s+\d+\s*$", re.IGNORECASE)
    LEAVE_RESPONSE_RE = re.compile(r"RX:\s*ZDO,\s*command\s+0x8034,\s*status:\s*0x00", re.IGNORECASE)
    ANNOUNCE_RE = re.compile(r"Device Announce:\s*(0x[0-9A-Fa-f]{4})")
    INFO_NODE_RE = re.compile(r"node \[(?:\(>\))?([0-9A-Fa-f]{16})\]")
    INFO_NODE_ID_RE = re.compile(r"nodeID \[(0x[0-9A-Fa-f]{1,4})\]")
    NEIGHBOR_RE = re.compile(
        r"^\s*\d+:\s+(0x[0-9A-Fa-f]{4})\s+(\d+)\s+\d+\s+\d+\s+\d+\s+"
        r"0x[0-9A-Fa-f]+\s+\(>\)([0-9A-Fa-f]{16})\s*$"
    )

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.devices: dict[str, dict[str, Any]] = {}
        self.line_buffer = ""
        self.pending_leave_node_id: str | None = None
        self.pending_gateway_eui64: str | None = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        data = read_json_file(DEVICES_FILE, {"devices": []})
        devices = data.get("devices", []) if isinstance(data, dict) else []
        with self.lock:
            self.devices = {}
            for device in devices:
                if not isinstance(device, dict):
                    continue
                key = self._key_for(device.get("eui64"), device.get("nodeId"))
                if key:
                    self.devices[key] = self._normalize_device(device)

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            payload = {"devices": self.list_devices()}
        tmp_path = DEVICES_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(DEVICES_FILE)

    def list_devices(self) -> list[dict[str, Any]]:
        with self.lock:
            devices = [dict(device) for device in self.devices.values()]
        return sorted(
            devices,
            key=lambda item: (
                item.get("role") != "gateway",
                item.get("eui64") is None,
                item.get("lastSeen") or "",
                item.get("nodeId") or "",
            ),
            reverse=True,
        )

    def rebuild_from_logs(self) -> list[dict[str, Any]]:
        with self.lock:
            self.devices = {}
            self.line_buffer = ""
            self.pending_leave_node_id = None
            self.pending_gateway_eui64 = None
        for path in sorted(LOG_DIR.glob("gateway-*.log")):
            try:
                ts = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                self.parse_text(path.read_text(encoding="utf-8", errors="replace") + "\n", ts)
            except OSError:
                continue
        self.save()
        return self.list_devices()

    def parse_text(self, text: str, ts: str | None = None) -> set[str]:
        normalized = clean_log_text(text)
        with self.lock:
            combined = self.line_buffer + normalized
            lines = combined.split("\n")
            self.line_buffer = lines.pop() if not combined.endswith("\n") else ""
        changed = False
        joined_node_ids: set[str] = set()
        for line in lines:
            line_changed, line_joined_node_ids = self._parse_line(line, ts or now_iso())
            changed |= line_changed
            joined_node_ids.update(line_joined_node_ids)
        if changed:
            self.save()
        return joined_node_ids

    def _parse_line(self, line: str, ts: str) -> tuple[bool, set[str]]:
        changed = False
        joined_node_ids: set[str] = set()
        leave_command = self.LEAVE_COMMAND_RE.match(line)
        if leave_command:
            with self.lock:
                self.pending_leave_node_id = self._normalize_node_id(leave_command.group(1))
            return False, joined_node_ids

        if self.LEAVE_RESPONSE_RE.search(line):
            with self.lock:
                pending_leave_node_id = self.pending_leave_node_id
                self.pending_leave_node_id = None
            if pending_leave_node_id:
                changed |= self.remove_by_node_id(pending_leave_node_id)

        for match in self.TC_HANDLER_RE.finditer(line):
            status = match.group(1).strip().lower()
            node_id = match.group(2)
            if "left" in status:
                changed |= self.remove_by_node_id(node_id)
            elif "join" in status:
                changed |= self.upsert(node_id=node_id, source="trust-center-join", last_seen=ts)
                normalized_node_id = self._normalize_node_id(node_id)
                if normalized_node_id:
                    joined_node_ids.add(normalized_node_id)
        gateway_node = self.INFO_NODE_RE.search(line)
        if gateway_node:
            gateway_eui64 = self._normalize_eui64(gateway_node.group(1))
            with self.lock:
                self.pending_gateway_eui64 = gateway_eui64
            changed |= self.upsert(
                eui64=gateway_eui64,
                source="gateway-info",
                last_seen=ts,
                role="gateway",
                name="网关",
            )

        gateway_node_id = self.INFO_NODE_ID_RE.search(line)
        if gateway_node_id:
            with self.lock:
                gateway_eui64 = self.pending_gateway_eui64
            changed |= self.upsert(
                node_id=gateway_node_id.group(1),
                eui64=gateway_eui64,
                source="gateway-info",
                last_seen=ts,
                role="gateway",
                name="网关",
            )

        for match in self.ANNOUNCE_RE.finditer(line):
            changed |= self.upsert(node_id=match.group(1), source="device-announce", last_seen=ts)
        match = self.NEIGHBOR_RE.match(line)
        if match:
            changed |= self.upsert(
                node_id=match.group(1),
                eui64=match.group(3),
                lqi=int(match.group(2)),
                source="neighbor-table",
                last_seen=ts,
            )
        return changed, joined_node_ids

    def remove_by_node_id(self, node_id: str | None) -> bool:
        node_id = self._normalize_node_id(node_id)
        if not node_id:
            return False
        with self.lock:
            keys = [
                key
                for key, device in self.devices.items()
                if self._normalize_node_id(device.get("nodeId")) == node_id
            ]
            if not keys:
                return False
            for key in keys:
                self.devices.pop(key, None)
            return True

    def upsert(
        self,
        *,
        node_id: str | None = None,
        eui64: str | None = None,
        lqi: int | None = None,
        source: str,
        last_seen: str,
        role: str | None = None,
        name: str | None = None,
    ) -> bool:
        node_id = self._normalize_node_id(node_id)
        eui64 = self._normalize_eui64(eui64)
        with self.lock:
            key = self._key_for(eui64, node_id)
            if not key:
                return False
            device = self.devices.get(key)
            if node_id:
                matching_keys = [
                    existing_key
                    for existing_key, existing_device in self.devices.items()
                    if existing_key != key and self._normalize_node_id(existing_device.get("nodeId")) == node_id
                ]
                for existing_key in matching_keys:
                    device = self._merge_devices(self.devices.pop(existing_key), device)
            if device is None:
                device = {
                    "nodeId": node_id,
                    "eui64": eui64,
                    "lqi": lqi,
                    "firstSeen": last_seen,
                    "lastSeen": last_seen,
                    "sources": [],
                }
            before = json.dumps(device, sort_keys=True, ensure_ascii=False)
            if node_id:
                device["nodeId"] = node_id
            if eui64:
                device["eui64"] = eui64
            if lqi is not None:
                device["lqi"] = lqi
            if role:
                device["role"] = role
            if name:
                device["name"] = name
            device["lastSeen"] = last_seen
            if not device.get("firstSeen"):
                device["firstSeen"] = last_seen
            sources = set(device.get("sources") or [])
            sources.add(source)
            device["sources"] = sorted(sources)
            new_key = self._key_for(device.get("eui64"), device.get("nodeId"))
            if not new_key:
                return False
            self.devices[new_key] = self._normalize_device(device)
            return before != json.dumps(self.devices[new_key], sort_keys=True, ensure_ascii=False)

    def _merge_devices(
        self,
        left: dict[str, Any] | None,
        right: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for source in (left or {}, right or {}):
            for key, value in source.items():
                if value not in (None, "", []):
                    merged[key] = value
        sources = set((left or {}).get("sources") or []) | set((right or {}).get("sources") or [])
        merged["sources"] = sorted(sources)
        first_seen = min(
            [value for value in [(left or {}).get("firstSeen"), (right or {}).get("firstSeen")] if value],
            default=merged.get("lastSeen"),
        )
        if first_seen:
            merged["firstSeen"] = first_seen
        return merged

    def _normalize_device(self, device: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(device)
        normalized["nodeId"] = self._normalize_node_id(normalized.get("nodeId"))
        normalized["eui64"] = self._normalize_eui64(normalized.get("eui64"))
        normalized["sources"] = sorted(set(normalized.get("sources") or []))
        if normalized.get("role") not in {"gateway"}:
            normalized.pop("role", None)
        if not normalized.get("name"):
            normalized.pop("name", None)
        return normalized

    def _key_for(self, eui64: str | None, node_id: str | None) -> str | None:
        eui64 = self._normalize_eui64(eui64)
        node_id = self._normalize_node_id(node_id)
        if eui64:
            return f"eui64:{eui64}"
        if node_id:
            return f"node:{node_id}"
        return None

    def _normalize_node_id(self, value: str | None) -> str | None:
        return normalize_node_id(value)

    def _normalize_eui64(self, value: str | None) -> str | None:
        if not value:
            return None
        cleaned = re.sub(r"[^0-9A-Fa-f]", "", str(value))
        if len(cleaned) != 16:
            return None
        return cleaned.upper()


class GatewayManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.command_lock = threading.RLock()
        self.prompt_condition = threading.Condition()
        self.prompt_seq = 0
        self.output_tail = ""
        self.proc: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None
        self.reader_thread: threading.Thread | None = None
        self.session_id: str | None = None
        self.session_log: Path | None = None
        self.last_exit_code: int | None = None
        self.executable: str = str(DEFAULT_EXECUTABLE)
        self.cwd: str = str(infer_workdir(DEFAULT_EXECUTABLE))
        self.network_index: str = DEFAULT_NETWORK_INDEX
        self.configured_serial_port: str = CONFIGURED_SERIAL_PORT
        self.serial_port: str = resolve_runtime_serial_port(DEFAULT_SERIAL_PORT)
        self.baud_rate: str = DEFAULT_BAUD_RATE
        self.history: deque[dict[str, Any]] = deque(maxlen=2000)
        self.subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self.gateway_info_timer: threading.Timer | None = None
        self.neighbor_table_timer: threading.Timer | None = None
        self.last_neighbor_table_at = 0.0
        self.pending_neighbor_table_reason: str | None = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def running(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict[str, Any]:
        with self.lock:
            pid = self.proc.pid if self.proc and self.proc.poll() is None else None
            return {
                "running": pid is not None,
                "pid": pid,
                "session_id": self.session_id,
                "last_exit_code": self.last_exit_code,
                "executable": self.executable,
                "cwd": self.cwd,
                "network_index": self.network_index,
                "serial_port": self.serial_port,
                "configured_serial_port": self.configured_serial_port,
                "baud_rate": self.baud_rate,
                "gateway_root": str(GATEWAY_ROOT),
                "allowed_root": str(ALLOWED_ROOT),
                "default_executable": str(DEFAULT_EXECUTABLE),
            }

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.running():
                raise ValueError("gateway is already running")

            executable = str(payload.get("executable") or DEFAULT_EXECUTABLE).strip()
            network_index = str(payload.get("network_index") or self.network_index or DEFAULT_NETWORK_INDEX).strip()
            serial_port = str(
                payload.get("serial_port")
                or self.serial_port
                or resolve_runtime_serial_port(DEFAULT_SERIAL_PORT)
                or DEFAULT_SERIAL_PORT
            ).strip()
            baud_rate = str(payload.get("baud_rate") or self.baud_rate or DEFAULT_BAUD_RATE).strip()

            exe_path = Path(executable).expanduser().resolve()
            if not is_within(exe_path, ALLOWED_ROOT):
                raise ValueError(f"executable must be under {ALLOWED_ROOT}")
            if not exe_path.is_file():
                raise ValueError("selected executable does not exist")
            if not os.access(exe_path, os.X_OK):
                raise ValueError("selected file is not executable")
            if not serial_port.startswith("/dev/"):
                raise ValueError("serial port must start with /dev/")
            if not network_index.isdigit():
                raise ValueError("network index must be a number")
            if not baud_rate.isdigit():
                raise ValueError("baud rate must be a number")

            master_fd, slave_fd = pty.openpty()
            args = [str(exe_path), "-n", network_index, "-p", serial_port, "-b", baud_rate]
            cwd = str(infer_workdir(exe_path))
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            self.session_log = LOG_DIR / f"gateway-{session_id}.log"
            self.history.clear()
            with self.prompt_condition:
                self.prompt_seq = 0
                self.output_tail = ""
            self.last_exit_code = None
            if self.gateway_info_timer is not None:
                self.gateway_info_timer.cancel()
                self.gateway_info_timer = None
            if self.neighbor_table_timer is not None:
                self.neighbor_table_timer.cancel()
                self.neighbor_table_timer = None
            self.last_neighbor_table_at = 0.0
            self.pending_neighbor_table_reason = None
            self.session_id = session_id
            self.executable = str(exe_path)
            self.cwd = cwd
            self.network_index = network_index
            self.serial_port = serial_port
            self.baud_rate = baud_rate

            try:
                self.proc = subprocess.Popen(
                    args,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=cwd,
                    close_fds=True,
                    start_new_session=True,
                )
            finally:
                os.close(slave_fd)

            self.master_fd = master_fd
            self._write_operation("start", {"args": args, "cwd": cwd, "session_id": session_id})
            self._emit(f"$ {' '.join(args)}\n", "system")

            self.reader_thread = threading.Thread(target=self._read_loop, name="gateway-pty-reader", daemon=True)
            self.reader_thread.start()
            self.schedule_gateway_info_refresh("gateway-start")
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            proc = self.proc
            master_fd = self.master_fd
            if proc is None or proc.poll() is not None:
                return self.status()
            self._write_operation("stop", {"pid": proc.pid, "session_id": self.session_id})
            self._emit("\n[stop requested]\n", "system")

        if master_fd is not None:
            try:
                os.write(master_fd, b"\x03")
            except OSError:
                pass

        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

        with self.lock:
            self.last_exit_code = proc.returncode
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = None
            if self.gateway_info_timer is not None:
                self.gateway_info_timer.cancel()
                self.gateway_info_timer = None
            if self.neighbor_table_timer is not None:
                self.neighbor_table_timer.cancel()
                self.neighbor_table_timer = None
            self.pending_neighbor_table_reason = None
            return self.status()

    def send_command(self, command: str) -> dict[str, Any]:
        command = command.rstrip("\r\n")
        if not command:
            raise ValueError("command is empty")
        lines = [line.strip() for line in command.splitlines() if line.strip()]
        if not lines:
            raise ValueError("command is empty")
        if len(lines) > 1:
            return self.send_command_sequence(lines)
        with self.command_lock:
            self._send_single_command(lines[0], "send", {})
        return {"ok": True}

    def send_command_sequence(
        self,
        commands: list[str],
        *,
        inter_command_delay: float = GATEWAY_COMMAND_SEQUENCE_DELAY_SECONDS,
        prompt_timeout: float = GATEWAY_COMMAND_PROMPT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        lines = [line.strip() for line in commands if line and line.strip()]
        if not lines:
            raise ValueError("command is empty")
        with self.command_lock:
            total = len(lines)
            for index, line in enumerate(lines, start=1):
                baseline_prompt_seq = self._prompt_seq()
                self._send_single_command(
                    line,
                    "send",
                    {"sequence_index": index, "sequence_total": total} if total > 1 else {},
                )
                if index < total:
                    self._wait_for_prompt_after(baseline_prompt_seq, prompt_timeout)
                    if inter_command_delay > 0:
                        time.sleep(inter_command_delay)
        return {"ok": True}

    def _send_single_command(self, command: str, action: str, extra: dict[str, Any]) -> None:
        with self.lock:
            if not self.running() or self.master_fd is None:
                raise ValueError("gateway is not running")
            self._send_command_locked(command, action, extra)

    def _prompt_seq(self) -> int:
        with self.prompt_condition:
            return self.prompt_seq

    def _wait_for_prompt_after(self, baseline_prompt_seq: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.prompt_condition:
            while self.prompt_seq <= baseline_prompt_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.prompt_condition.wait(timeout=min(0.1, remaining))
        return True

    def schedule_gateway_info_refresh(self, reason: str) -> None:
        with self.lock:
            if not self.running() or self.master_fd is None:
                return
            if self.gateway_info_timer is not None and self.gateway_info_timer.is_alive():
                return
            self.gateway_info_timer = threading.Timer(INFO_START_DELAY_SECONDS, self._send_auto_gateway_info, args=(reason,))
            self.gateway_info_timer.daemon = True
            self.gateway_info_timer.start()

    def _send_auto_gateway_info(self, reason: str) -> None:
        with self.command_lock:
            with self.lock:
                self.gateway_info_timer = None
                if not self.running() or self.master_fd is None:
                    return
                self._send_command_locked(INFO_COMMAND, "auto-send", {"reason": reason})

    def schedule_neighbor_table_refresh(self, reason: str) -> None:
        with self.lock:
            if not self.running() or self.master_fd is None:
                return
            self.pending_neighbor_table_reason = reason
            if self.neighbor_table_timer is not None and self.neighbor_table_timer.is_alive():
                return
            elapsed = time.monotonic() - self.last_neighbor_table_at
            cooldown_remaining = max(0.0, NEIGHBOR_TABLE_COOLDOWN_SECONDS - elapsed)
            delay = max(NEIGHBOR_TABLE_JOIN_DELAY_SECONDS, cooldown_remaining)
            self.neighbor_table_timer = threading.Timer(delay, self._send_auto_neighbor_table)
            self.neighbor_table_timer.daemon = True
            self.neighbor_table_timer.start()

    def _send_auto_neighbor_table(self) -> None:
        with self.command_lock:
            with self.lock:
                self.neighbor_table_timer = None
                reason = self.pending_neighbor_table_reason or "device-join"
                self.pending_neighbor_table_reason = None
                if not self.running() or self.master_fd is None:
                    return
                self._send_command_locked(
                    NEIGHBOR_TABLE_COMMAND,
                    "auto-send",
                    {"reason": reason},
                )
                self.last_neighbor_table_at = time.monotonic()

    def _send_command_locked(self, command: str, action: str, extra: dict[str, Any]) -> None:
        if self.master_fd is None:
            raise ValueError("gateway is not running")
        os.write(self.master_fd, (command + "\n").encode("utf-8"))
        self._write_operation(action, {"command": command, "session_id": self.session_id, **extra})
        self._emit(f"> {command}\n", "input")

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.history)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            self.subscribers.discard(q)

    def _read_loop(self) -> None:
        while True:
            with self.lock:
                fd = self.master_fd
                proc = self.proc
            if fd is None or proc is None:
                break
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            self._emit(data.decode("utf-8", errors="replace"), "output")

        with self.lock:
            proc = self.proc
            exit_code = proc.poll() if proc is not None else self.last_exit_code
            if proc is not None and exit_code is None:
                try:
                    exit_code = proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    exit_code = None
            self.last_exit_code = exit_code
            self._emit(f"\n[process exited with code {exit_code}]\n", "system")

    def _emit(self, text: str, kind: str) -> None:
        event = {"ts": now_iso(), "kind": kind, "text": text}
        if kind == "output":
            self._track_gateway_prompt(text)
        with self.lock:
            self.history.append(event)
            if self.session_log is not None:
                try:
                    with self.session_log.open("a", encoding="utf-8") as fh:
                        fh.write(text)
                except OSError:
                    pass
            subscribers = list(self.subscribers)
        joined_node_ids: set[str] = set()
        try:
            joined_node_ids = zigbee_registry.parse_text(text, event["ts"])
        except Exception:
            traceback.print_exc()
        if kind == "output" and joined_node_ids:
            reason = "device-join:" + ",".join(sorted(joined_node_ids))
            self.schedule_neighbor_table_refresh(reason)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _track_gateway_prompt(self, text: str) -> None:
        combined = self.output_tail + text
        prompt_count = combined.count(GATEWAY_PROMPT)
        self.output_tail = combined[-max(1, len(GATEWAY_PROMPT) - 1):]
        if prompt_count:
            with self.prompt_condition:
                self.prompt_seq += prompt_count
                self.prompt_condition.notify_all()

    def _write_operation(self, action: str, payload: dict[str, Any]) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {"ts": now_iso(), "action": action, **payload}
        try:
            with (LOG_DIR / "operations.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass



manager = GatewayManager()
zigbee_registry = ZigbeeDeviceRegistry()


class ZeroCrossCalibrator:
    TYPE_NAMES = {0x01: "on", 0x02: "off"}
    COMMAND_BYTES = {"on": "FA", "off": "FB"}
    ACTION_LABELS = {"on": "开灯", "off": "关灯"}

    def __init__(self, gateway: GatewayManager) -> None:
        self.gateway = gateway
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.reader_thread: threading.Thread | None = None
        self.serial_fd: int | None = None
        self.serial_buffer = bytearray()
        self.frame_seq = 0
        self.latest_measurements: dict[str, dict[str, Any] | None] = {"on": None, "off": None}
        self.state: dict[str, Any] = self._idle_state()

    def _idle_state(self) -> dict[str, Any]:
        serial_port = resolve_runtime_serial_port(CALIBRATION_SERIAL_PORT)
        return {
            "active": False,
            "serial_port": serial_port,
            "configured_serial_port": CALIBRATION_SERIAL_PORT,
            "serial_baud_rate": CALIBRATION_BAUD_RATE,
            "serial_open": False,
            "target_node": None,
            "endpoint": "1",
            "round": 0,
            "max_rounds": ZERO_CROSS_MAX_ROUNDS,
            "half_cycle_us": ZERO_CROSS_HALF_CYCLE_US,
            "success_window_us": ZERO_CROSS_SUCCESS_WINDOW_US,
            "last_on_measurement_us": None,
            "last_off_measurement_us": None,
            "last_on_status": None,
            "last_off_status": None,
            "last_on_calibration_us": None,
            "last_off_calibration_us": None,
            "consecutive_timeouts": 0,
            "result": "idle",
            "last_error": None,
            "started_at": None,
            "stopped_at": None,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            status = dict(self.state)
            status["serial_open"] = self.serial_fd is not None
            return status

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        node_id = normalize_node_id(str(payload.get("nodeId") or payload.get("node_id") or ""))
        endpoint = str(payload.get("endpoint") or "1").strip() or "1"
        if not node_id:
            raise ValueError("nodeId is required")
        if not endpoint.isdigit():
            raise ValueError("endpoint must be a number")
        if not self.gateway.running():
            raise ValueError("gateway is not running")
        serial_port = resolve_runtime_serial_port(
            str(payload.get("serial_port") or CALIBRATION_SERIAL_PORT or "").strip()
        )
        if not serial_port:
            raise ValueError("calibration serial port is not configured")
        if not serial_port.startswith("/dev/"):
            raise ValueError("calibration serial port must start with /dev/")

        with self.lock:
            if self.state.get("active"):
                raise ValueError("zero-cross calibration is already running")
            self.stop_event.clear()
            self.serial_buffer = bytearray()
            self.latest_measurements = {"on": None, "off": None}
            self.frame_seq = 0
            self.state = self._idle_state()
            self.state.update(
                {
                    "active": True,
                    "serial_port": serial_port,
                    "target_node": node_id,
                    "endpoint": endpoint,
                    "result": "running",
                    "started_at": now_iso(),
                    "stopped_at": None,
                }
            )

        try:
            self._open_serial(serial_port)
        except Exception as exc:
            self._set_error(f"failed to open calibration serial {serial_port}: {exc}")
            self._finish("error")
            raise
        self.reader_thread = threading.Thread(target=self._serial_read_loop, name="zero-cross-serial-reader", daemon=True)
        self.reader_thread.start()
        self.worker_thread = threading.Thread(target=self._worker_loop, name="zero-cross-calibrator", daemon=True)
        self.worker_thread.start()
        self._emit(f"[zerocross] 自动过零校准已启动: node={node_id} endpoint={endpoint} serial={serial_port}\n")
        return self.status()

    def stop(self, reason: str = "stopped") -> dict[str, Any]:
        with self.lock:
            was_active = bool(self.state.get("active"))
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self._close_serial()
        if was_active:
            self._finish(reason)
            self._emit(f"[zerocross] 自动过零校准已停止: {reason}\n")
        return self.status()

    def _open_serial(self, serial_port: str) -> None:
        fd = os.open(serial_port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0
            attrs[4] = termios.B9600
            attrs[5] = termios.B9600
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 1
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            os.close(fd)
            raise
        with self.lock:
            self.serial_fd = fd
            self.state["serial_open"] = True

    def _close_serial(self) -> None:
        with self.lock:
            fd = self.serial_fd
            self.serial_fd = None
            self.state["serial_open"] = False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _serial_read_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    fd = self.serial_fd
                if fd is None:
                    return
                try:
                    readable, _, _ = select.select([fd], [], [], 0.2)
                    if not readable:
                        continue
                    data = os.read(fd, 64)
                except OSError as exc:
                    if not self.stop_event.is_set():
                        self._set_error(f"calibration serial read failed: {exc}")
                    return
                if data:
                    self._parse_serial_bytes(data)
        finally:
            self._close_serial()

    def _parse_serial_bytes(self, data: bytes) -> None:
        invalid_frames: list[bytes] = []
        records: list[tuple[str, int, bytes]] = []
        with self.lock:
            self.serial_buffer.extend(data)
            while len(self.serial_buffer) >= 6:
                header_at = self.serial_buffer.find(b"\x55\xAA")
                if header_at < 0:
                    del self.serial_buffer[:-1]
                    break
                if header_at > 0:
                    del self.serial_buffer[:header_at]
                if len(self.serial_buffer) < 6:
                    break
                frame = bytes(self.serial_buffer[:6])
                del self.serial_buffer[:6]
                parsed = self.parse_frame(frame)
                if parsed is None:
                    invalid_frames.append(frame)
                    continue
                records.append((parsed["kind"], parsed["value_us"], frame))
        for frame in invalid_frames:
            self._emit(f"[zerocross] 丢弃无效仪器帧: {frame.hex(' ').upper()}\n")
        for kind, value_us, frame in records:
            self._record_measurement(kind, value_us, frame)

    @classmethod
    def parse_frame(cls, frame: bytes) -> dict[str, Any] | None:
        if len(frame) != 6 or frame[0] != 0x55 or frame[1] != 0xAA:
            return None
        frame_type = frame[2]
        kind = cls.TYPE_NAMES.get(frame_type)
        if not kind:
            return None
        expected_checksum = (frame[3] + frame[4] + frame_type - 1) & 0xFF
        if frame[5] != expected_checksum:
            return None
        return {
            "kind": kind,
            "frame_type": frame_type,
            "value_us": (frame[3] << 8) | frame[4],
        }

    def _record_measurement(self, kind: str, value_us: int, frame: bytes) -> None:
        with self.condition:
            self.frame_seq += 1
            record = {
                "seq": self.frame_seq,
                "kind": kind,
                "value_us": value_us,
                "frame": frame.hex(" ").upper(),
                "ts": now_iso(),
            }
            self.latest_measurements[kind] = record
            self.state[f"last_{kind}_measurement_us"] = value_us
            self.condition.notify_all()
        self._emit(f"[zerocross] 仪器测量: {self.ACTION_LABELS[kind]} {value_us}us ({record['frame']})\n")

    def _worker_loop(self) -> None:
        final_result = "stopped"
        try:
            with self.lock:
                node_id = self.state["target_node"]
                endpoint = self.state["endpoint"]
                max_rounds = int(self.state["max_rounds"])
            for round_no in range(1, max_rounds + 1):
                if self.stop_event.is_set():
                    final_result = "stopped"
                    break
                with self.lock:
                    self.state["round"] = round_no
                    self.state["last_on_status"] = None
                    self.state["last_off_status"] = None
                self._emit(f"[zerocross] 第 {round_no} 轮开始\n")
                on_ok = self._run_edge("on", node_id, endpoint)
                if self.stop_event.is_set():
                    final_result = "stopped"
                    break
                if not self._wait_or_stop(ZERO_CROSS_SWITCH_INTERVAL_SECONDS):
                    final_result = "stopped"
                    break
                off_ok = self._run_edge("off", node_id, endpoint)
                if self.stop_event.is_set():
                    final_result = "stopped"
                    break
                if on_ok and off_ok:
                    final_result = "success"
                    self._emit(f"[zerocross] 第 {round_no} 轮开/关过零均已达标，自动校准完成\n")
                    break
                with self.lock:
                    if self.state["consecutive_timeouts"] >= ZERO_CROSS_MAX_CONSECUTIVE_TIMEOUTS:
                        final_result = "timeout"
                        self._emit("[zerocross] 连续测量超时过多，自动校准停止\n")
                        break
                if not self._wait_or_stop(ZERO_CROSS_CALIBRATION_SETTLE_SECONDS):
                    final_result = "stopped"
                    break
            else:
                final_result = "max-rounds"
                self._emit(f"[zerocross] 已达到最大轮数 {max_rounds}，自动校准停止\n")
        except Exception as exc:
            final_result = "error"
            self._set_error(str(exc))
            traceback.print_exc()
        finally:
            self.stop_event.set()
            self._close_serial()
            self._finish(final_result)

    def _run_edge(self, kind: str, node_id: str, endpoint: str) -> bool:
        action = "on" if kind == "on" else "off"
        label = self.ACTION_LABELS[kind]
        with self.lock:
            baseline_seq = self.frame_seq
        self._emit(f"[zerocross] {label}: 发送开关命令\n")
        self.gateway.send_command_sequence([
            f"zcl on-off {action}",
            f"send {node_id} {endpoint} {endpoint}",
        ])
        measurement = self._wait_for_measurement(kind, baseline_seq)
        if measurement is None:
            with self.lock:
                self.state[f"last_{kind}_status"] = "timeout"
                self.state["consecutive_timeouts"] += 1
            self._emit(f"[zerocross] {label}: 等待仪器测量超时\n")
            return False

        value_us = int(measurement["value_us"])
        with self.lock:
            self.state["consecutive_timeouts"] = 0
        if value_us <= ZERO_CROSS_SUCCESS_WINDOW_US:
            with self.lock:
                self.state[f"last_{kind}_status"] = "success"
            self._emit(f"[zerocross] {label}: {value_us}us <= {ZERO_CROSS_SUCCESS_WINDOW_US}us，达标\n")
            return True

        calibration_us = max(0, min(0xFFFF, ZERO_CROSS_HALF_CYCLE_US - value_us))
        with self.lock:
            self.state[f"last_{kind}_status"] = "adjusted"
            self.state[f"last_{kind}_calibration_us"] = calibration_us
        bytes_text = uint16_hex_bytes(calibration_us)
        command_byte = self.COMMAND_BYTES[kind]
        self._emit(
            f"[zerocross] {label}: {value_us}us 未达标，下发补偿 {calibration_us}us ({bytes_text})\n"
        )
        self.gateway.send_command_sequence([
            f"raw 0xEEEE {{11 01 {command_byte} {bytes_text}}}",
            f"send {node_id} {endpoint} {endpoint}",
        ])
        return False

    def _wait_for_measurement(self, kind: str, baseline_seq: int) -> dict[str, Any] | None:
        deadline = time.monotonic() + ZERO_CROSS_MEASUREMENT_TIMEOUT_SECONDS
        with self.condition:
            while not self.stop_event.is_set():
                record = self.latest_measurements.get(kind)
                if record and int(record.get("seq", 0)) > baseline_seq:
                    return dict(record)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(timeout=min(0.2, remaining))
        return None

    def _wait_or_stop(self, seconds: float) -> bool:
        return not self.stop_event.wait(seconds)

    def _finish(self, result: str) -> None:
        with self.lock:
            if not self.state.get("active") and self.state.get("result") != "running":
                return
            self.state["active"] = False
            self.state["result"] = result
            self.state["stopped_at"] = now_iso()
            self.state["serial_open"] = self.serial_fd is not None

    def _set_error(self, message: str) -> None:
        with self.lock:
            self.state["last_error"] = message
        self._emit(f"[zerocross] ERROR: {message}\n")

    def _emit(self, text: str) -> None:
        self.gateway._emit(text, "system")


zero_cross_calibrator = ZeroCrossCalibrator(manager)


def list_serial_devices() -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    seen_realpaths: set[str] = set()
    aliases: dict[str, list[str]] = {}
    for by_id in sorted(glob.glob("/dev/serial/by-id/*")):
        aliases.setdefault(os.path.realpath(by_id), []).append(by_id)

    # Z3Gateway rejects very long -p values, so prefer short kernel device names.
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        for item in sorted(glob.glob(pattern)):
            real = os.path.realpath(item)
            if real in seen_realpaths:
                continue
            seen_realpaths.add(real)
            alias_text = aliases.get(real, [])
            label = item
            if alias_text:
                label = f"{item} ({alias_text[0]})"
            devices.append({"path": item, "realpath": real, "label": label})

    for by_id in sorted(glob.glob("/dev/serial/by-id/*")):
        real = os.path.realpath(by_id)
        if real in seen_realpaths:
            continue
        seen_realpaths.add(real)
        devices.append({"path": real, "realpath": real, "label": f"{real} ({by_id})"})
    return devices


def browse_directory(raw_path: str | None) -> dict[str, Any]:
    path = Path(unquote(raw_path or str(ALLOWED_ROOT))).expanduser()
    if not path.is_absolute():
        path = ALLOWED_ROOT / path
    resolved = path.resolve()
    if not is_within(resolved, ALLOWED_ROOT):
        raise ValueError(f"path must be under {ALLOWED_ROOT}")
    if not resolved.exists():
        raise ValueError("path does not exist")
    if resolved.is_file():
        resolved = resolved.parent
    if not resolved.is_dir():
        raise ValueError("path is not a directory")

    entries: list[dict[str, Any]] = []
    for entry in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            entry_resolved = entry.resolve()
            inside = is_within(entry_resolved, ALLOWED_ROOT)
            stat = entry.stat()
            entries.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "is_file": entry.is_file(),
                    "is_executable": entry.is_file() and os.access(entry, os.X_OK) and inside,
                    "blocked": not inside,
                    "size": stat.st_size,
                }
            )
        except OSError:
            continue

    parent = None
    if resolved != ALLOWED_ROOT:
        parent_path = resolved.parent.resolve()
        if is_within(parent_path, ALLOWED_ROOT):
            parent = str(parent_path)
    return {"root": str(ALLOWED_ROOT), "path": str(resolved), "parent": parent, "entries": entries}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "Z3GatewayControlPanel/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api_get(parsed.path, parse_qs(parsed.query))
            elif parsed.path == "/logs/stream" or parsed.path == "/api/logs/stream":
                self.handle_log_stream()
            else:
                self.handle_static(parsed.path)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            payload = self.read_json_body()
            self.handle_api_post(parsed.path, payload)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/status":
            self.send_json(manager.status())
        elif path == "/api/zerocross/status":
            self.send_json(zero_cross_calibrator.status())
        elif path == "/api/devices":
            self.send_json({"devices": list_serial_devices()})
        elif path == "/api/zigbee/devices":
            self.send_json({"devices": zigbee_registry.list_devices()})
        elif path == "/api/commands":
            self.send_json(read_json_file(COMMANDS_FILE, {"groups": []}))
        elif path == "/api/browse":
            raw_path = query.get("path", [str(ALLOWED_ROOT)])[0]
            self.send_json(browse_directory(raw_path))
        elif path == "/api/logs/current":
            self.send_json({"events": manager.snapshot()})
        elif path == "/api/logs/stream":
            self.handle_log_stream()
        else:
            self.send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def handle_api_post(self, path: str, payload: dict[str, Any]) -> None:
        if path == "/api/start":
            self.send_json(manager.start(payload))
        elif path == "/api/stop":
            zero_cross_calibrator.stop("gateway-stop")
            self.send_json(manager.stop())
        elif path == "/api/zerocross/start":
            self.send_json(zero_cross_calibrator.start(payload))
        elif path == "/api/zerocross/stop":
            self.send_json(zero_cross_calibrator.stop("user-stop"))
        elif path == "/api/send":
            self.send_json(manager.send_command(str(payload.get("command") or "")))
        elif path == "/api/zigbee/devices/reparse":
            self.send_json({"devices": zigbee_registry.rebuild_from_logs()})
        else:
            self.send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def handle_static(self, path: str) -> None:
        if path in ("", "/"):
            target = STATIC_DIR / "index.html"
        else:
            rel = Path(path.lstrip("/"))
            target = (STATIC_DIR / rel).resolve()
            if not is_within(target, STATIC_DIR.resolve()):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_log_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_event(event: dict[str, Any]) -> None:
            data = json.dumps(event, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            for event in manager.snapshot():
                send_event(event)
            q = manager.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=15)
                        send_event(event)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            finally:
                manager.unsubscribe(q)
        except (BrokenPipeError, ConnectionResetError):
            return

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    zigbee_registry.rebuild_from_logs()
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), RequestHandler)
    print(f"Z3Gateway control panel: http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    print(f"Gateway root: {GATEWAY_ROOT}")
    print(f"Allowed browser root: {ALLOWED_ROOT}")
    print(f"Data dir: {DATA_DIR}")
    print(f"Default executable: {DEFAULT_EXECUTABLE}")
    print(f"Configured serial: {CONFIGURED_SERIAL_PORT or '-'}")
    print(f"Default serial: {DEFAULT_SERIAL_PORT or '-'}")
    print(f"Calibration serial: {CALIBRATION_SERIAL_PORT or '-'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        zero_cross_calibrator.stop("server-stop")
        manager.stop()
        server.server_close()


if __name__ == "__main__":
    main()
