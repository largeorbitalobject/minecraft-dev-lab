"""Parsers for every evidence format the fleet produces. Pure functions, stdlib only.

Formats:
  * feature-plan result.txt          (PASS/INCOMPLETE first line, `key=value` tokens)
  * feature-plan progress.txt        (phase timeline)
  * harness result.json + progress.jsonl   (HARNESS-RESULT-SCHEMA.md, mcdevlab.harness.result.v1)
  * Dev Lab smoke summaries          (ct224 smoke.v2, fd smoke v1, gpc smoke v1, mac dry-run)
  * c39 validations                  (Mac frame metrics, mcdevlab.c39-validation.v1)
  * Dev Lab jobs                     (mcdev.run.v1 receipts from run_list)
"""
from __future__ import annotations

import json
import math
import re

from .model import canon_verdict, parse_iso, stamp_to_ts

# ----------------------------------------------------------------------------------------------
# metric semantics
# ----------------------------------------------------------------------------------------------

_CONST_TOKENS = {"gate", "expected", "limit", "threshold", "budget"}
_HIGHER_TOKENS = {"fps", "throughput", "hits"}
_LOWER_TOKENS = {"ns", "ms", "us", "s", "seconds", "sec", "latency", "time", "duration", "elapsed", "p50", "p90",
                 "p95", "p99", "p999", "max", "hitch", "hitches", "pause", "dropped", "error", "errors", "fail",
                 "failed", "threw", "stall", "stalls", "miss", "misses", "leak", "heap", "wait", "lag"}


def guess_better(name: str):
    """'lower' | 'higher' | None (neutral/descriptive). Used for sources that do not declare it.

    Token based so `items` or `seems` never read as a time unit. Constants (gate, expected, limit) and
    plain counts/sizes are neutral: shown, never coloured as better or worse.
    """
    tokens = set(re.split(r"[_.]+", name.lower()))
    if tokens & _CONST_TOKENS:
        return None
    if tokens & _HIGHER_TOKENS:
        return "higher"
    if tokens & _LOWER_TOKENS:
        return "lower"
    return None


def guess_unit(name: str) -> str:
    tokens = re.split(r"[_.]+", name.lower())
    for t in reversed(tokens):
        if t in ("ns", "us", "ms", "mb"):
            return {"mb": "MB"}.get(t, t)
        if t in ("s", "sec", "seconds"):
            return "s"
        if t == "bytes":
            return "B"
        if t == "fps":
            return "fps"
    return ""


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def to_number(text):
    if text is None:
        return None
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text) if math.isfinite(float(text)) else None
    if _NUM_RE.match(str(text)):
        v = float(text)
        return int(v) if v.is_integer() and "." not in str(text) and "e" not in str(text).lower() else v
    return None


# ----------------------------------------------------------------------------------------------
# feature-plan result.txt / progress.txt
# ----------------------------------------------------------------------------------------------

_STATUS_RE = re.compile(r"^(PASS|FAIL|INCOMPLETE|LIMIT|QA|SKIP|WARN|INFO)\b\s*(.*)$")
_KV_RE = re.compile(r"(?<![\w>=<])([A-Za-z_][\w]*)=(\S*)")
# gate thresholds that apply to a measurement in the same line
_LIMIT_KEYS = ("overlay_p95_ns", "max_submission_frame_ns")  # keys that gate_ns actually limits


