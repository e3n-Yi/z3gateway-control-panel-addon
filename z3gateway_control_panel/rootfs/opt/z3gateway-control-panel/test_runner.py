"""Semi-automatic Zigbee device test runner for the Z3Gateway control panel.

Scripts (JSON, format "z3-test-script/1") describe attribute writes, start
states, commands, operator prompts and report expectations. The runner sends
frames through the gateway's serialized CLI path, observes the @Z3 events that
DeviceCenter.feed() decodes, and evaluates tolerant matchers. Nothing here
imports server.py: the gateway, device center and publisher are injected so
the runner can be exercised against a simulated gateway.

See docs/Z3GATEWAY_TEST_RUNNER_DESIGN.md for the format and semantics.
"""

from __future__ import annotations

import copy
import html
import json
import re
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from zcl_frames import ZclDecodeError, decode_global, parse_zcl_frame

SCRIPT_FORMAT = "z3-test-script/1"
RESULT_FORMAT = "z3-test-result/1"
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RESULT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,140}$")
SCRIPT_MAX_BYTES = 1024 * 1024
STEP_OBSERVATION_LIMIT = 500
RUN_EVENT_LIMIT = 20000

CLUSTER_ON_OFF = 0x0006
CLUSTER_LEVEL = 0x0008
ZDO_BIND_REQUEST = 0x0021
ZDO_BIND_RESPONSE = 0x8021

# name -> (cluster, attribute id, ZCL type)
ATTRIBUTES: dict[str, tuple[int, int, int]] = {
    "onOff": (CLUSTER_ON_OFF, 0x0000, 0x10),
    "globalSceneControl": (CLUSTER_ON_OFF, 0x4000, 0x10),
    "onTime": (CLUSTER_ON_OFF, 0x4001, 0x21),
    "offWaitTime": (CLUSTER_ON_OFF, 0x4002, 0x21),
    "startUpOnOff": (CLUSTER_ON_OFF, 0x4003, 0x30),
    "currentLevel": (CLUSTER_LEVEL, 0x0000, 0x20),
    "remainingTime": (CLUSTER_LEVEL, 0x0001, 0x21),
    "minLevel": (CLUSTER_LEVEL, 0x0002, 0x20),
    "maxLevel": (CLUSTER_LEVEL, 0x0003, 0x20),
    "options": (CLUSTER_LEVEL, 0x000F, 0x18),
    "onOffTransitionTime": (CLUSTER_LEVEL, 0x0010, 0x21),
    "onLevel": (CLUSTER_LEVEL, 0x0011, 0x20),
    "onTransitionTime": (CLUSTER_LEVEL, 0x0012, 0x21),
    "offTransitionTime": (CLUSTER_LEVEL, 0x0013, 0x21),
    "defaultMoveRate": (CLUSTER_LEVEL, 0x0014, 0x20),
    "startUpCurrentLevel": (CLUSTER_LEVEL, 0x4000, 0x20),
}
ATTRIBUTE_BY_ID = {(cluster, attr): name for name, (cluster, attr, _) in ATTRIBUTES.items()}
TYPE_SIZES = {0x10: 1, 0x18: 1, 0x19: 2, 0x20: 1, 0x21: 2, 0x22: 3, 0x23: 4, 0x30: 1, 0x31: 2}
DEFAULT_REPORTING = [
    {"attribute": "onOff", "min": 0, "max": 3600},
    {"attribute": "currentLevel", "min": 1, "max": 3600, "change": 1},
]

STEP_TYPES = {"write_attributes", "read", "set_state", "command", "burst", "expect", "wait", "operator", "confirm"}
MATCHER_KINDS = {"first_value", "final_value", "contains_value", "monotonic", "duration", "no_report",
                 "in_range", "count", "default_response", "read_value", "report_matches_read"}
VERDICT_RANK = {"error": 5, "fail": 4, "inconclusive": 3, "skipped": 2, "pass": 1}
VERDICT_LABELS = {"pass": "通过", "fail": "不通过", "inconclusive": "无法判定", "error": "错误",
                  "skipped": "已跳过", "not-run": "未运行", "aborted": "已中止", "running": "运行中",
                  "warn": "提示", "precondition-failed": "前置条件未满足"}
RUN_STATUS_LABELS = {"setup": "绑定与上报配置", "running": "运行中", "completed": "已完成",
                     "aborted": "已中止", "error": "出错"}


class ScriptError(ValueError):
    pass


class Aborted(Exception):
    pass


class FatalRunError(Exception):
    """Stops the whole run, e.g. the target device was deleted or re-addressed."""


class StepError(Exception):
    """Ends the current step with status 'error'."""


class SkipCase(Exception):
    pass


def as_int(value: Any, name: str = "value") -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError as exc:
        raise ScriptError(f"{name} 不是整数: {value!r}") from exc


def norm_value(attribute: str, value: Any) -> Any:
    if ATTRIBUTES.get(attribute, (0, 0, 0))[2] == 0x10:
        if isinstance(value, str):
            return 1 if value.strip().lower() in {"on", "true", "1"} else 0
        return 1 if value else 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return as_int(value, attribute)


def display_value(attribute: str, value: Any) -> str:
    if ATTRIBUTES.get(attribute, (0, 0, 0))[2] == 0x10 and value in (0, 1):
        return "ON" if value else "OFF"
    return str(value)


def attribute_name(cluster: int, attribute: int) -> str:
    return ATTRIBUTE_BY_ID.get((cluster, attribute), f"0x{cluster:04X}:0x{attribute:04X}")


def hex_value(data_type: int, value: int) -> str:
    size = TYPE_SIZES.get(data_type)
    if size is None:
        raise ScriptError(f"不支持写入类型 0x{data_type:02X}")
    if not 0 <= value < (1 << (8 * size)):
        raise ScriptError(f"取值 {value} 超出类型 0x{data_type:02X} 范围")
    return "{" + " ".join(f"{b:02X}" for b in value.to_bytes(size, "little")) + "}"


def move_mode(value: Any) -> int:
    if isinstance(value, str) and value.strip().lower() in {"up", "down"}:
        return 0 if value.strip().lower() == "up" else 1
    mode = as_int(value, "mode")
    if mode not in (0, 1):
        raise ScriptError("mode 必须是 up/down 或 0/1")
    return mode


def ranged(params: dict[str, Any], key: str, maximum: int, default: int | None = None) -> int:
    if key not in params:
        if default is None:
            raise ScriptError(f"缺少参数 {key}")
        return default
    value = as_int(params[key], key)
    if not 0 <= value <= maximum:
        raise ScriptError(f"{key} 超出范围 0..{maximum}")
    return value


def build_command(params: dict[str, Any], next_seq: Callable[[], int]) -> tuple[list[str], int, str]:
    """Return (CLI lines without the send line, cluster, short description)."""
    name = params.get("command")
    mask = lambda: ranged(params, "options_mask", 255, 0)  # noqa: E731
    override = lambda: ranged(params, "options_override", 255, 0)  # noqa: E731
    if name in {"on", "off", "toggle"}:
        return [f"zcl on-off {name}"], CLUSTER_ON_OFF, name.upper()
    if name == "move_to_level":
        level, transition = ranged(params, "level", 255), ranged(params, "transition", 0xFFFF)
        m, o = mask(), override()
        return ([f"zcl level-control mv-to-level {level} {transition} {m} {o}"], CLUSTER_LEVEL,
                f"Move To Level L={level} T={transition} mask={m} override={o}")
    if name == "move_to_level_on_off":
        level, transition = ranged(params, "level", 255), ranged(params, "transition", 0xFFFF)
        return ([f"zcl level-control o-mv-to-level {level} {transition}"], CLUSTER_LEVEL,
                f"Move To Level (with On/Off) L={level} T={transition}")
    if name == "move":
        mode, rate = move_mode(params.get("mode")), ranged(params, "rate", 255)
        m, o = mask(), override()
        return ([f"zcl level-control move {mode} {rate} {m} {o}"], CLUSTER_LEVEL,
                f"Move {'UP' if mode == 0 else 'DOWN'} rate={rate} mask={m} override={o}")
    if name == "move_on_off":
        mode, rate = move_mode(params.get("mode")), ranged(params, "rate", 255)
        return ([f"zcl level-control o-move {mode} {rate}"], CLUSTER_LEVEL,
                f"Move (with On/Off) {'UP' if mode == 0 else 'DOWN'} rate={rate}")
    if name == "step":
        mode, size = move_mode(params.get("mode")), ranged(params, "size", 255)
        transition, m, o = ranged(params, "transition", 0xFFFF), mask(), override()
        return ([f"zcl level-control step {mode} {size} {transition} {m} {o}"], CLUSTER_LEVEL,
                f"Step {'UP' if mode == 0 else 'DOWN'} size={size} T={transition}")
    if name == "step_on_off":
        mode, size = move_mode(params.get("mode")), ranged(params, "size", 255)
        transition = ranged(params, "transition", 0xFFFF)
        return ([f"zcl level-control o-step {mode} {size} {transition}"], CLUSTER_LEVEL,
                f"Step (with On/Off) {'UP' if mode == 0 else 'DOWN'} size={size} T={transition}")
    if name == "stop":
        m, o = mask(), override()
        return [f"zcl level-control stop {m} {o}"], CLUSTER_LEVEL, f"Stop mask={m} override={o}"
    if name == "stop_on_off":
        return ["zcl level-control o-stop"], CLUSTER_LEVEL, "Stop (with On/Off)"
    if name == "raw":
        cluster = ranged(params, "cluster", 0xFFFF)
        if "frame" in params:
            text = re.sub(r"[\s{}]", "", str(params["frame"]))
            if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}){3,}", text):
                raise ScriptError("frame 必须是至少 3 字节的十六进制 ZCL 帧")
            data = bytes.fromhex(text)
        else:
            command_id = ranged(params, "command_id", 255)
            payload_text = re.sub(r"[\s{}]", "", str(params.get("payload", "")))
            if not re.fullmatch(r"(?:[0-9A-Fa-f]{2})*", payload_text):
                raise ScriptError("payload 必须是十六进制字节")
            fc = 0x01 if params.get("cluster_specific", True) else 0x00
            if params.get("disable_default_response"):
                fc |= 0x10
            data = bytes([fc, next_seq() & 0xFF, command_id]) + bytes.fromhex(payload_text)
        frame_hex = " ".join(f"{b:02X}" for b in data)
        return [f"raw 0x{cluster:04X} {{{frame_hex}}}"], cluster, f"Raw 0x{cluster:04X} {{{frame_hex}}}"
    raise ScriptError(f"未知命令 {name!r}")


