"""Run model constants and small helpers shared by parsers, collector, server and compare.

One internal run model (run.json) for every source:

  id, host, gate, recipe, source, kind, request_id, job_id,
  candidate {label, commit, jar_sha256, mod_id},
  verdict (canonical), verdict_raw, reason,
  started_ts, finished_ts, duration_s,
  environment {...}, metrics [{name, segment, unit, value, better}],
  checks [{name, status, detail}], steps [...], series {...},
  screenshots [{name, thumb, full, width, height, step}],
  log {path, lines, bytes}, crash {...}, timeline [...], evidence {...}
"""
from __future__ import annotations

import datetime as _dt
import re

RUN_SCHEMA = "mcdevlab.viewer.run.v1"

# Canonical verdicts. Everything the sources say is mapped onto these; the raw word is kept.
VERDICTS = ("PASS", "FAIL", "ERROR", "TIMEOUT", "INCOMPLETE", "BLOCKED", "CANCELLED", "RUNNING", "UNKNOWN")
BAD_VERDICTS = {"FAIL", "ERROR", "TIMEOUT"}

_VERDICT_MAP = {
    "PASS": "PASS", "PASSED": "PASS", "OK": "PASS", "DRYRUN_OK": "PASS", "ACCEPTED_SUMMARY_ONLY": "PASS",
    "ACCEPTED": "PASS", "INSTALLED": "PASS", "COMPLETE": "PASS", "SUCCESS": "PASS",
    "FAIL": "FAIL", "FAILED": "FAIL", "REJECTED": "FAIL",
    "ERROR": "ERROR", "REFUSED_OR_FAILED": "ERROR", "CRASH": "ERROR", "CRASHED": "ERROR",
    "TIMEOUT": "TIMEOUT", "TIMED_OUT": "TIMEOUT",
    "INCOMPLETE": "INCOMPLETE", "PARTIAL": "INCOMPLETE",
    "BLOCKED": "BLOCKED", "REFUSED": "BLOCKED", "NOT_VALIDATED": "BLOCKED", "SKIPPED": "BLOCKED",
    "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
    "RUNNING": "RUNNING", "QUEUED": "RUNNING",
    "UNKNOWN": "UNKNOWN",
}


def canon_verdict(raw) -> str:
    if raw is None:
        return "UNKNOWN"
    key = str(raw).strip().upper().replace(" ", "_")
    return _VERDICT_MAP.get(key, "UNKNOWN")


# Hosts. Aliases written by the various tools map to the fleet host id.
HOST_ALIASES = {
    "ctl": "ctl", "proxmox": "ctl", "orbital-pve": "ctl",
    "proxmox-test": "proxmox-test", "ct224": "proxmox-test", "mc-client-test": "proxmox-test",
    "amd": "amd", "fd": "amd", "orbital-fd": "amd",
    "gpc": "gpc",
    "mac": "mac", "orbital-mac": "mac",
}


def canon_host(raw, default=None):
    if raw is None:
        return default
    return HOST_ALIASES.get(str(raw).strip().lower(), str(raw).strip().lower() or default)


def utc_iso(ts):
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def parse_iso(s):
    """ISO-8601 (Z or offset or naive-as-UTC, 7 digit fractions ok) to epoch seconds, or None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = _ISO_RE.match(str(s).strip())
    if not m:
        return None
    y, mo, d, h, mi, se, frac, tz = m.groups()
    t = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), tzinfo=_dt.timezone.utc).timestamp()
    if frac:
        t += float("0." + frac)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        t -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return t


def stamp_to_ts(stamp):
    """20261005T065523Z -> epoch seconds."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?", stamp or "")
    if not m:
        return None
    return _dt.datetime(*[int(x) for x in m.groups()], tzinfo=_dt.timezone.utc).timestamp()


def slug(text, maxlen=64):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-.")
    return (s or "x")[:maxlen]


def metric_key(m):
    return (m.get("segment") + ":" if m.get("segment") else "") + m["name"]


def new_run(host, gate, source, stamp):
    return {
        "schema": RUN_SCHEMA,
        "id": "%s--%s--%s" % (slug(host), slug(gate), slug(stamp)),
        "host": host, "gate": gate, "recipe": None, "source": source, "kind": "client",
        "request_id": None, "job_id": None,
        "candidate": {"label": None, "commit": None, "jar_sha256": None, "mod_id": None},
        "verdict": "UNKNOWN", "verdict_raw": None, "reason": None,
        "started_ts": None, "finished_ts": None, "duration_s": None,
        "environment": {}, "metrics": [], "checks": [], "steps": [], "series": {},
        "screenshots": [], "log": None, "crash": None, "timeline": [], "log_markers": [],
        "exit": None, "evidence": {"files": []}, "job": None, "notes": [],
    }