def parse_featureplan_result(text: str):
    """Parse a feature-plan result.txt (or INCOMPLETE.txt / HARNESS_INCOMPLETE.txt).

    Returns dict: verdict_raw, verdict, reason, checks[], metrics[], renderer, gl_version, route, gates,
    limits[] (LIMIT lines: honest scope notes), header (first line).
    """
    out = {"verdict_raw": None, "verdict": "UNKNOWN", "reason": None, "checks": [], "metrics": [],
           "renderer": None, "gl_version": None, "route": None, "gates": None, "limits": [], "header": None,
           "qa": []}
    if not text:
        return out
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return out
    first = lines[0].strip()
    out["header"] = first
    m = _STATUS_RE.match(first)
    if m and m.group(1) in ("PASS", "FAIL", "INCOMPLETE"):
        out["verdict_raw"] = m.group(1)
        out["verdict"] = canon_verdict(m.group(1))
        rest = m.group(2)
        if ":" in rest:
            out["reason"] = rest.split(":", 1)[1].strip() or None
        body = lines[1:]
    else:
        # a bare progress/incomplete note with no status header
        body = lines
        out["verdict_raw"] = None
    seen_metric = {}
    for ln in body:
        s = ln.strip()
        if s.startswith("route="):
            out["route"] = s[len("route="):]
            continue
        if s.startswith("gates="):
            out["gates"] = s[len("gates="):]
            continue
        if s.startswith("renderer="):
            mm = re.match(r"^renderer=(.*?)\s+version=(.*)$", s)
            if mm:
                out["renderer"], out["gl_version"] = mm.group(1).strip(), mm.group(2).strip()
            else:
                out["renderer"] = s[len("renderer="):].strip()
            continue
        m = _STATUS_RE.match(s)
        if not m:
            continue
        status, rest = m.group(1), m.group(2)
        if status == "LIMIT":
            name, _, detail = rest.partition(":")
            out["limits"].append({"name": name.strip(), "detail": detail.strip() or rest})
            out["checks"].append({"name": name.strip(), "status": "LIMIT", "detail": detail.strip()})
            continue
        if status == "QA":
            out["qa"].append(rest)
            continue
        parts = rest.split(None, 1)
        if not parts:
            continue
        name = parts[0].rstrip(":")
        tail = parts[1] if len(parts) > 1 else ""
        kvs = _KV_RE.findall(tail)
        detail = tail
        check = {"name": name, "status": status, "detail": detail}
        out["checks"].append(check)
        gate = None
        nums = {}
        for k, v in kvs:
            n = to_number(v)
            if n is not None:
                nums[k] = n
        if "gate_ns" in nums:
            gate = nums["gate_ns"]
        for k, n in nums.items():
            mname = "%s.%s" % (name, k)
            metric = {"name": mname, "segment": None, "unit": guess_unit(k), "value": n,
                      "better": guess_better(k), "check": name}
            if k in _LIMIT_KEYS and gate is not None:
                metric["limit"] = gate
            if k in ("gate_ns",) or k.startswith("expected"):
                metric["better"] = None
                metric["role"] = "constant"
            if mname in seen_metric:
                out["metrics"][seen_metric[mname]] = metric
            else:
                seen_metric[mname] = len(out["metrics"])
                out["metrics"].append(metric)
    if out["verdict"] in ("FAIL", "INCOMPLETE") and not out["reason"]:
        bad = [c for c in out["checks"] if c["status"] == "FAIL"]
        if bad:
            out["reason"] = "%s: %s" % (bad[0]["name"], bad[0]["detail"][:200])
    return out


_PHASE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+(.*)$")


def parse_progress_txt(text: str):
    """`2026-10-05T06:56:40.787Z phase=TELEPORT startup_and_features_ms=85 feature_ms=0` -> timeline."""
    tl = []
    for ln in (text or "").splitlines():
        m = _PHASE_RE.match(ln.strip())
        if not m:
            continue
        ts = parse_iso(m.group(1))
        kv = {k: v for k, v in _KV_RE.findall(m.group(2))}
        phase = kv.get("phase")
        if phase is None:
            phase = m.group(2).split()[0] if m.group(2).split() else "?"
        entry = {"ts": ts, "phase": phase}
        for k, v in kv.items():
            if k == "phase":
                continue
            n = to_number(v)
            entry[k] = n if n is not None else v
        tl.append(entry)
    for i, e in enumerate(tl):
        nxt = tl[i + 1]["ts"] if i + 1 < len(tl) else None
        e["dwell_s"] = round(nxt - e["ts"], 3) if (nxt is not None and e["ts"] is not None) else None
    return tl


# ----------------------------------------------------------------------------------------------
# harness progress.jsonl / result.json
# ----------------------------------------------------------------------------------------------