def _validate_matcher(matcher: Any, where: str, errors: list[str]) -> None:
    if not isinstance(matcher, dict):
        errors.append(f"{where}: matcher 必须是对象")
        return
    kind = matcher.get("kind")
    if kind not in MATCHER_KINDS:
        errors.append(f"{where}: 未知 matcher {kind!r}")
        return
    attribute = matcher.get("attribute", "currentLevel")
    if kind != "default_response" and attribute not in ATTRIBUTES:
        errors.append(f"{where}: 未知属性 {attribute!r}")
    if matcher.get("severity", "required") not in {"required", "info"}:
        errors.append(f"{where}: severity 只能是 required/info")
    needs = {"first_value": ["value"], "final_value": ["value"], "contains_value": ["value"],
             "read_value": ["value"], "duration": ["ms"], "monotonic": ["direction"]}
    for key in needs.get(kind, []):
        if key not in matcher:
            errors.append(f"{where}: {kind} 缺少 {key}")
    if kind == "monotonic" and matcher.get("direction") not in {"up", "down"}:
        errors.append(f"{where}: direction 只能是 up/down")
    for key in ("tolerance", "ms", "tolerance_ms", "tolerance_pct", "within_ms", "min", "max", "status"):
        if key in matcher:
            try:
                as_int(matcher[key], key) if key != "tolerance_pct" else float(matcher[key])
            except (ScriptError, ValueError, TypeError):
                errors.append(f"{where}: {key} 不是数字")


def _validate_command(params: Any, where: str, errors: list[str]) -> None:
    if not isinstance(params, dict):
        errors.append(f"{where}: 命令必须是对象")
        return
    try:
        build_command(params, lambda: 0)
    except ScriptError as exc:
        errors.append(f"{where}: {exc}")


def validate_step(step: Any, where: str, errors: list[str]) -> None:
    if not isinstance(step, dict):
        errors.append(f"{where}: 步骤必须是对象")
        return
    kind = step.get("type")
    if kind not in STEP_TYPES:
        errors.append(f"{where}: 未知步骤类型 {kind!r}")
        return
    if kind == "write_attributes":
        attributes, raw = step.get("attributes") or {}, step.get("raw") or []
        if not isinstance(attributes, dict) or not isinstance(raw, list) or not (attributes or raw):
            errors.append(f"{where}: write_attributes 需要 attributes 或 raw")
            return
        for name, value in attributes.items():
            if name not in ATTRIBUTES:
                errors.append(f"{where}: 未知属性 {name!r}")
                continue
            try:
                hex_value(ATTRIBUTES[name][2], norm_value(name, value))
            except ScriptError as exc:
                errors.append(f"{where}: {name}: {exc}")
        for index, item in enumerate(raw):
            try:
                ranged(item, "cluster", 0xFFFF), ranged(item, "id", 0xFFFF)
                hex_value(ranged(item, "type", 255), as_int(item.get("value"), "value"))
            except (ScriptError, AttributeError, TypeError) as exc:
                errors.append(f"{where}.raw[{index}]: {exc}")
    elif kind == "read":
        names = step.get("attributes")
        if not isinstance(names, list) or not names:
            errors.append(f"{where}: read 需要 attributes 列表")
            return
        for name in names:
            if name not in ATTRIBUTES:
                errors.append(f"{where}: 未知属性 {name!r}")
        for name in (step.get("expect") or {}):
            if name not in names:
                errors.append(f"{where}: expect 中的 {name!r} 未读取")
    elif kind == "set_state":
        if not isinstance(step.get("on"), bool):
            errors.append(f"{where}: set_state.on 必须是 true/false")
        try:
            level = as_int(step.get("level"), "level")
            if not 1 <= level <= 254:
                errors.append(f"{where}: set_state.level 必须在 1..254")
        except ScriptError as exc:
            errors.append(f"{where}: {exc}")
    elif kind == "command":
        _validate_command(step, where, errors)
    elif kind == "burst":
        frames = step.get("frames")
        if not isinstance(frames, list) or len(frames) < 2:
            errors.append(f"{where}: burst 需要至少两个 frames")
        else:
            for index, frame in enumerate(frames):
                _validate_command(frame, f"{where}.frames[{index}]", errors)
        if step.get("transport", "auto") not in {"auto", "cli", "firmware"}:
            errors.append(f"{where}: transport 只能是 auto/cli/firmware")
        for key in ("gap_ms", "gap_tolerance_ms"):
            if key in step:
                try:
                    if as_int(step[key], key) < 0:
                        errors.append(f"{where}: {key} 不能为负")
                except ScriptError as exc:
                    errors.append(f"{where}: {exc}")
    elif kind == "expect":
        matchers = step.get("matchers")
        if not isinstance(matchers, list) or not matchers:
            errors.append(f"{where}: expect 需要 matchers 列表")
        else:
            for index, matcher in enumerate(matchers):
                _validate_matcher(matcher, f"{where}.matchers[{index}]", errors)
        for name in step.get("read_back") or []:
            if name not in ATTRIBUTES:
                errors.append(f"{where}: read_back 未知属性 {name!r}")
        for key in ("window_ms", "min_ms"):
            if key in step:
                try:
                    as_int(step[key], key)
                except ScriptError as exc:
                    errors.append(f"{where}: {exc}")
    elif kind == "wait":
        if "ms" not in step and "quiet_ms" not in step:
            errors.append(f"{where}: wait 需要 ms 或 quiet_ms")
    elif kind in {"operator", "confirm"}:
        if not isinstance(step.get("prompt"), str) or not step["prompt"].strip():
            errors.append(f"{where}: {kind} 需要 prompt 文本")


