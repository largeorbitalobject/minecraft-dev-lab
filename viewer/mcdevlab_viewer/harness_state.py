"""Read host-local large-runner receipts; never contact or control a client.

RUNNING means a recently mirrored controller observation, not a fresh process probe.
The wrapper refreshes its local mirror every 20 seconds. Missing/stale observations
cannot establish that a remote game exited, so they must not advertise availability.
"""
import json
import os
import re
import stat
import time

from .model import parse_iso

MAX_BYTES = 128 * 1024
FRESH_SECONDS = 90


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise ValueError("invalid receipt size/type")
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("receipt changed while reading")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("receipt is not an object")
    return obj, after.st_mtime


def observe(root, host, now=None):
    if not root:
        return None
    now = time.time() if now is None else now
    pattern = re.compile(r"^\d{8}T\d{6}Z-(.+-" + re.escape(host) + r")-[A-Za-z0-9._-]+$")
    try:
        with os.scandir(root) as entries:
            names = [e.name for e in entries if pattern.fullmatch(e.name) and e.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return None
    except OSError:
        return {"state": "stale", "reason": "Controller archive unavailable"}
    if not names:
        return None
    name = max(names)  # Producer run IDs begin with UTC launch time.
    path = os.path.join(root, name, "controller-status.json")
    job = {"id": name, "request_id": None, "recipe": pattern.fullmatch(name).group(1),
           "source": "large-harness-controller", "started": None, "heartbeat": None}
    result = {"state": "stale", "job": job, "receipt": path,
              "reason": "Controller observation unavailable; remote exit unconfirmed"}
    try:
        obj, modified = _read(path)
        if obj.get("host") != host or obj.get("run") != name:
            raise ValueError("controller identity mismatch")
        status = obj["status"]
        job.update(state=status, started=parse_iso(obj.get("started_utc")), heartbeat=modified)
        result["status"] = status
        result["reason"] = str(obj.get("reason") or status)[:300]
        if status in ("PASS", "FAIL"):
            if obj.get("exit_confirmed") is True:
                result["state"] = "idle"
            else:
                result["reason"] = "Terminal receipt lacks confirmed exit"
        elif status == "CLEANUP_PENDING":
            result["reason"] = "Cleanup pending; remote game may still be running"
        elif not -5 <= now - modified <= FRESH_SECONDS:
            result["reason"] = "Controller observation older than 90 seconds or clock invalid; remote exit unconfirmed"
        elif status == "RUNNING" and job["started"] is not None and job["started"] <= now + 5:
            result["state"] = "running"
        elif status in ("PREFLIGHT", "STARTING", "LAUNCH_INTENT"):
            result["state"] = "running"
        elif status == "PAUSED":
            result["state"] = "paused"
        elif status == "REFUSED":
            result["state"] = "blocked"
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        pass
    return result