def parse_progress_jsonl(text: str):
    """Tolerant: skips partial/garbled lines (the file is appended while the game runs)."""
    samples = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and "t_ms" in o:
            samples.append(o)
    return samples


def downsample(values, limit=600):
    if len(values) <= limit:
        return list(values)
    step = len(values) / float(limit)
    return [values[min(len(values) - 1, int(i * step))] for i in range(limit)]


def progress_series(samples, limit=600):
    """Columnar series for charts."""
    keys = ["fps", "frame_ms_p50", "frame_ms_p95", "frame_ms_max", "tick_ms_p50", "heap_used_mb", "heap_max_mb",
            "chunks_rendered"]
    cols = {"t_ms": downsample([s.get("t_ms") for s in samples], limit)}
    for k in keys:
        vals = [s.get(k) for s in samples]
        if any(v is not None for v in vals):
            cols[k] = downsample(vals, limit)
    cols["phase"] = downsample([s.get("phase") for s in samples], limit)
    cols["step"] = downsample([s.get("step") for s in samples], limit)
    return cols


HARNESS_SCHEMA = "mcdevlab.harness.result.v1"


def validate_harness_result(obj):
    """Return a list of human-readable contract problems (empty = conforms)."""
    problems = []
    if not isinstance(obj, dict):
        return ["result.json is not an object"]
    if obj.get("schema") != HARNESS_SCHEMA:
        problems.append("schema is %r, expected %r" % (obj.get("schema"), HARNESS_SCHEMA))
    if obj.get("verdict") not in ("PASS", "FAIL", "ERROR", "TIMEOUT"):
        problems.append("verdict %r not one of PASS|FAIL|ERROR|TIMEOUT" % (obj.get("verdict"),))
    for k in ("scenario", "candidate", "environment", "steps", "metrics"):
        if k not in obj:
            problems.append("missing %s" % k)
    for i, m in enumerate(obj.get("metrics") or []):
        if not isinstance(m, dict) or "name" not in m or to_number(m.get("value")) is None:
            problems.append("metrics[%d] needs name and numeric value" % i)
        elif m.get("better") not in ("lower", "higher", None):
            problems.append("metrics[%d].better must be lower|higher" % i)
    return problems


def harness_to_run_fields(obj, out_dir_rel=""):
    """Map result.json to run-model fields. Does not assign id/host defaults."""
    env = dict(obj.get("environment") or {})
    scen = obj.get("scenario") or {}
    cand = obj.get("candidate") or {}
    started = parse_iso(obj.get("started_utc"))
    finished = parse_iso(obj.get("finished_utc"))
    dur = obj.get("duration_ms")
    metrics = []
    for m in obj.get("metrics") or []:
        v = to_number(m.get("value"))
        if v is None or not m.get("name"):
            continue
        better = m.get("better")
        metrics.append({"name": m["name"], "segment": m.get("segment") or None, "unit": m.get("unit") or "",
                        "value": v, "better": better if better in ("lower", "higher") else None})
    series = {}
    for label, s in (obj.get("series") or {}).items():
        vals = [to_number(x) for x in (s.get("frame_ms") or [])]
        series[label] = {"frame_ms": vals, "interval_ms": to_number(s.get("interval_ms"))}
    shots = []
    for s in obj.get("screenshots") or []:
        if s.get("file"):
            shots.append({"name": s.get("name") or s["file"], "file": s["file"], "step": s.get("step"),
                          "width": s.get("width"), "height": s.get("height")})
    steps = []
    for s in obj.get("steps") or []:
        steps.append({"index": s.get("index"), "type": s.get("type"), "args": s.get("args") or {},
                      "status": s.get("status"), "started_ms": s.get("started_ms"),
                      "duration_ms": s.get("duration_ms"), "message": s.get("message")})
    checks = []
    for lm in obj.get("log_markers") or []:
        checks.append({"name": "log marker: %s" % lm.get("pattern"), "status": "PASS" if lm.get("matched") else "FAIL",
                       "detail": lm.get("line") or ""})
    return {
        "verdict_raw": obj.get("verdict"), "verdict": canon_verdict(obj.get("verdict")), "reason": obj.get("reason"),
        "scenario": {"name": scen.get("name"), "sha256": scen.get("sha256"), "path": scen.get("path")},
        "candidate": {"label": cand.get("label"), "commit": cand.get("commit"),
                      "jar_sha256": cand.get("jar_sha256"), "mod_id": cand.get("mod_id")},
        "host_raw": obj.get("host"), "started_ts": started, "finished_ts": finished,
        "duration_s": (dur / 1000.0) if isinstance(dur, (int, float)) else
                      ((finished - started) if started and finished else None),
        "environment": env, "metrics": metrics, "series": series, "screenshots": shots, "steps": steps,
        "checks": checks, "log_markers": obj.get("log_markers") or [], "exit": obj.get("exit"),
    }


