"""Explicit, bounded JSON provider boundary. Import and inspect never start a client.

The enclosing service owns the durable request ledger and exclusive endpoint lease.
This module never retries a request or translates it into desktop input itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PROTOCOL = "mcdevlab.bridge/1"
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 262144
MAX_PIN_BYTES = 128 * 1024 * 1024
MAX_STDERR_BYTES = 8192
WITNESS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["session_id", "world_id", "frame_id", "observed_at_ms"],
    "properties": {
        "session_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "world_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "frame_id": {"type": "integer", "minimum": 0},
        "observed_at_ms": {"type": "integer", "minimum": 1},
    },
}
REQUEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["request_id", "operation"],
    "properties": {
        "request_id": {"type": "string", "format": "uuid"},
        "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_.-]{0,63}$"},
        "arguments": {"type": "object"}, "expected": WITNESS_SCHEMA,
    },
}


class BridgeRefusal(ValueError):
    """A constant code only; never include raw provider output or credentials."""


def _require(condition, code):
    if not condition:
        raise BridgeRefusal(code)


def _pairs(items):
    out = {}
    for key, value in items:
        _require(key not in out, "DUPLICATE_JSON_KEY")
        out[key] = value
    return out


def _bad_number(_value):
    raise BridgeRefusal("NONFINITE_JSON")


def _json_bytes(value):
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise BridgeRefusal("INVALID_JSON") from error


def _platform():
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


def _pin(path, expected):
    _require(isinstance(path, str) and Path(path).is_absolute(), "PIN_PATH_REQUIRED")
    _require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected), "PIN_DIGEST_REQUIRED")
    p = Path(path)
    _require(p.is_file() and not p.is_symlink(), "PIN_FILE_UNAVAILABLE")
    before = p.stat()
    _require(before.st_size <= MAX_PIN_BYTES, "PIN_FILE_TOO_LARGE")
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        remaining = MAX_PIN_BYTES + 1
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                break
            remaining -= len(block)
            digest.update(block)
    after = p.stat()
    _require(remaining > 0 and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "PIN_FILE_CHANGED")
    _require(digest.hexdigest() == expected, "PROVIDER_PIN_MISMATCH")
    return str(p.resolve())


def _configuration(config):
    _require(isinstance(config, dict), "CONFIG_OBJECT_REQUIRED")
    if config.get("kind") == "atlas-controller":
        raise BridgeRefusal("ATLAS_CONTROLLER_ADAPTER_NOT_IMPLEMENTED")
    _require(config.get("kind") == "json-stdio", "UNSUPPORTED_BRIDGE_KIND")
    platforms = config.get("platforms")
    _require(isinstance(platforms, list) and 1 <= len(platforms) <= 3 and
             all(p in {"linux", "macos", "windows"} for p in platforms), "PLATFORMS_REQUIRED")
    _require(_platform() in platforms, "UNSUPPORTED_PLATFORM")
    argv = config.get("argv")
    _require(isinstance(argv, list) and 1 <= len(argv) <= 32 and all(isinstance(a, str) and "\0" not in a and len(a) <= 4096 for a in argv), "PROVIDER_ARGV_REQUIRED")
    _require(Path(argv[0]).is_absolute() and Path(argv[0]).is_file(), "ABSOLUTE_EXECUTABLE_REQUIRED")
    cwd = config.get("cwd")
    _require(isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir(), "ABSOLUTE_CWD_REQUIRED")
    pins = config.get("pins")
    _require(isinstance(pins, list) and 1 <= len(pins) <= 64, "PROVIDER_PINS_REQUIRED")
    checked = set()
    for pin in pins:
        _require(isinstance(pin, dict) and set(pin) == {"path", "sha256"}, "INVALID_PROVIDER_PIN")
        checked.add(_pin(pin["path"], pin["sha256"]))
    _require(str(Path(argv[0]).resolve()) in checked, "EXECUTABLE_PIN_REQUIRED")
    # A directly named script cannot hide behind an interpreter-only pin.
    for arg in argv[1:]:
        candidate = Path(arg) if Path(arg).is_absolute() else Path(cwd) / arg
        if candidate.is_file():
            _require(str(candidate.resolve()) in checked, "ARGUMENT_FILE_PIN_REQUIRED")
    manifest = config.get("manifest")
    _require(isinstance(manifest, dict) and set(manifest) == {"protocol", "provider_id", "operations"}, "INVALID_CAPABILITY_MANIFEST")
    _require(manifest.get("protocol") == PROTOCOL and _text(manifest.get("provider_id")), "UNSUPPORTED_PROVIDER_PROTOCOL")
    operations = manifest.get("operations")
    _require(isinstance(operations, dict) and 1 <= len(operations) <= 64, "OPERATIONS_REQUIRED")
    for name, spec in operations.items():
        _require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name), "INVALID_OPERATION")
        _require(isinstance(spec, dict) and set(spec) <= {"effect", "description"} and spec.get("effect") in {"observe", "action"}, "INVALID_OPERATION_CONTRACT")
        _require("description" not in spec or isinstance(spec["description"], str) and len(spec["description"]) <= 512, "INVALID_OPERATION_CONTRACT")
    _require(operations.get("status", {}).get("effect") == "observe", "STATUS_CAPABILITY_REQUIRED")
    timeout = config.get("timeout_seconds", 10)
    _require(type(timeout) in (int, float) and math.isfinite(timeout) and 0.1 <= timeout <= 30, "INVALID_PROVIDER_TIMEOUT")
    limit = config.get("output_limit_bytes", 65536)
    _require(type(limit) is int and 1024 <= limit <= MAX_RESPONSE_BYTES, "INVALID_OUTPUT_LIMIT")
    freshness = config.get("freshness_ms", 5000)
    _require(type(freshness) is int and 100 <= freshness <= 10000, "INVALID_FRESHNESS")
    return manifest, float(timeout), limit, freshness


def inspect_bridge(config: dict) -> dict:
    """Verify local configuration and code pins; do not execute the provider.

    Availability means configured dispatch capability, never a live-client proof.
    """
    try:
        manifest, timeout, limit, freshness = _configuration(config)
        return {"available": True, "reason": "CONFIGURED_NOT_LIVE_VERIFIED", "protocol": PROTOCOL,
                "provider_id": manifest["provider_id"], "operations": manifest["operations"],
                "platform": _platform(), "pins_verified": True, "live_verified": False,
                "timeout_seconds": timeout, "output_limit_bytes": limit, "freshness_ms": freshness}
    except (BridgeRefusal, OSError, ValueError, TypeError) as error:
        return {"available": False, "reason": str(error) if isinstance(error, BridgeRefusal) else "PROVIDER_CONFIGURATION_UNAVAILABLE",
                "platform": _platform(), "pins_verified": False, "live_verified": False, "operations": {}}


def _witness(value, now, freshness, *, world_required=False):
    _require(isinstance(value, dict) and set(value) == {"session_id", "world_id", "frame_id", "observed_at_ms"}, "WITNESS_REQUIRED")
    _require(_text(value["session_id"]), "SESSION_WITNESS_REQUIRED")
    _require(_text(value["world_id"]) or value["world_id"] is None and not world_required, "WORLD_WITNESS_REQUIRED")
    _require(type(value["frame_id"]) is int and 0 <= value["frame_id"] < 2**63, "FRAME_WITNESS_REQUIRED")
    _require(type(value["observed_at_ms"]) is int and now - freshness <= value["observed_at_ms"] <= now + 1000, "STALE_WITNESS")
    return value


def _exchange(config, payload, timeout, limit):
    from .common import atomic_json
    from .node import own_command
    from .processes import OwnedProcess

    with tempfile.TemporaryDirectory(prefix="mcdev-bridge-") as temporary:
        state = Path(temporary)
        directory = state / "bridge"
        directory.mkdir(mode=0o700)
        token = uuid.uuid4().hex
        atomic_json(directory / "exec.json", {"argv": config["argv"], "cwd": config["cwd"], "gate": token})
        shim_cwd = str(Path(__file__).resolve().parent.parent) if Path(sys.argv[0]).suffix != ".pyz" else config["cwd"]
        try:
            owner = OwnedProcess(own_command("exec", "--state", str(state), "--job", "bridge"),
                                 subprocess.PIPE, cwd=shim_cwd,
                                 stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            # The exec shim cannot spawn the provider until the gate below exists.
            return b"", "PROVIDER_START_FAILED", False
        try:
            atomic_json(directory / "gate.json", {"token": token})
            return (*_communicate(owner, payload, timeout, limit), True)
        except (OSError, RuntimeError, ValueError):
            # Gate publication may already have become visible: effect unknown.
            return b"", "PROVIDER_IO_FAILED", True
        finally:
            owner.close()


def _communicate(owner, payload, timeout, limit):
    process = owner.process
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    failures = threading.Event()

    def drain(index, stream, bound):
        try:
            while True:
                block = stream.read(4096)
                if not block:
                    return
                if len(buffers[index]) + len(block) > bound:
                    overflow.set()
                    return
                buffers[index].extend(block)
        except OSError:
            failures.set()
        finally:
            stream.close()

    def feed():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            failures.set()
        finally:
            process.stdin.close()

    threads = [threading.Thread(target=drain, args=(0, process.stdout, limit), daemon=True),
               threading.Thread(target=drain, args=(1, process.stderr, MAX_STDERR_BYTES), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    code = None
    try:
        while process.poll() is None or any(t.is_alive() for t in threads):
            if overflow.is_set():
                code = "PROVIDER_OUTPUT_LIMIT"
                break
            if failures.is_set():
                code = "PROVIDER_IO_FAILED"
                break
            if time.monotonic() >= deadline:
                code = "PROVIDER_TIMEOUT"
                break
            time.sleep(0.005)
        if overflow.is_set():
            code = "PROVIDER_OUTPUT_LIMIT"
        elif failures.is_set():
            code = code or "PROVIDER_IO_FAILED"
        elif process.poll() not in (None, 0):
            code = code or "PROVIDER_EXIT_FAILED"
    finally:
        # Descendants retaining pipes cannot extend the request or survive cleanup.
        owner.stop()
        for thread in threads:
            thread.join(timeout=0.5)
    if any(t.is_alive() for t in threads):
        code = "PROVIDER_CLEANUP_INCOMPLETE"
    return bytes(buffers[0]), code


def invoke_bridge(config: dict, request: dict) -> dict:
    """Invoke one configured operation. All post-dispatch action errors are unknown.

    Caller must persist intent and own an endpoint lease BEFORE calling. Never retry
    unknown results. A provider must validate expected witnesses before any effect.
    """
    dispatched = False
    action = False
    request_id = request.get("request_id") if isinstance(request, dict) else None
    operation = request.get("operation") if isinstance(request, dict) else None
    try:
        manifest, timeout, limit, freshness = _configuration(config)
        _require(isinstance(request, dict) and set(request) <= {"request_id", "operation", "arguments", "expected"}, "INVALID_REQUEST_FIELDS")
        _require(isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id, "CANONICAL_REQUEST_UUID_REQUIRED")
        _require(isinstance(operation, str) and operation in manifest["operations"], "UNSUPPORTED_OPERATION")
        action = manifest["operations"][operation]["effect"] == "action"
        arguments = request.get("arguments", {})
        _require(isinstance(arguments, dict), "ARGUMENTS_OBJECT_REQUIRED")
        now = time.time_ns() // 1_000_000
        expected = request.get("expected")
        if action:
            _witness(expected, now, freshness, world_required=True)
        elif expected is not None:
            _witness(expected, now, freshness)
        envelope = {"protocol": PROTOCOL, "provider_id": manifest["provider_id"], "request_id": request_id,
                    "operation": operation, "arguments": arguments, "expected": expected,
                    "issued_at_ms": now, "deadline_ms": now + int(timeout * 1000)}
        payload = _json_bytes(envelope) + b"\n"
        _require(len(payload) <= MAX_REQUEST_BYTES, "REQUEST_TOO_LARGE")
        # Constructor failure precedes the private child gate; later errors are unknown.
        dispatched = True
        try:
            raw, code, dispatched = _exchange(config, payload, timeout, limit)
        except OSError as error:
            raise BridgeRefusal("PROVIDER_CLEANUP_INCOMPLETE") from error
        _require(code is None, code or "PROVIDER_IO_FAILED")
        try:
            response = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs, parse_constant=_bad_number)
        except (UnicodeError, ValueError, RecursionError) as error:
            if isinstance(error, BridgeRefusal):
                raise
            raise BridgeRefusal("MALFORMED_PROVIDER_RESPONSE") from error
        _require(isinstance(response, dict) and set(response) == {"protocol", "provider_id", "request_id", "operation", "status", "code", "before", "after", "result"}, "INVALID_RESPONSE_FIELDS")
        for field in ("protocol", "provider_id", "request_id", "operation"):
            _require(response[field] == envelope[field], "RESPONSE_IDENTITY_MISMATCH")
        _require(response["status"] in {"ok", "rejected"}, "INVALID_PROVIDER_STATUS")
        _require(isinstance(response["code"], str) and re.fullmatch(r"[A-Z0-9_]{1,80}", response["code"]), "INVALID_PROVIDER_CODE")
        _require(isinstance(response["result"], dict), "RESULT_OBJECT_REQUIRED")
        after_now = time.time_ns() // 1_000_000
        # Before may be a full request duration older; after must be fresh now.
        before = _witness(response["before"], after_now, freshness + int(timeout * 1000), world_required=action)
        after = _witness(response["after"], after_now, freshness, world_required=action)
        _require(before["observed_at_ms"] >= now - 1000 and after["observed_at_ms"] >= before["observed_at_ms"], "WITNESS_ORDER_MISMATCH")
        _require(before["session_id"] == after["session_id"] and before["world_id"] == after["world_id"], "SESSION_OR_WORLD_CHANGED")
        _require(after["frame_id"] >= before["frame_id"], "FRAME_REGRESSION")
        if expected is not None:
            _require(all(before[k] == expected[k] for k in ("session_id", "world_id")) and before["frame_id"] >= expected["frame_id"], "EXPECTED_WITNESS_MISMATCH")
        if action and response["status"] == "ok":
            _require(after["frame_id"] > before["frame_id"], "COMPLETED_FRAME_REQUIRED")
        # Catch replacement while the provider was running; this cannot undo effects.
        _configuration(config)
        return {"status": response["status"], "code": response["code"], "dispatched": True,
                "request_id": request_id, "operation": operation,
                "outcome": "refused" if response["status"] == "rejected" else "verified" if action else "observed",
                "before": before, "after": after, "result": response["result"]}
    except (BridgeRefusal, ValueError, TypeError, OSError) as error:
        code = str(error) if isinstance(error, BridgeRefusal) else "INVALID_BRIDGE_REQUEST"
        unknown = dispatched and action
        return {"status": "unknown" if unknown else "unavailable", "code": code, "dispatched": dispatched,
                "request_id": request_id, "operation": operation,
                "outcome": "unknown" if unknown else "not_dispatched" if not dispatched else "unverified"}