def validate_script(script: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(script, dict):
        return ["脚本必须是 JSON 对象"]
    if script.get("format") != SCRIPT_FORMAT:
        errors.append(f"format 必须是 {SCRIPT_FORMAT!r}")
    if not isinstance(script.get("id"), str) or not ID_RE.fullmatch(script["id"]):
        errors.append("id 必须是 1-64 个字母、数字、点、下划线或连字符")
    setup = script.get("setup", {})
    if not isinstance(setup, dict):
        errors.append("setup 必须是对象")
    else:
        for index, item in enumerate(setup.get("reporting", DEFAULT_REPORTING)):
            if not isinstance(item, dict) or item.get("attribute") not in ATTRIBUTES:
                errors.append(f"setup.reporting[{index}]: 未知属性")
    cases = script.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases 必须是非空列表")
        return errors
    seen: set[str] = set()
    for case_index, case in enumerate(cases):
        where = f"cases[{case_index}]"
        if not isinstance(case, dict):
            errors.append(f"{where}: 用例必须是对象")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not ID_RE.fullmatch(case_id):
            errors.append(f"{where}: id 无效")
        elif case_id in seen:
            errors.append(f"{where}: id {case_id!r} 重复")
        else:
            seen.add(case_id)
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{where}: steps 必须是非空列表")
            continue
        for step_index, step in enumerate(steps):
            validate_step(step, f"{where}.steps[{step_index}]", errors)
    return errors


def case_flags(case: dict[str, Any]) -> dict[str, bool]:
    steps = case.get("steps", [])
    return {
        "operator": any(step.get("type") == "operator" for step in steps),
        "confirm": any(step.get("type") == "confirm" for step in steps),
        "timing": any(step.get("type") == "burst" and (as_int(step.get("gap_ms", 0)) <= 100
                                                        or step.get("transport") == "firmware") for step in steps),
    }


def parse_script_text(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > SCRIPT_MAX_BYTES:
        raise ScriptError("脚本超过 1 MiB")
    try:
        script = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptError(f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}") from exc
    errors = validate_script(script)
    if errors:
        shown = errors[:20] + ([f"…另有 {len(errors) - 20} 个错误"] if len(errors) > 20 else [])
        raise ScriptError("脚本校验失败：\n" + "\n".join(shown))
    return script


class ScriptStore:
    def __init__(self, builtin_dir: Path, user_dir: Path) -> None:
        self.builtin_dir = Path(builtin_dir)
        self.user_dir = Path(user_dir)
        self.lock = threading.RLock()

    def _paths(self) -> list[tuple[Path, bool]]:
        paths: list[tuple[Path, bool]] = []
        for directory, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if directory.is_dir():
                paths += [(path, builtin) for path in sorted(directory.glob("*.json")) if path.is_file()]
        return paths

    def _load(self, path: Path) -> dict[str, Any]:
        return parse_script_text(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        items = []
        with self.lock:
            for path, builtin in self._paths():
                try:
                    script = self._load(path)
                except (OSError, ScriptError) as exc:
                    items.append({"id": path.stem, "title": path.name, "builtin": builtin, "error": str(exc)})
                    continue
                flags = [case_flags(case) for case in script["cases"]]
                items.append({
                    "id": script["id"], "title": script.get("title") or script["id"],
                    "source": script.get("source", ""), "description": script.get("description", ""),
                    "cases": len(script["cases"]), "operator_cases": sum(f["operator"] for f in flags),
                    "confirm_cases": sum(f["confirm"] for f in flags), "builtin": builtin,
                    "modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                })
        return items

    def find(self, script_id: str) -> tuple[Path, bool]:
        if not isinstance(script_id, str) or not ID_RE.fullmatch(script_id):
            raise KeyError(script_id)
        for path, builtin in self._paths():
            if path.stem == script_id:
                return path, builtin
        for path, builtin in self._paths():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("id") == script_id:
                    return path, builtin
            except (OSError, ValueError, AttributeError):
                continue
        raise KeyError(script_id)

    def get(self, script_id: str) -> dict[str, Any]:
        with self.lock:
            path, builtin = self.find(script_id)
            script = self._load(path)
        script["builtin"] = builtin
        for case in script["cases"]:
            case["flags"] = case_flags(case)
        return script

    def save(self, text: str, *, overwrite: bool = False) -> dict[str, Any]:
        script = parse_script_text(text)
        with self.lock:
            try:
                path, builtin = self.find(script["id"])
                if builtin:
                    raise FileExistsError("内置脚本不能被覆盖，请修改脚本 id")
                if not overwrite:
                    raise FileExistsError(f"脚本 {script['id']} 已存在")
            except KeyError:
                pass
            self.user_dir.mkdir(parents=True, exist_ok=True)
            target = self.user_dir / f"{script['id']}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(target)
        return {"id": script["id"], "title": script.get("title") or script["id"], "cases": len(script["cases"])}

    def delete(self, script_id: str) -> str:
        with self.lock:
            path, builtin = self.find(script_id)
            if builtin:
                raise PermissionError("内置脚本不能删除")
            path.unlink()
        return script_id


class ResultStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.lock = threading.RLock()

    def _path(self, result_id: str, suffix: str = ".json") -> Path:
        if not isinstance(result_id, str) or not RESULT_ID_RE.fullmatch(result_id):
            raise KeyError(result_id)
        return self.directory / f"{result_id}{suffix}"

    def save(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            for suffix, payload in ((".json", result), (".summary.json", summarize_result(result))):
                target = self._path(result["id"], suffix)
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                temporary.replace(target)

    def list(self) -> list[dict[str, Any]]:
        items = []
        with self.lock:
            if not self.directory.is_dir():
                return []
            for path in self.directory.glob("*.summary.json"):
                try:
                    items.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return sorted(items, key=lambda item: item.get("started_at") or "", reverse=True)

    def get(self, result_id: str) -> dict[str, Any]:
        path = self._path(result_id)
        with self.lock:
            if not path.is_file():
                raise KeyError(result_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, result_id: str) -> str:
        with self.lock:
            path = self._path(result_id)
            if not path.is_file():
                raise KeyError(result_id)
            path.unlink()
            self._path(result_id, ".summary.json").unlink(missing_ok=True)
        return result_id


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result.get(key) for key in ("id", "status", "started_at", "finished_at", "summary", "message")} | {
        "script": {k: result.get("script", {}).get(k) for k in ("id", "title")},
        "device": {k: result.get("device", {}).get(k) for k in ("id", "name", "model", "eui64", "nodeId", "endpoint", "swBuildId")},
        "cases": len(result.get("cases", [])),
    }


# ---------------------------------------------------------------- matchers

def _within(actual: Any, expected: Any, tolerance: int) -> bool:
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return actual == expected


def evaluate_matcher(matcher: dict[str, Any], observations: list[dict[str, Any]],
                     context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate one matcher against observations (t in ms relative to the anchor)."""
    context = context or {}
    kind = matcher["kind"]
    attribute = matcher.get("attribute", "currentLevel")
    severity = matcher.get("severity", "required")
    tolerance = as_int(matcher.get("tolerance", 0), "tolerance")
    reports = [o for o in observations if o["attribute"] == attribute and o["source"] == "report"]
    reads = [o for o in observations if o["attribute"] == attribute and o["source"] == "read"]
    values = [o["value"] for o in reports]
    shown = lambda items: ", ".join(display_value(attribute, v) for v in items) or "无"  # noqa: E731
    result: dict[str, Any] = {"kind": kind, "attribute": attribute, "severity": severity}
    expected_value = norm_value(attribute, matcher["value"]) if "value" in matcher else None
    tol_text = f" ±{tolerance}" if tolerance else ""

    def done(ok: bool, expected: Any, actual: Any, message: str) -> dict[str, Any]:
        result.update(ok=bool(ok), expected=expected, actual=actual, message=message)
        return result

    if kind == "first_value":
        actual = values[0] if values else None
        ok = actual is not None and _within(actual, expected_value, tolerance)
        return done(ok, expected_value, actual,
                    f"首个 {attribute} 上报 = {display_value(attribute, actual) if actual is not None else '无'}，"
                    f"期望 {display_value(attribute, expected_value)}{tol_text}")
    if kind in {"final_value", "read_value"}:
        source = matcher.get("source", "read" if kind == "read_value" else "report")
        pool = {"report": reports or ([] if kind == "read_value" else reads), "read": reads,
                "any": sorted(reports + reads, key=lambda o: o["t"])}.get(source, reports)
        if source == "report" and not reports and kind == "final_value":
            pool = reads
        actual = pool[-1]["value"] if pool else None
        ok = actual is not None and _within(actual, expected_value, tolerance)
        label = "读回" if pool is reads else "最终上报"
        return done(ok, expected_value, actual,
                    f"{label} {attribute} = {display_value(attribute, actual) if actual is not None else '无'}，"
                    f"期望 {display_value(attribute, expected_value)}{tol_text}")
    if kind == "report_matches_read":
        reported = values[-1] if values else None
        read = reads[-1]["value"] if reads else None
        ok = reported is not None and read is not None and _within(reported, read, tolerance)
        return done(ok, read, reported,
                    f"最终上报 {attribute} = {display_value(attribute, reported) if reported is not None else '无'}，"
                    f"读回 = {display_value(attribute, read) if read is not None else '无'}{tol_text}")
    if kind == "contains_value":
        ok = any(_within(v, expected_value, tolerance) for v in values)
        return done(ok, expected_value, values,
                    f"{attribute} 上报 [{shown(values)}] {'包含' if ok else '不包含'} "
                    f"{display_value(attribute, expected_value)}{tol_text}")
    if kind == "monotonic":
        direction = matcher["direction"]
        strict = bool(matcher.get("strict"))
        bad = []
        for previous, current in zip(values, values[1:]):
            if direction == "up" and (current < previous or (strict and current == previous)):
                bad.append((previous, current))
            if direction == "down" and (current > previous or (strict and current == previous)):
                bad.append((previous, current))
        ok = len(values) >= 1 and not bad
        detail = "；反向: " + ", ".join(f"{a}→{b}" for a, b in bad[:5]) if bad else ""
        return done(ok, direction, values,
                    f"{attribute} 上报 [{shown(values)}] {'单调' if ok else '不单调'}"
                    f"{'递增' if direction == 'up' else '递减'}{detail}" if values else f"没有 {attribute} 上报")
    if kind == "duration":
        expected_ms = as_int(matcher["ms"], "ms")
        slack = max(as_int(matcher.get("tolerance_ms", 0), "tolerance_ms"),
                    int(expected_ms * float(matcher.get("tolerance_pct", 0)) / 100))
        pool = reports
        if matcher.get("until") == "final_value" and "value" in matcher:
            pool = [o for o in reports if _within(o["value"], expected_value, tolerance)][:1]
        # "from": "first_report" measures from the first report of any attribute,
        # e.g. after a knob press whose exact moment the panel cannot see.
        first = [o["t"] for o in observations if o["source"] == "report"]
        origin = min(first) if matcher.get("from") == "first_report" and first else 0
        actual = pool[-1]["t"] - origin if pool else None
        start_text = "首个上报" if origin else "锚点"
        ok = actual is not None and abs(actual - expected_ms) <= slack
        return done(ok, {"ms": expected_ms, "tolerance_ms": slack}, actual,
                    f"{attribute} 最后上报距{start_text} {actual} ms，期望约 {expected_ms} ±{slack} ms"
                    if actual is not None else f"没有 {attribute} 上报，无法计算时长")
    if kind == "no_report":
        within = matcher.get("within_ms")
        pool = reports if within is None else [o for o in reports if o["t"] <= as_int(within, "within_ms")]
        scope = "窗口内" if within is None else f"前 {as_int(within)} ms 内"
        return done(not pool, "无上报", [o["value"] for o in pool],
                    f"{scope}{'没有' if not pool else '收到'} {attribute} 上报"
                    + (f" [{shown([o['value'] for o in pool])}]" if pool else ""))
    if kind == "in_range":
        low, high = as_int(matcher.get("min", 0), "min"), as_int(matcher.get("max", 255), "max")
        pool = values[-1:] if matcher.get("scope") == "final" else values
        bad = [v for v in pool if not low <= v <= high]
        ok = bool(pool) and not bad
        return done(ok, [low, high], pool, f"{attribute} 上报 [{shown(pool)}] {'均在' if ok else '不全在'} {low}..{high}")
    if kind == "count":
        low, high = as_int(matcher.get("min", 0), "min"), as_int(matcher.get("max", 10 ** 6), "max")
        ok = low <= len(values) <= high
        return done(ok, [low, high], len(values), f"{attribute} 上报 {len(values)} 次，期望 {low}..{high} 次")
    if kind == "default_response":
        expected_status = as_int(matcher.get("status", 0), "status")
        responses = context.get("default_responses", [])
        actual = responses[-1]["status"] if responses else None
        ok = actual == expected_status
        return done(ok, expected_status, actual,
                    f"Default Response 状态 = {('0x%02X' % actual) if actual is not None else '未收到'}，"
                    f"期望 0x{expected_status:02X}")
    raise ScriptError(f"未知 matcher {kind!r}")


def step_status_from_matchers(results: list[dict[str, Any]]) -> str:
    if any(not r["ok"] and r["severity"] == "required" for r in results):
        return "fail"
    if any(not r["ok"] for r in results):
        return "warn"
    return "pass"


def case_verdict(steps: list[dict[str, Any]]) -> str:
    # A skipped manual confirmation is neutral: the report shows it as
    # unconfirmed, and the automatic checks still decide the verdict.
    verdict = "pass"
    mapping = {"error": "error", "fail": "fail", "inconclusive": "inconclusive",
               "precondition-failed": "inconclusive", "aborted": "error"}
    for step in steps:
        candidate = mapping.get(step.get("status"), "pass")
        if VERDICT_RANK.get(candidate, 0) > VERDICT_RANK.get(verdict, 0):
            verdict = candidate
    return verdict


# ---------------------------------------------------------------- runner

class TestRunner:
    # Phase-2 hook: CLI template for a firmware burst command, e.g.
    # "custom burst {node} {endpoint} {gap_ms} {frames}". None in phase 1.
    burst_command: str | None = None

    def __init__(self, gateway: Any, device_center: Any, scripts: ScriptStore, results: ResultStore, *,
                 publish: Callable[[dict[str, Any]], None] | None = None,
                 gateway_eui64: Callable[[], str | None] | None = None,
                 busy_check: Callable[[], str | None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 response_timeout: float = 3.0, tx_timeout: float = 1.0,
                 bind_timeout: float = 5.0, inter_command_delay: float = 0.05) -> None:
        self.gateway = gateway
        self.device_center = device_center
        self.scripts = scripts
        self.results = results
        self.publish_hook = publish
        self.gateway_eui64 = gateway_eui64 or (lambda: None)
        self.busy_check = busy_check or (lambda: None)
        self.clock = clock
        self.response_timeout = response_timeout
        self.tx_timeout = tx_timeout
        self.bind_timeout = bind_timeout
        self.inter_command_delay = inter_command_delay
        self.cond = threading.Condition()
        self.abort_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active = False
        self.result: dict[str, Any] | None = None
        self.state: dict[str, Any] = {"active": False, "status": "idle"}
        self.events: deque[dict[str, Any]] = deque(maxlen=RUN_EVENT_LIMIT)
        self.event_seq = 0
        self.target: dict[str, Any] | None = None
        self.prompt: dict[str, Any] | None = None
        self.response: dict[str, Any] | None = None
        self.raw_seq = 0xA0
        self.t0 = 0.0
        self.known: dict[str, Any] = {}
        self.log_lines: deque[str] = deque(maxlen=200)
        device_center.add_listener(self._on_event)

    # -------------------------------------------------------- public API

    def status(self) -> dict[str, Any]:
        with self.cond:
            state = copy.deepcopy(self.state)
            state["prompt"] = copy.deepcopy(self.prompt)
            state["log"] = list(self.log_lines)[-60:]
            if self.result is not None:
                state["cases"] = [self._case_brief(case) for case in self.result["cases"]]
                state["summary"] = self.result.get("summary")
                state["result_id"] = self.result["id"]
        return state

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("请求必须是对象")
        with self.cond:
            if self.active:
                raise ValueError("已有测试在运行")
        busy = self.busy_check()
        if busy:
            raise ValueError(f"{busy}正在运行，不能开始测试")
        if request.get("mode", "report") not in {"report", "poll"}:
            raise ValueError("mode 只能是 report 或 poll")
        if request.get("mode") == "poll":
            raise ValueError("Hue 轮询模式（mode=poll）是第二阶段功能，当前版本未启用")
        manual = request.get("manual", "ask")
        if manual not in {"ask", "skip"}:
            raise ValueError("manual 只能是 ask 或 skip")
        try:
            script = self.scripts.get(str(request.get("script") or ""))
        except KeyError as exc:
            raise ValueError("脚本不存在") from exc
        wanted = request.get("cases") or [case["id"] for case in script["cases"]]
        if not isinstance(wanted, list):
            raise ValueError("cases 必须是列表")
        cases = [case for case in script["cases"] if case["id"] in set(wanted)]
        if not cases:
            raise ValueError("没有选择任何用例")
        if not self.gateway.running():
            raise ValueError("请先启动网关")
        key = str(request.get("device") or "")
        device = self.device_center.get(key)
        if device.get("role") == "gateway":
            raise ValueError("不能以网关自身为测试目标")
        if device.get("identity") != "known" or not device.get("nodeId") or not device.get("addressVerified"):
            raise ValueError("设备身份或当前地址待确认，请先刷新资料")
        endpoint = as_int(request.get("endpoint", script.get("endpoint", 1)), "endpoint")
        endpoints = device.get("endpoints") or {}
        if str(endpoint) not in endpoints:
            raise ValueError(f"设备没有已发现的端点 {endpoint}")
        clusters = endpoints[str(endpoint)].get("inClusters") or []
        if CLUSTER_ON_OFF not in clusters:
            raise ValueError(f"端点 {endpoint} 不支持 On/Off 簇")
        run_setup = bool(request.get("setup", True))
        gateway_eui = self.gateway_eui64() if run_setup and script.get("setup", {}).get("bind", True) else None
        if run_setup and script.get("setup", {}).get("bind", True) and not gateway_eui:
            raise ValueError("未知网关 EUI64，请确认网关已启动并已执行 info")
        self.device_center.lock_for_test(key)
        started = datetime.now().astimezone()
        run_id = f"{started:%Y%m%d-%H%M%S}-{script['id']}-{uuid.uuid4().hex[:4]}"
        with self.cond:
            self.active = True
            self.abort_event.clear()
            self.events.clear()
            self.prompt = None
            self.response = None
            self.known = {}
            self.log_lines.clear()
            self.t0 = self.clock()
            self.target = {"id": key, "node": int(device["nodeId"], 16), "nodeId": device["nodeId"],
                           "generation": device["generation"], "endpoint": endpoint, "eui64": device.get("eui64"),
                           "gateway_eui64": gateway_eui}
            self.result = {
                "format": RESULT_FORMAT, "id": run_id, "status": "setup",
                "script": {k: script.get(k) for k in ("id", "title", "source", "description")},
                "device": {"id": key, "name": device.get("name"), "eui64": device.get("eui64"),
                           "nodeId": device["nodeId"], "endpoint": endpoint, "manufacturer": device.get("manufacturer"),
                           "model": device.get("model"), "swBuildId": device.get("swBuildId"),
                           "dateCode": device.get("dateCode")},
                "gateway": {"session": getattr(self.gateway, "session_id", None), "eui64": gateway_eui},
                "options": {"setup": run_setup, "manual": manual, "stop_on_fail": bool(request.get("stop_on_fail")),
                            "mode": "report", "cases": [case["id"] for case in cases]},
                "started_at": started.isoformat(timespec="seconds"), "finished_at": None,
                "setup": {"steps": [], "status": "pending"},
                "cases": [self._new_case(case) for case in cases],
                "summary": {}, "message": None,
            }
            self.state = {"active": True, "status": "setup", "run_id": run_id,
                          "script": self.result["script"], "device": self.result["device"],
                          "total": len(cases), "done": 0, "current": None, "message": None,
                          "started_at": self.result["started_at"]}
            self._update_summary()
        self.thread = threading.Thread(target=self._run, args=(script, cases), name="device-test-runner", daemon=True)
        self.thread.start()
        self._publish_state()
        return self.status()

    def abort(self, reason: str = "user") -> dict[str, Any]:
        with self.cond:
            if not self.active:
                return self.status()
            self.state["message"] = {"user": "已请求中止", "gateway-stop": "网关停止，测试中止",
                                     "server-stop": "面板服务停止，测试中止"}.get(reason, reason)
            self.abort_event.set()
            self.cond.notify_all()
        self._log(f"中止请求：{reason}")
        return self.status()

    def respond(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        comment = str(payload.get("comment") or "")[:1000]
        with self.cond:
            prompt = self.prompt
            if not self.active or prompt is None:
                raise ValueError("当前没有等待处理的提示")
            if payload.get("prompt_id") != prompt["id"]:
                raise ValueError("提示已过期，请刷新")
            allowed = {"operator": {"continue", "skip-case"}, "confirm": {"pass", "fail", "skip"}}[prompt["kind"]]
            if action not in allowed:
                raise ValueError(f"当前提示不支持操作 {action!r}")
            self.response = {"action": action, "comment": comment, "t": self.clock()}
            self.cond.notify_all()
        return {"ok": True}

    # -------------------------------------------------------- events

    def _on_event(self, event: dict[str, Any], received: float) -> None:
        target = self.target
        if not self.active or target is None or event.get("node") != target["node"]:
            return
        kind = event.get("event")
        if kind not in {"rx", "tx", "sent"}:
            return
        record: dict[str, Any] = {"kind": kind, "t": received, "host_t": event.get("t"),
                                  "cluster": event.get("cluster"), "profile": event.get("profile"),
                                  "ep": event.get("ep"), "dest_ep": event.get("destEp"),
                                  "status": event.get("status"), "data": str(event.get("data") or "")}
        if record["profile"] == 0:
            try:
                raw = bytes.fromhex(record["data"])
            except ValueError:
                raw = b""
            record["zdo_seq"] = raw[0] if raw else None
            record["zdo_status"] = raw[1] if len(raw) > 1 else None
        else:
            frame = parse_zcl_frame(record["data"])
            if frame is not None:
                record["zcl_seq"] = frame["seq"]
                record["command_id"] = frame["command_id"]
                record["cluster_specific"] = frame["cluster_specific"]
                if kind == "rx":
                    try:
                        record["decoded"] = decode_global(frame)
                    except ZclDecodeError as exc:
                        record["decode_error"] = str(exc)
        with self.cond:
            self.event_seq += 1
            record["n"] = self.event_seq
            self.events.append(record)
            self.cond.notify_all()

    def _events_after(self, mark: int) -> list[dict[str, Any]]:
        # Caller holds self.cond.
        found = []
        for record in reversed(self.events):
            if record["n"] <= mark:
                break
            found.append(record)
        found.reverse()
        return found

    def _wait_event(self, predicate: Callable[[dict[str, Any]], bool], mark: int, timeout: float) -> dict[str, Any] | None:
        deadline = self.clock() + timeout
        with self.cond:
            checked = mark
            while True:
                for record in self._events_after(checked):
                    checked = record["n"]
                    if predicate(record):
                        return record
                if self.abort_event.is_set():
                    raise Aborted()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.05))

    def _sleep(self, seconds: float) -> None:
        if seconds > 0 and self.abort_event.wait(seconds):
            raise Aborted()
        if self.abort_event.is_set():
            raise Aborted()

    def _ms(self, t: float) -> int:
        return int(round((t - self.t0) * 1000))

    # -------------------------------------------------------- sending

    def _check_target(self) -> None:
        if self.abort_event.is_set():
            raise Aborted()
        if not self.gateway.running():
            raise FatalRunError("网关已停止")
        target = self.target
        try:
            device = self.device_center.get(target["id"])
        except ValueError as exc:
            raise FatalRunError("测试设备已被删除") from exc
        if device.get("generation") != target["generation"] or device.get("nodeId") != target["nodeId"]:
            raise FatalRunError("设备注册或短地址已变化，测试中止")

    def _send_line(self) -> str:
        return f"send 0x{self.target['node']:04X} 1 {self.target['endpoint']}"

    def _next_raw_seq(self) -> int:
        self.raw_seq = (self.raw_seq + 1) & 0xFF
        return self.raw_seq

    def _send(self, lines: list[str], cluster: int, step: dict[str, Any], description: str,
              *, zdo: bool = False, expect_command: int | None = None) -> dict[str, Any]:
        self._check_target()
        with self.cond:
            mark = self.event_seq
        started = self.clock()
        commands = lines if zdo else lines + [self._send_line()]
        try:
            self.gateway.send_command_sequence(commands, inter_command_delay=self.inter_command_delay)
        except ValueError as exc:
            raise StepError(f"CLI 发送失败：{exc}") from exc
        returned = self.clock()

        def ours(record: dict[str, Any]) -> bool:
            # The host answers device frames with automatic Default Responses on
            # the same cluster; one can be emitted between the mark and our frame.
            if record["kind"] != "tx" or record["cluster"] != cluster:
                return False
            if zdo or record.get("command_id") is None:
                return True
            if not record.get("cluster_specific") and record["command_id"] == 0x0B:
                return False
            return expect_command is None or (not record.get("cluster_specific")
                                              and record["command_id"] == expect_command)
        tx = self._wait_event(ours, mark, self.tx_timeout)
        info = {"description": description, "commands": commands, "cluster": cluster,
                "t_send_ms": self._ms(started), "t_returned_ms": self._ms(returned),
                "t_ms": self._ms(tx["t"]) if tx else self._ms(returned), "tx_seen": tx is not None,
                "data": tx["data"] if tx else None, "zcl_seq": (tx or {}).get("zcl_seq"),
                "zdo_seq": (tx or {}).get("zdo_seq"), "command_id": (tx or {}).get("command_id"),
                "_t": tx["t"] if tx else returned, "_host_t": (tx or {}).get("host_t"), "_mark": mark}
        step.setdefault("tx", []).append(info)
        if tx is None:
            self._log(f"未观察到 {description} 的 @Z3 tx 事件，时间以 CLI 返回为准")
        return info

    def _no_reply_message(self, info: dict[str, Any], action: str, reply: str = "响应") -> str:
        if not info["tx_seen"]:
            return f"{action}未发出：网关没有产生对应的 @Z3 tx 事件，请查看高级控制台日志中的 CLI 输出"
        return f"{action}已发出（ZCL Seq {info.get('zcl_seq')}），{self.response_timeout:g} 秒内未收到 {reply}"

    def _response(self, info: dict[str, Any], command_id: int, timeout: float | None = None) -> dict[str, Any] | None:
        cluster = info["cluster"]
        seq = info.get("zcl_seq")

        def matches(record: dict[str, Any]) -> bool:
            return (record["kind"] == "rx" and record["cluster"] == cluster and record.get("profile") != 0
                    and record.get("command_id") == command_id and not record.get("cluster_specific")
                    and (seq is None or record.get("zcl_seq") == seq))
        return self._wait_event(matches, info["_mark"], self.response_timeout if timeout is None else timeout)

    def _record_sent_status(self, step: dict[str, Any]) -> None:
        with self.cond:
            for info in step.get("tx", []):
                if "aps_status" in info:
                    continue
                for record in self._events_after(info["_mark"]):
                    if record["kind"] == "sent" and record["cluster"] == info["cluster"] and (
                            info.get("zcl_seq") is None or record.get("zcl_seq") == info.get("zcl_seq")) and (
                            info.get("command_id") is None or record.get("command_id") == info.get("command_id")):
                        info["aps_status"] = record["status"]
                        break

    def _observation(self, record: dict[str, Any], item: dict[str, Any], source: str) -> dict[str, Any] | None:
        if "value" not in item:
            return None
        name = attribute_name(record["cluster"], item["attribute"])
        value = item["value"]
        if name in ATTRIBUTES and isinstance(value, int):
            value = norm_value(name, value)
        return {"t_ms": self._ms(record["t"]), "attribute": name, "value": value, "source": source,
                "seq": record.get("zcl_seq"), "_t": record["t"], "_host_t": record.get("host_t")}

    def _read(self, name: str, step: dict[str, Any]) -> dict[str, Any]:
        cluster, attribute, _ = ATTRIBUTES[name]
        info = self._send([f"zcl global read 0x{cluster:04X} 0x{attribute:04X}"], cluster, step, f"读取 {name}",
                          expect_command=0x00)
        record = self._response(info, 0x01)
        if record is None:
            raise StepError(self._no_reply_message(info, f"读取 {name}"))
        for item in (record.get("decoded") or {}).get("records", []):
            if item.get("attribute") == attribute:
                if item.get("status", 0) != 0:
                    raise StepError(f"读取 {name} 失败，状态 0x{item['status']:02X}")
                observation = self._observation(record, item, "read")
                if observation is None:
                    raise StepError(f"读取 {name} 的数据类型无法解析")
                self.known[name] = observation["value"]
                step.setdefault("observations", []).append(observation)
                return observation
        raise StepError(f"读取 {name} 的响应中没有该属性")

    def _write(self, cluster: int, attribute: int, data_type: int, value: int, label: str,
               step: dict[str, Any], verify: bool) -> None:
        info = self._send([f"zcl global write 0x{cluster:04X} 0x{attribute:04X} 0x{data_type:02X} "
                           f"{hex_value(data_type, value)}"], cluster, step, f"写入 {label}={value}",
                          expect_command=0x02)
        if not verify:
            return
        record = self._response(info, 0x04)
        if record is None:
            raise StepError(self._no_reply_message(info, f"写入 {label}", "Write Attributes Response"))
        statuses = [item.get("status") for item in (record.get("decoded") or {}).get("records", [])]
        if any(status != 0 for status in statuses):
            raise StepError(f"写入 {label} 被拒绝，状态 {', '.join('0x%02X' % s for s in statuses)}")

    # -------------------------------------------------------- run loop

    def _run(self, script: dict[str, Any], cases: list[dict[str, Any]]) -> None:
        status = "completed"
        message = None
        try:
            if self.result["options"]["setup"]:
                self._setup(script)
            else:
                self.result["setup"]["status"] = "skipped"
            with self.cond:
                self.result["status"] = self.state["status"] = "running"
            self._publish_state()
            for index, case in enumerate(cases):
                verdict = self._run_case(index, case)
                if verdict in {"fail", "error"} and self.result["options"]["stop_on_fail"]:
                    message = f"用例 {case.get('number') or case['id']} 未通过，按设置停止"
                    break
        except Aborted:
            status = "aborted"
            message = self.state.get("message") or "已中止"
        except FatalRunError as exc:
            status, message = "error", str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            status, message = "error", f"内部错误：{exc}"
        finally:
            with self.cond:
                for case in self.result["cases"]:
                    if case["verdict"] in {"running", "pending"}:
                        case["verdict"] = "not-run" if case["verdict"] == "pending" else (
                            "error" if status == "error" else "not-run")
                        for step in case["steps"]:
                            if step["status"] in {"running", "pending"}:
                                step["status"] = "aborted" if step["status"] == "running" else "not-run"
                self.result.update(status=status, message=message,
                                   finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                self._update_summary()
                self.prompt = None
                self.state.update(active=False, status=status, message=message, current=None)
                self.active = False
            try:
                self.device_center.unlock_test()
            except Exception:  # pragma: no cover - defensive
                traceback.print_exc()
            self._save()
            self._log(f"测试结束：{RUN_STATUS_LABELS.get(status, status)}" + (f"（{message}）" if message else ""))
            self._publish_state()

    def _setup(self, script: dict[str, Any]) -> None:
        setup = script.get("setup", {})
        container = self.result["setup"]
        target = self.target
        try:
            if setup.get("bind", True):
                for cluster in (CLUSTER_ON_OFF, CLUSTER_LEVEL):
                    step = self._new_step({"type": "bind", "label": f"绑定 0x{cluster:04X} 到网关"}, len(container["steps"]))
                    container["steps"].append(step)
                    self._begin_step(step, None)
                    info = self._send([f"zdo bind 0x{target['node']:04X} {target['endpoint']} 1 0x{cluster:04X} "
                                       f"{{{target['eui64']}}} {{{target['gateway_eui64']}}}"], ZDO_BIND_REQUEST, step,
                                      f"ZDO Bind 0x{cluster:04X}", zdo=True)
                    seq = info.get("zdo_seq")
                    record = self._wait_event(lambda r: r["kind"] == "rx" and r["profile"] == 0
                                              and r["cluster"] == ZDO_BIND_RESPONSE
                                              and (seq is None or r.get("zdo_seq") == seq),
                                              info["_mark"], self.bind_timeout)
                    if record is None:
                        raise StepError(f"绑定 0x{cluster:04X} 未收到 Bind Response")
                    if record.get("zdo_status") != 0:
                        raise StepError(f"绑定 0x{cluster:04X} 失败，ZDO 状态 0x{record.get('zdo_status') or 0:02X}")
                    self._end_step(step, "pass", "绑定成功")
            for item in setup.get("reporting", DEFAULT_REPORTING):
                name = item["attribute"]
                cluster, attribute, data_type = ATTRIBUTES[name]
                low, high = as_int(item.get("min", 0)), as_int(item.get("max", 3600))
                change = hex_value(data_type, as_int(item.get("change", 0))) if data_type != 0x10 else "{00}"
                step = self._new_step({"type": "configure_reporting",
                                       "label": f"配置 {name} 上报 min={low}s max={high}s"}, len(container["steps"]))
                container["steps"].append(step)
                self._begin_step(step, None)
                info = self._send([f"zcl global send-me-a-report 0x{cluster:04X} 0x{attribute:04X} 0x{data_type:02X} "
                                   f"{low} {high} {change}"], cluster, step, f"Configure Reporting {name}",
                                  expect_command=0x06)
                record = self._response(info, 0x07)
                if record is None:
                    raise StepError(self._no_reply_message(info, f"配置 {name} 上报", "Configure Reporting Response"))
                statuses = [r.get("status") for r in (record.get("decoded") or {}).get("records", [])]
                if any(s != 0 for s in statuses):
                    raise StepError(f"配置 {name} 上报被拒绝，状态 {', '.join('0x%02X' % s for s in statuses)}")
                self._end_step(step, "pass", "上报配置成功")
            container["status"] = "pass"
        except StepError as exc:
            container["status"] = "error"
            if container["steps"]:
                self._end_step(container["steps"][-1], "error", str(exc))
            raise FatalRunError(f"绑定/上报配置失败：{exc}") from exc
        finally:
            self._save()

    def _new_case(self, case: dict[str, Any]) -> dict[str, Any]:
        return {"id": case["id"], "number": case.get("number") or case["id"], "sheet": case.get("sheet"),
                "order": case.get("order"), "title": case.get("title") or "", "notes": case.get("notes") or "",
                "flags": case_flags(case), "verdict": "pending", "started_at": None, "t_start_ms": None,
                "t_end_ms": None, "steps": [self._new_step(step, index) for index, step in enumerate(case["steps"])]}

    def _new_step(self, step: dict[str, Any], index: int) -> dict[str, Any]:
        return {"index": index, "type": step["type"], "label": step.get("label") or describe_step(step),
                "observe": step.get("observe"), "status": "pending", "message": None,
                "t_start_ms": None, "t_end_ms": None}

    def _case_brief(self, case: dict[str, Any]) -> dict[str, Any]:
        return {"id": case["id"], "number": case["number"], "title": case["title"], "verdict": case["verdict"],
                "flags": case["flags"]}

    def _begin_step(self, step: dict[str, Any], case: dict[str, Any] | None) -> None:
        with self.cond:
            step.update(status="running", t_start_ms=self._ms(self.clock()))
            self.state["current"] = {
                "case_id": case["id"] if case else None, "number": case["number"] if case else "初始化",
                "title": case["title"] if case else "绑定与上报配置", "step_index": step["index"],
                "step_total": len(case["steps"]) if case else None, "step_label": step["label"],
                "observe": step.get("observe")}
        self._publish_state()

    def _end_step(self, step: dict[str, Any], status: str, message: str | None) -> None:
        self._record_sent_status(step)
        with self.cond:
            step.update(status=status, message=message, t_end_ms=self._ms(self.clock()))
        label = VERDICT_LABELS.get(status, status)
        self._log(f"  {step['label']}：{label}" + (f" — {message}" if message else ""))

    def _run_case(self, index: int, case: dict[str, Any]) -> str:
        result_case = self.result["cases"][index]
        with self.cond:
            result_case.update(verdict="running", started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                               t_start_ms=self._ms(self.clock()))
        self._log(f"用例 {result_case['number']}：{result_case['title']}")
        context: dict[str, Any] = {"anchor": None}
        try:
            for step_index, step in enumerate(case["steps"]):
                record = result_case["steps"][step_index]
                self._begin_step(record, result_case)
                try:
                    status, message = self._run_step(step, record, context)
                except StepError as exc:
                    status, message = "error", str(exc)
                except SkipCase:
                    self._end_step(record, "skipped", "操作员跳过本用例")
                    for rest in result_case["steps"][step_index + 1:]:
                        rest["status"] = "skipped"
                    break
                self._end_step(record, status, message)
                if status == "error":
                    for rest in result_case["steps"][step_index + 1:]:
                        rest["status"] = "not-run"
                    break
        except (Aborted, FatalRunError) as exc:
            with self.cond:
                for record in result_case["steps"]:
                    if record["status"] == "running":
                        record.update(status="aborted", t_end_ms=self._ms(self.clock()))
                    elif record["status"] == "pending":
                        record["status"] = "not-run"
                result_case.update(verdict="aborted" if isinstance(exc, Aborted) else "error",
                                   t_end_ms=self._ms(self.clock()))
                self.state["done"] = sum(c["verdict"] not in {"pending", "running"} for c in self.result["cases"])
                self._update_summary()
            self._save()
            raise
        with self.cond:
            if any(r["status"] == "skipped" and r.get("message") == "操作员跳过本用例" for r in result_case["steps"]):
                verdict = "skipped"
            else:
                verdict = case_verdict(result_case["steps"])
            result_case.update(verdict=verdict, t_end_ms=self._ms(self.clock()))
            self.state["done"] = index + 1
            self._update_summary()
        self._log(f"用例 {result_case['number']} 结果：{VERDICT_LABELS.get(verdict, verdict)}")
        self._save()
        self._publish({"type": "case", "case": self._case_brief(result_case)})
        self._publish_state()
        return verdict

    def _run_step(self, step: dict[str, Any], record: dict[str, Any], context: dict[str, Any]) -> tuple[str, str | None]:
        kind = step["type"]
        if kind == "write_attributes":
            verify = step.get("verify", True)
            for name, value in (step.get("attributes") or {}).items():
                cluster, attribute, data_type = ATTRIBUTES[name]
                value = norm_value(name, value)
                self._write(cluster, attribute, data_type, value, name, record, verify)
                self.known[name] = value
            for item in step.get("raw") or []:
                self._write(as_int(item["cluster"]), as_int(item["id"]), as_int(item["type"]), as_int(item["value"]),
                            f"0x{as_int(item['cluster']):04X}:0x{as_int(item['id']):04X}", record, verify)
            return "pass", None
        if kind == "read":
            values = {name: self._read(name, record)["value"] for name in step["attributes"]}
            bad = []
            for name, expected in (step.get("expect") or {}).items():
                tolerance = as_int(expected.get("tolerance", 0)) if isinstance(expected, dict) else 0
                value = norm_value(name, expected["value"] if isinstance(expected, dict) else expected)
                if not _within(values[name], value, tolerance):
                    bad.append(f"{name}={display_value(name, values[name])}（期望 {display_value(name, value)}）")
            shown = "，".join(f"{n}={display_value(n, v)}" for n, v in values.items())
            return ("fail", "不符合：" + "；".join(bad)) if bad else ("pass", shown)
        if kind == "set_state":
            return self._set_state(step, record)
        if kind == "command":
            lines, cluster, description = build_command(step, self._next_raw_seq)
            info = self._send(lines, cluster, record, description)
            context["anchor"] = context["last_command"] = info
            return "pass", f"已发送 {description}"
        if kind == "burst":
            return self._burst(step, record, context)
        if kind == "expect":
            return self._expect(step, record, context)
        if kind == "wait":
            if "ms" in step:
                self._sleep(as_int(step["ms"]) / 1000)
                return "pass", f"等待 {as_int(step['ms'])} ms"
            waited = self._wait_quiet(as_int(step["quiet_ms"]) / 1000, as_int(step.get("max_ms", 15000)) / 1000)
            return "pass", f"等待上报静默，用时 {waited} ms"
        if kind == "operator":
            shown = self.clock()
            with self.cond:
                mark = self.event_seq
            context["anchor"] = {"_t": shown, "_host_t": None, "_mark": mark, "t_ms": self._ms(shown),
                                 "description": "操作员动作", "cluster": None}
            context["last_command"] = None
            response = self._prompt("operator", step["prompt"], record, step.get("timeout_ms"))
            if response["action"] == "skip-case":
                raise SkipCase()
            record["operator"] = {"action": "continue", "t_ms": self._ms(response["t"])}
            return "pass", f"操作员在 +{self._ms(response['t']) - self._ms(shown)} ms 后确认继续"
        if kind == "confirm":
            if self.result["options"]["manual"] == "skip":
                record["operator"] = {"action": "skip", "comment": "运行设置：跳过人工确认"}
                return "skipped", "按运行设置跳过人工确认"
            response = self._prompt("confirm", step["prompt"], record, None)
            record["operator"] = {"action": response["action"], "comment": response["comment"],
                                  "t_ms": self._ms(response["t"])}
            status = {"pass": "pass", "fail": "fail", "skip": "skipped"}[response["action"]]
            return status, response["comment"] or None
        raise StepError(f"未知步骤 {kind}")

    def _prompt(self, kind: str, text: str, record: dict[str, Any], timeout_ms: Any) -> dict[str, Any]:
        prompt_id = uuid.uuid4().hex[:12]
        with self.cond:
            self.response = None
            self.prompt = {"id": prompt_id, "kind": kind, "text": text, "observe": record.get("observe"),
                           "case": (self.state.get("current") or {}).get("number"),
                           "case_title": (self.state.get("current") or {}).get("title"),
                           "step_label": record["label"], "shown_ms": self._ms(self.clock())}
            self.state["status_detail"] = "awaiting_operator" if kind == "operator" else "awaiting_confirm"
        self._publish_state()
        self._log(f"  等待{'操作员' if kind == 'operator' else '人工确认'}：{text}")
        deadline = self.clock() + as_int(timeout_ms) / 1000 if timeout_ms else None
        try:
            with self.cond:
                while self.response is None:
                    if self.abort_event.is_set():
                        raise Aborted()
                    if deadline is not None and self.clock() >= deadline:
                        raise StepError("操作员响应超时")
                    self.cond.wait(0.1)
                response = self.response
        finally:
            with self.cond:
                self.prompt = None
                self.response = None
                self.state.pop("status_detail", None)
            self._publish_state()
        return response

    def _latest_report_t(self, since_t: float) -> float | None:
        with self.cond:
            latest = None
            for record in reversed(self.events):
                if record["t"] < since_t:
                    break
                if record["kind"] == "rx" and (record.get("decoded") or {}).get("kind") == "report":
                    latest = record["t"]
                    break
        return latest

    def _wait_quiet(self, quiet: float, maximum: float, minimum: float = 0.0) -> int:
        start = self.clock()
        while True:
            now = self.clock()
            last = self._latest_report_t(start) or start
            if now - start >= maximum or (now - last >= quiet and now - start >= minimum):
                return int((now - start) * 1000)
            self._sleep(0.05)

    def _set_state(self, step: dict[str, Any], record: dict[str, Any]) -> tuple[str, str | None]:
        level = as_int(step["level"])
        on = bool(step["on"])
        self._send([f"zcl level-control o-mv-to-level {level} 0"], CLUSTER_LEVEL, record, f"设定亮度 {level}（立即开灯）")
        self._wait_quiet(0.8, 4.0, 0.3)
        if not on:
            self._send(["zcl on-off off"], CLUSTER_ON_OFF, record, "关灯")
            off_tenths = self.known.get("offTransitionTime")
            off_tenths = self.known.get("onOffTransitionTime", 10) if off_tenths in (None, 0xFFFF) else off_tenths
            minimum = (off_tenths or 0) / 10 + 0.5
            self._wait_quiet(as_int(step.get("settle_quiet_ms", 1200)) / 1000,
                             max(as_int(step.get("settle_max_ms", 15000)) / 1000, minimum + 1), minimum)
        actual_on = self._read("onOff", record)["value"]
        actual_level = self._read("currentLevel", record)["value"]
        problems = []
        if bool(actual_on) != on:
            problems.append(f"onOff={display_value('onOff', actual_on)}")
        if abs(actual_level - level) > 2:
            problems.append(f"currentLevel={actual_level}")
        state_text = f"{'开' if on else '关'}，亮度 {level}"
        if problems:
            return "precondition-failed", f"期望 {state_text}，读回 {'，'.join(problems)}"
        return "pass", f"已到达 {state_text}"

    def _burst(self, step: dict[str, Any], record: dict[str, Any], context: dict[str, Any]) -> tuple[str, str | None]:
        transport = step.get("transport", "auto")
        if transport == "firmware" or (transport == "auto" and self.burst_command):
            if not self.burst_command:
                raise StepError("需要第二阶段网关固件命令（精确帧间隔），当前网关不支持")
            raise StepError("固件 burst 通道尚未实现")  # pragma: no cover - phase 2
        gap = as_int(step.get("gap_ms", 0)) / 1000
        tolerance = as_int(step.get("gap_tolerance_ms", 15))
        infos: list[dict[str, Any]] = []
        overhead = 0.0
        for index, frame in enumerate(step["frames"]):
            if index:
                delay = infos[-1]["_t"] + gap - overhead - self.clock()
                if delay > 0:
                    self._sleep(delay)
            lines, cluster, description = build_command(frame, self._next_raw_seq)
            begun = self.clock()
            info = self._send(lines, cluster, record, description)
            if info["tx_seen"]:
                overhead = max(0.0, info["_t"] - begun)
            infos.append(info)
            if index == 0:
                context["anchor"] = info
        gaps = [round((b["_t"] - a["_t"]) * 1000) for a, b in zip(infos, infos[1:])]
        if all(i["_host_t"] is not None for i in infos):
            gaps = [round(b["_host_t"] - a["_host_t"]) for a, b in zip(infos, infos[1:])]
        record["gaps_ms"] = gaps
        record["requested_gap_ms"] = as_int(step.get("gap_ms", 0))
        context["last_command"] = infos[-1]
        text = f"请求间隔 {record['requested_gap_ms']} ms，实测 {', '.join(map(str, gaps))} ms（CLI 通道）"
        if any(g > record["requested_gap_ms"] + tolerance for g in gaps):
            return "inconclusive", f"CLI 时序无法达到 {record['requested_gap_ms']} ms 间隔；{text}"
        return "pass", text

    def _expect(self, step: dict[str, Any], record: dict[str, Any], context: dict[str, Any]) -> tuple[str, str | None]:
        anchor = context.get("anchor")
        start = self.clock()
        if anchor is None:
            with self.cond:
                mark = self.event_seq
            anchor = {"_t": start, "_host_t": None, "_mark": mark, "description": "步骤开始"}
        anchor_t = anchor["_t"]
        window = as_int(step.get("window_ms", 6000)) / 1000
        minimum = as_int(step.get("min_ms", 0)) / 1000
        quiet = step.get("quiet_ms", 1500)
        if any(m["kind"] == "no_report" and "within_ms" not in m for m in step["matchers"]):
            quiet = None
        base = max(anchor_t, start)
        while True:
            now = self.clock()
            if now - base >= window:
                break
            if quiet is not None:
                last = self._latest_report_t(anchor_t) or anchor_t
                if now - last >= as_int(quiet) / 1000 and now - anchor_t >= minimum:
                    break
            self._sleep(0.05)
        record["window_ms"] = int((self.clock() - anchor_t) * 1000)
        observations: list[dict[str, Any]] = []
        default_responses: list[dict[str, Any]] = []
        last_command = context.get("last_command") or anchor
        with self.cond:
            for event in self._events_after(anchor.get("_mark", 0)):
                if event["kind"] != "rx" or event["t"] < anchor_t or event.get("profile") == 0:
                    continue
                if event.get("ep") != self.target["endpoint"]:
                    continue
                decoded = event.get("decoded") or {}
                if decoded.get("kind") == "report":
                    for item in decoded.get("records", []):
                        observation = self._observation(event, item, "report")
                        if observation:
                            observations.append(observation)
                elif decoded.get("kind") == "default-response":
                    if (last_command.get("zcl_seq") is None or event.get("zcl_seq") == last_command.get("zcl_seq")) and (
                            last_command.get("command_id") is None or decoded["command_id"] == last_command.get("command_id")):
                        default_responses.append({"t_ms": self._ms(event["t"]), "status": decoded["status"],
                                                  "command_id": decoded["command_id"]})
        for name in step.get("read_back") or []:
            observations.append(self._read(name, record))
        use_host = anchor.get("_host_t") is not None and all(o["_host_t"] is not None for o in observations)
        relative = []
        for o in observations:
            rel = (o["_host_t"] - anchor["_host_t"]) if use_host else (o["_t"] - anchor_t) * 1000
            relative.append({"t": int(round(rel)), "attribute": o["attribute"], "value": o["value"], "source": o["source"]})
        record["time_source"] = "host" if use_host else "panel"
        record["anchor_ms"] = self._ms(anchor_t)
        record["observations"] = [dict({k: v for k, v in o.items() if not k.startswith("_")}, rel_ms=r["t"])
                                  for o, r in zip(observations, relative)][:STEP_OBSERVATION_LIMIT]
        record["default_responses"] = default_responses
        results = [evaluate_matcher(m, relative, {"default_responses": default_responses}) for m in step["matchers"]]
        record["matchers"] = results
        status = step_status_from_matchers(results)
        failed = [r["message"] for r in results if not r["ok"] and r["severity"] == "required"]
        info = [r["message"] for r in results if not r["ok"] and r["severity"] == "info"]
        summary = f"{len(relative)} 条观测；" + ("；".join(failed) if failed else "必需检查全部通过")
        if info:
            summary += f"（提示 {len(info)} 项）"
        return status, summary

    # -------------------------------------------------------- bookkeeping

    def _update_summary(self) -> None:
        # Caller holds self.cond.
        counts: dict[str, int] = {}
        for case in self.result["cases"]:
            counts[case["verdict"]] = counts.get(case["verdict"], 0) + 1
        self.result["summary"] = counts

    def _save(self) -> None:
        with self.cond:
            snapshot = _strip_private(copy.deepcopy(self.result))
        try:
            self.results.save(snapshot)
        except OSError:  # pragma: no cover - disk issues are reported in the log
            traceback.print_exc()

    def _log(self, text: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {text}"
        with self.cond:
            self.log_lines.append(line)
        self._publish({"type": "log", "line": line})

    def _publish(self, payload: dict[str, Any]) -> None:
        if self.publish_hook is None:
            return
        try:
            self.publish_hook(payload)
        except Exception:  # pragma: no cover - never let the UI break a run
            traceback.print_exc()

    def _publish_state(self) -> None:
        with self.cond:
            state = copy.deepcopy(self.state)
            state["prompt"] = copy.deepcopy(self.prompt)
            if self.result is not None:
                state["summary"] = dict(self.result.get("summary") or {})
                state["result_id"] = self.result["id"]
        self._publish({"type": "state", "state": state})


def _strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


def describe_step(step: dict[str, Any]) -> str:
    kind = step.get("type")
    if kind == "write_attributes":
        items = [f"{k}={v}" for k, v in (step.get("attributes") or {}).items()]
        return "写入属性 " + ", ".join(items or ["raw"])
    if kind == "read":
        return "读取 " + ", ".join(step.get("attributes") or [])
    if kind == "set_state":
        return f"前置状态：{'开' if step.get('on') else '关'}，亮度 {step.get('level')}"
    if kind == "command":
        try:
            return "发送 " + build_command(step, lambda: 0)[2]
        except ScriptError:
            return "发送命令"
    if kind == "burst":
        return f"连续发送 {len(step.get('frames') or [])} 帧，间隔 {step.get('gap_ms', 0)} ms"
    if kind == "expect":
        return f"检查上报（{len(step.get('matchers') or [])} 项）"
    if kind == "wait":
        return f"等待 {step['ms']} ms" if "ms" in step else "等待上报静默"
    if kind == "operator":
        return "操作员：" + str(step.get("prompt", ""))
    if kind == "confirm":
        return "人工确认：" + str(step.get("prompt", ""))
    return str(kind)


# ---------------------------------------------------------------- report

def _order_key(case: dict[str, Any]) -> tuple[Any, ...]:
    order = case.get("order")
    return (0, float(order)) if isinstance(order, (int, float)) else (1, str(case.get("number") or case.get("id")))


def render_html_report(result: dict[str, Any]) -> str:
    esc = lambda value: html.escape("" if value is None else str(value))  # noqa: E731
    cases = sorted(result.get("cases", []), key=_order_key)
    summary = result.get("summary") or {}
    device = result.get("device") or {}
    script = result.get("script") or {}
    options = result.get("options") or {}
    chips = "".join(f'<span class="chip v-{esc(k)}">{esc(VERDICT_LABELS.get(k, k))} {esc(v)}</span>'
                    for k, v in sorted(summary.items(), key=lambda kv: -VERDICT_RANK.get(kv[0], 0)))
    rows = "".join(
        f'<tr><td>{esc(c.get("number"))}</td><td>{esc(c.get("title"))}</td>'
        f'<td><span class="chip v-{esc(c.get("verdict"))}">{esc(VERDICT_LABELS.get(c.get("verdict"), c.get("verdict")))}</span></td>'
        f'<td>{esc(_case_problem(c))}</td></tr>' for c in cases)
    details = []
    for case in [{"number": "初始化", "title": "绑定与上报配置", "verdict": (result.get("setup") or {}).get("status"),
                  "steps": (result.get("setup") or {}).get("steps", [])}] + cases:
        if not case.get("steps"):
            continue
        step_rows = []
        for step in case["steps"]:
            extra = []
            if step.get("gaps_ms"):
                extra.append(f"帧间隔 {esc(step['gaps_ms'])} ms（请求 {esc(step.get('requested_gap_ms'))} ms）")
            for matcher in step.get("matchers") or []:
                mark = "✔" if matcher["ok"] else ("✖" if matcher["severity"] == "required" else "△")
                extra.append(f'<span class="m-{"ok" if matcher["ok"] else matcher["severity"]}">{mark} {esc(matcher["message"])}</span>')
            observations = step.get("observations") or []
            if observations:
                text = " · ".join(f'+{o.get("rel_ms", o.get("t_ms"))}ms {o["attribute"]}={display_value(o["attribute"], o["value"])}'
                                  f'{"(读)" if o["source"] == "read" else ""}' for o in observations[:80])
                extra.append(f'<span class="obs">{esc(text)}</span>')
            operator = step.get("operator")
            if operator:
                extra.append(f'操作员：{esc(operator.get("action"))} {esc(operator.get("comment") or "")}')
            step_rows.append(
                f'<tr><td>{esc(step.get("t_start_ms"))}</td><td>{esc(step.get("label"))}'
                f'{"<br><small>" + esc(step.get("observe")) + "</small>" if step.get("observe") else ""}</td>'
                f'<td><span class="chip v-{esc(step.get("status"))}">{esc(VERDICT_LABELS.get(step.get("status"), step.get("status")))}</span></td>'
                f'<td>{esc(step.get("message") or "")}{"<br>" if extra else ""}{"<br>".join(extra)}</td></tr>')
        details.append(
            f'<section><h3>{esc(case.get("number"))} · {esc(case.get("title"))} '
            f'<span class="chip v-{esc(case.get("verdict"))}">{esc(VERDICT_LABELS.get(case.get("verdict"), case.get("verdict")))}</span></h3>'
            f'{"<p class=notes>" + esc(case.get("notes")) + "</p>" if case.get("notes") else ""}'
            f'<table><thead><tr><th>t (ms)</th><th>步骤</th><th>状态</th><th>详情</th></tr></thead>'
            f'<tbody>{"".join(step_rows)}</tbody></table></section>')
    meta = [("脚本", f'{script.get("title")} ({script.get("id")})'), ("来源", script.get("source")),
            ("设备", f'{device.get("name") or ""} {device.get("manufacturer") or ""} {device.get("model") or ""}'),
            ("IEEE / 短地址 / 端点", f'{device.get("eui64")} / {device.get("nodeId")} / {device.get("endpoint")}'),
            ("软件版本 / 日期代码", f'{device.get("swBuildId") or "-"} / {device.get("dateCode") or "-"}'),
            ("开始 / 结束", f'{result.get("started_at")} / {result.get("finished_at") or "-"}'),
            ("运行状态", f'{RUN_STATUS_LABELS.get(result.get("status"), result.get("status"))} {result.get("message") or ""}'),
            ("运行选项", f'绑定/上报配置：{"是" if options.get("setup") else "跳过"}；人工确认：'
                         f'{"询问" if options.get("manual") == "ask" else "跳过"}；时间来源：面板时钟（含 PTY 延迟）')]
    meta_html = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>测试报告 {esc(result.get("id"))}</title>
<style>
:root{{--bg:#f6f7fb;--panel:#fff;--text:#1f2937;--muted:#6b7280;--line:#e5e7eb;}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif;}}
main{{max-width:1180px;margin:0 auto;padding:24px 16px 48px;}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 10px}} h3{{font-size:15px;margin:0 0 8px}}
.card,section{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:14px;overflow-x:auto}}
dl{{display:grid;grid-template-columns:minmax(120px,max-content) 1fr;gap:6px 16px;margin:0}} dt{{color:var(--muted)}} dd{{margin:0;overflow-wrap:anywhere}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{text-align:left;vertical-align:top;padding:7px 8px;border-top:1px solid var(--line)}}
th{{color:var(--muted);font-weight:600;border-top:0}} td:first-child{{white-space:nowrap}}
.chip{{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;background:#eef2f7;color:#475569;white-space:nowrap}}
.v-pass{{background:#e7f6ec;color:#166534}} .v-fail,.v-error{{background:#fdecec;color:#b42318}}
.v-inconclusive,.v-warn,.v-precondition-failed{{background:#fff4e0;color:#9a5b00}} .v-skipped,.v-not-run,.v-aborted{{background:#eef2f7;color:#64748b}}
.m-ok{{color:#166534}} .m-required{{color:#b42318}} .m-info{{color:#9a5b00}} .obs{{color:var(--muted);font-family:ui-monospace,Consolas,monospace;font-size:12px}}
.notes{{color:var(--muted);margin:0 0 8px}} .summary{{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}}
@media print{{body{{background:#fff}} .card,section{{break-inside:avoid}}}}
</style></head><body><main>
<h1>Zigbee 设备自动测试报告</h1><p style="color:var(--muted);margin:0">{esc(result.get("id"))}</p>
<div class="card"><dl>{meta_html}</dl><div class="summary">{chips}</div></div>
<h2>用例结果（按客户编号排序）</h2>
<div class="card"><table><thead><tr><th>编号</th><th>用例</th><th>结果</th><th>主要问题</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>步骤详情</h2>{"".join(details)}
<p style="color:var(--muted);margin-top:24px">时间为相对测试开始的毫秒数，取自面板时钟，包含 PTY 与调度延迟。CLI 通道无法产生 20–40 ms 级帧间隔；本网关上的通过不代表与 Hue / Hubitat 的互通性。</p>
</main></body></html>"""


def _case_problem(case: dict[str, Any]) -> str:
    for step in case.get("steps", []):
        if step.get("status") in {"fail", "error", "inconclusive", "precondition-failed"}:
            return f"{step.get('label')}：{step.get('message') or ''}"
    return ""