# ----------------------------------------------------------------------------------------------
# Dev Lab smoke summaries
# ----------------------------------------------------------------------------------------------

def _renderer_from_line(line):
    if not line:
        return None
    m = re.search(r"OpenGL Renderer:\s*(.+)$", line)
    return m.group(1).strip() if m else None


def parse_smoke_ct224(s):
    """mcdevlab.smoke.v2 (CT224 run_feature_plan_smoke.py wrapper)."""
    gpu = s.get("gpu")
    return {
        "verdict_raw": s.get("status"), "verdict": canon_verdict(s.get("status")),
        "jar_sha256": s.get("jar_sha256"), "gl_renderer": gpu if isinstance(gpu, str) else (gpu or {}).get("renderer"),
        "fixture": s.get("fixture"), "duration_s": to_number(s.get("seconds")),
        "result_txt": s.get("result_txt"), "evidence_dir": s.get("evidence_dir"),
        "wrapper_exit": s.get("wrapper_exit"), "crash": s.get("crash"),
        "harness_exit": s.get("harness_exit") or {},
    }


def parse_smoke_fd(s):
    """orbital-atlas.fd-smoke.v1 (fd smoke / feature-plan summaries)."""
    cand = s.get("candidate") or {}
    gpu = s.get("gpu_identity") or {}
    checks = []
    c = s.get("checks")
    if isinstance(c, dict):
        for k, v in c.items():
            checks.append({"name": k, "status": "PASS" if v is True else ("FAIL" if v is False else str(v)), "detail": ""})
    for f in s.get("failures") or []:
        checks.append({"name": "failure", "status": "FAIL", "detail": str(f)})
    metrics = []
    for key in ("duration_s", "smoke_launcher_exit", "harness_exit_code", "owned_jvm_exit_code", "fresh_result_file_count"):
        v = to_number(s.get(key))
        if v is not None:
            metrics.append({"name": key, "segment": None, "unit": "s" if key == "duration_s" else "",
                            "value": v, "better": "lower" if key in ("duration_s",) else None})
    for k, v in (s.get("probe_results") or {}).items():
        text = v if isinstance(v, str) else json.dumps(v)
        word = text.split(None, 1)[0] if text.strip() else "?"
        checks.append({"name": "probe " + k, "status": word if word in ("PASS", "FAIL", "INCOMPLETE") else "INFO",
                       "detail": text[:300]})
    verdict_raw = s.get("verdict")
    return {
        "verdict_raw": verdict_raw, "verdict": canon_verdict(verdict_raw),
        "candidate": {"label": cand.get("label"), "commit": cand.get("source_commit"),
                      "jar_sha256": cand.get("sha256") or s.get("jar_sha256_launched"), "mod_id": cand.get("mod_id")},
        "started_ts": to_number(s.get("started")), "finished_ts": to_number(s.get("finished")),
        "duration_s": to_number(s.get("duration_s")),
        "gl_renderer": s.get("game_opengl_renderer") or gpu.get("renderer") or s.get("renderer"),
        "gl_version": gpu.get("version"), "gl_vendor": gpu.get("vendor"),
        "max_heap": s.get("effective_max_heap"), "checks": checks, "metrics": metrics,
        "evidence_dir": s.get("evidence_dir"), "run_id": s.get("run_id"), "run_label": _arg_after(s.get("args"), "--run-label"),
        "smoke_script": s.get("smoke_script"), "result_txt_head": s.get("result_txt_head"),
        "args": s.get("args") or [], "failures": s.get("failures") or [],
    }


def _arg_after(args, flag):
    if not isinstance(args, list):
        return None
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def parse_smoke_gpc(s):
    """mcdevlab.gpc.smoke.v1 (also the Mac client_smoke summary, same shape)."""
    host = s.get("host") if isinstance(s.get("host"), dict) else {}
    metrics = []
    for key, better in (("elapsed_seconds", "lower"), ("log_error_count", "lower"), ("gl_error_count", "lower"),
                        ("map_failsafe_count", "lower"), ("diag_line_count", None)):
        v = to_number(s.get(key))
        if v is not None:
            metrics.append({"name": key, "segment": None, "unit": "s" if key == "elapsed_seconds" else "",
                            "value": v, "better": better})
    checks = []
    for c in s.get("checks") or []:
        if isinstance(c, dict):
            checks.append({"name": c.get("name"), "status": "PASS" if c.get("ok") else "FAIL", "detail": c.get("detail") or ""})
    status = s.get("status")
    return {
        "verdict_raw": status, "verdict": canon_verdict(status), "reason": s.get("reason"),
        "run_id": s.get("run_id"),
        "candidate": {"label": s.get("label"), "commit": s.get("source_commit"), "jar_sha256": s.get("jar_sha256"), "mod_id": None},
        "started_ts": parse_iso(s.get("started_utc")), "finished_ts": parse_iso(s.get("finished_utc")),
        "duration_s": to_number(s.get("elapsed_seconds")),
        "gl_renderer": _renderer_from_line(s.get("renderer_line")),
        "metrics": metrics, "checks": checks, "dry_run": bool(s.get("dry_run")) or status in ("DRYRUN_OK",),
        "feature_plan_result": s.get("feature_plan_result"), "mode": s.get("mode"), "gpus": host.get("gpus"),
        "computer": host.get("computer"), "profile": s.get("profile"), "target": s.get("target"),
        "idle_minutes": to_number(s.get("idle_minutes_at_start") or s.get("idle_minutes")),
        "hardware": s.get("hardware"), "world": s.get("world"),
        "crash_marker_line": s.get("crash_marker_line"), "crash_report_path": s.get("crash_report_path"),
        "last_diag_line": s.get("last_diag_line"),
    }


def parse_c39_validation(v, latest_stats=None):
    """mcdevlab.c39-validation.v1 receipt (+ optional stats from latest.json packets[])."""
    stats = v.get("stats") or latest_stats
    metrics = []
    if isinstance(stats, dict):
        mapping = (("median_ms", "frame_ms_p50", "ms", "lower"), ("p95_ms", "frame_ms_p95", "ms", "lower"),
                   ("p99_ms", "frame_ms_p99", "ms", "lower"), ("max_ms", "frame_ms_max", "ms", "lower"),
                   ("hitches", "hitches", "", "lower"), ("sample_count", "samples", "", None))
        for src, name, unit, better in mapping:
            n = to_number(stats.get(src))
            if n is not None:
                metrics.append({"name": name, "segment": "map_open", "unit": unit, "value": n, "better": better})
    labels = v.get("labels") or {}
    return {
        "verdict_raw": v.get("verdict"), "verdict": canon_verdict(v.get("verdict")),
        "reason": ("%s: %s" % (v.get("code"), v.get("reason"))) if v.get("code") else None,
        "started_ts": parse_iso(v.get("validated_utc")), "metrics": metrics,
        "packet": v.get("packet"), "packet_sha256": v.get("packet_sha256"), "producer": v.get("producer"),
        "matched_comparison_ok": labels.get("usable_for_matched_comparison") if labels else None,
        "labels": labels, "consumer": v.get("consumer"), "stats": stats,
    }


# ----------------------------------------------------------------------------------------------
# Dev Lab jobs (run_list entries)
# ----------------------------------------------------------------------------------------------

def parse_job(job):
    rc = job.get("receipt") or {}
    err = rc.get("error") or {}
    state = job.get("state")
    started = rc.get("started")
    finished = rc.get("finished")
    created = job.get("created")
    return {
        "job_id": job.get("id"), "request_id": job.get("request_id"), "instance": job.get("instance"),
        "recipe": job.get("recipe"), "resource": job.get("resource"), "state": state,
        "kind": rc.get("kind") or "test",
        "created": created, "started": started if started is not None else created,
        "finished": finished, "heartbeat": job.get("heartbeat"), "exit_code": rc.get("exit_code"),
        "error_type": err.get("type"), "error_message": err.get("message"),
        "artifacts": [a.get("name") for a in rc.get("artifacts") or []],
        "log_bytes": rc.get("log_bytes"), "cancel_requested": job.get("cancel_requested"),
        "note": job.get("note"),
    }


def job_verdict(j):
    """Verdict for a job that has no richer evidence."""
    state = (j.get("state") or "").upper()
    if state == "FAILED" and j.get("error_type") == "ClientFailureLatched":
        return "FAIL"
    return canon_verdict({"FAILED": "ERROR"}.get(state, state))


# ----------------------------------------------------------------------------------------------
# misc small parsers
# ----------------------------------------------------------------------------------------------

def parse_client_args(text):
    """Pull window size / gameDir from a ModLauncher args line."""
    out = {}
    m = re.search(r"--width,\s*(\d+),\s*--height,\s*(\d+)", text or "")
    if not m:
        m2, m3 = re.search(r"--width,\s*(\d+)", text or ""), re.search(r"--height,\s*(\d+)", text or "")
        if m2 and m3:
            out["window"] = [int(m2.group(1)), int(m3.group(1))]
    else:
        out["window"] = [int(m.group(1)), int(m.group(2))]
    return out


def run_stamp_from_name(name):
    """Last 20261005T065523Z-looking token of a dir name -> epoch."""
    m = re.findall(r"\d{8}T\d{6}Z", name or "")
    return stamp_to_ts(m[-1]) if m else None


_FAILURES_RE = re.compile(r"RESULT\s+(PASS|FAIL)\b.*?failures=\[(.*?)\]\s*$")
_EXC_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit)\b.*")
_KEYWORD_RE = re.compile(r"\b(refus\w*|mismatch|failed|failure|error|exception|denied|busy|blocked)\b", re.I)


def extract_reason(text, maxlen=400):
    """Best one-line reason out of a failed Dev Lab job's output tail (wrapper logs are free-form).

    Order: `RESULT FAIL ... failures=[...]` line; a Python exception line; a refused/failed keyword line before
    any JSON dump; `error`/`reason` of the JSON dump; the last readable line.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.replace("\r", "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _FAILURES_RE.search(ln)
        if m and m.group(1) == "FAIL":
            return ("failures: " + m.group(2).replace("'", ""))[:maxlen]
    cut = next((i for i, ln in enumerate(lines) if ln.startswith("{")), len(lines))
    pre = lines[:cut]
    for ln in reversed(pre):
        if _EXC_LINE_RE.match(ln):
            return ln[:maxlen]
    for ln in reversed(pre):
        if _KEYWORD_RE.search(ln) and not ln.startswith(("File ", "~", "^", "Traceback")):
            return ln[:maxlen]
    if cut < len(lines):
        blob = "\n".join(lines[cut:])
        try:
            obj, _ = json.JSONDecoder().raw_decode(blob)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for k in ("error", "reason"):
                if obj.get(k):
                    return ("%s: %s" % (k, obj[k]))[:maxlen] if k == "error" else str(obj[k])[:maxlen]
            if obj.get("status") and str(obj["status"]).upper() not in ("PASS", "PASSED", "OK"):
                return "status %s" % obj["status"]
    for ln in reversed(lines):
        if re.search(r"[A-Za-z]{3}", ln) and not re.match(r'^[\s{}\[\]",:]*"?\w+"?\s*:', ln) and not ln.startswith("File "):
            return ln[:maxlen]
    return None
