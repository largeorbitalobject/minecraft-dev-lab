"""Metric deltas, run-to-run noise and the environment-mismatch refusal.

Rule: a metric delta is only called better/worse when the two runs measured the same thing in the same
environment. If the renderer, host, scenario or settings differ, the delta is withheld (state
`not_comparable`) and the mismatch is reported instead. Facts missing on one side are listed as unverified,
which does not refuse but is always shown.
"""
from __future__ import annotations

import json
import math
import re

from .model import metric_key

NOISE_MIN_RUNS = 3
NOISE_K = 2.0  # a delta inside +/- k standard deviations of same-code runs is flagged as noise

# Preferred "key" metrics per gate family, in display order. Names are regex (full match) on the metric key.
KEY_METRIC_PATTERNS = {
    "harness": [r"(.+:)?frame_ms_p95", r"(.+:)?fps_avg", r"(.+:)?hitches_50ms", r"(.+:)?frame_ms_p99",
                r"(.+:)?heap_used_mb_max", r"(.+:)?tick_ms_p95"],
    "feature-plan": [r"overlay_200\.overlay_p95_ns", r"overlay_512\.overlay_p95_ns", r"overlay_200\.frame_p95_ns",
                     r"overlay_512\.frame_p95_ns", r"export_2048_png\.completion_ms", r"export_1024_png\.completion_ms",
                     r"export_512_png\.completion_ms"],
    "c39-frame-metrics": [r".*frame_ms_p50", r".*frame_ms_p95", r".*frame_ms_p99", r".*hitches"],
    "smoke": [r"duration_s", r"viewer\.log_errors", r"viewer\.log_warns"],
}


def key_metrics(run, limit=4):
    """Pick the few metrics that summarise a run (matrix cells, sparklines)."""
    ms = run.get("metrics") or []
    by_key = {metric_key(m): m for m in ms}
    pats = KEY_METRIC_PATTERNS.get("harness" if run.get("source") == "harness" else run.get("gate"), [])
    chosen = []
    for pat in pats:
        rx = re.compile(pat)
        for k, m in by_key.items():
            if rx.fullmatch(k) and k not in [metric_key(c) for c in chosen]:
                chosen.append(m)
        if len(chosen) >= limit and run.get("source") != "harness":
            break
    if not chosen:
        chosen = [m for m in ms if m.get("better") and not m.get("role") and not m["name"].startswith("viewer.")]
    return chosen[:limit if run.get("source") != "harness" else limit + 2]


# ----------------------------------------------------------------------------------------------
# environment
# ----------------------------------------------------------------------------------------------

def _norm(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return "x".join(str(x) for x in v)
    return re.sub(r"\s+", " ", str(v)).strip().lower() or None


def _norm_renderer(v):
    """Keep the device identity; drop driver/kernel build detail (that goes to the soft 'GL version' line)."""
    n = _norm(v)
    if not n:
        return None
    n = re.sub(r",\s*llvm [\d.]+", "", n)
    n = re.sub(r",\s*drm [\d.]+", "", n)
    n = re.sub(r",\s*\d+\.\d+\.\d+[\w.\-]*(?=\))", "", n)
    return n


def _os_family(v):
    """'Linux amd64 7.0.14-14-pve' -> 'linux amd64' (kernel point releases are not a mismatch)."""
    n = _norm(v)
    if not n:
        return None
    parts = n.split()
    if parts and parts[0] in ("linux", "windows", "mac", "macos", "darwin"):
        return " ".join(parts[:2])
    return n


def env_facts(run):
    env = run.get("environment") or {}
    scen = env.get("scenario") or {}
    facts = {
        "host": ("hard", _norm(run.get("host"))),
        "gate": ("hard", _norm(run.get("gate"))),
        "scenario": ("hard", _norm(scen.get("name"))),
        "scenario sha256": ("hard", _norm(scen.get("sha256"))),
        "fixture": ("hard", _norm(scen.get("fixture"))),
        "fixture level sha256": ("hard", _norm(scen.get("level_sha256"))),
        "gate definition": ("hard", _norm(scen.get("gates"))),
        "GL renderer": ("hard", _norm_renderer(env.get("gl_renderer"))),
        "OS": ("hard", _os_family(env.get("os"))),
        "Minecraft": ("hard", _norm(env.get("minecraft"))),
        "loader": ("hard", _norm(env.get("loader"))),
        "loader version": ("hard", _norm(env.get("loader_version"))),
        "max heap": ("hard", _norm(env.get("max_heap"))),
        "client manifest": ("hard", _norm(env.get("manifest_sha256"))),
        "settings": ("hard", _norm(json.dumps(env.get("settings"), sort_keys=True)) if env.get("settings") else None),
        "window": ("hard", _norm(env.get("window"))),
        "GL version": ("soft", _norm(env.get("gl_version"))),
        "Java": ("soft", _norm(env.get("java"))),
        "OS build": ("soft", _norm(env.get("os"))),
        "CPU": ("soft", _norm(env.get("cpu"))),
    }
    return facts


def _mods_dict(run):
    out = {}
    for m in (run.get("environment") or {}).get("mods") or []:
        if isinstance(m, dict) and m.get("id"):
            out[m["id"]] = str(m.get("version"))
    return out


def compare_env(base, cand):
    """-> {comparable: bool, mismatches: [...], unverified: [...], notes: [...]}"""
    bf, cf = env_facts(base), env_facts(cand)
    mism, unver, notes = [], [], []
    for field in bf:
        sev, bv = bf[field]
        _, cv = cf[field]
        if bv is None and cv is None:
            continue
        if bv is None or cv is None:
            if sev == "hard":
                unver.append({"field": field, "base": bv, "cand": cv,
                              "note": "missing on %s side" % ("baseline" if bv is None else "candidate")})
            continue
        if bv != cv:
            mism.append({"field": field, "severity": sev, "base": bv, "cand": cv})
    # mods
    bm, cm = _mods_dict(base), _mods_dict(cand)
    if bm and cm:
        diff = sorted(k for k in set(bm) | set(cm) if bm.get(k) != cm.get(k))
        own = {x for x in ((base.get("candidate") or {}).get("mod_id"), (cand.get("candidate") or {}).get("mod_id")) if x}
        unexplained = [k for k in diff if k not in own]
        if len(unexplained) == 1 and not own:
            notes.append("assumed candidate mod: %s (%s -> %s)" % (unexplained[0], bm.get(unexplained[0]), cm.get(unexplained[0])))
        elif unexplained:
            if len(unexplained) == 1:
                notes.append("mod differs: %s (%s -> %s)" % (unexplained[0], bm.get(unexplained[0]), cm.get(unexplained[0])))
                mism.append({"field": "mods", "severity": "soft", "base": "%s=%s" % (unexplained[0], bm.get(unexplained[0])),
                             "cand": "%s=%s" % (unexplained[0], cm.get(unexplained[0]))})
            else:
                shown = ", ".join(unexplained[:6]) + (" ..." if len(unexplained) > 6 else "")
                mism.append({"field": "mods", "severity": "hard", "base": "%d mods differ" % len(unexplained), "cand": shown})
    elif bool(bm) != bool(cm):
        unver.append({"field": "mods", "base": len(bm) or None, "cand": len(cm) or None, "note": "mod list missing on one side"})
    for r, side in ((base, "baseline"), (cand, "candidate")):
        if (r.get("environment") or {}).get("matched_comparison_ok") is False:
            mism.append({"field": "source flag", "severity": "hard", "base": "usable_for_matched_comparison=false" if side == "baseline" else None,
                         "cand": "usable_for_matched_comparison=false" if side == "candidate" else None})
    for r, side in ((base, "baseline"), (cand, "candidate")):
        v = r.get("verdict")
        if v and v != "PASS":
            mism.append({"field": "run verdict", "severity": "hard", "base": v if side == "baseline" else None,
                         "cand": v if side == "candidate" else None,
                         "note": "metrics from a run that did not pass are not a measurement"})
    hard = [m for m in mism if m["severity"] == "hard"]
    return {"comparable": not hard, "mismatches": mism, "unverified": unver, "notes": notes}


# ----------------------------------------------------------------------------------------------
# noise and deltas
# ----------------------------------------------------------------------------------------------

def noise_stats(runs, key):
    """Std-dev of one metric across same-code runs. None when fewer than NOISE_MIN_RUNS samples."""
    vals = []
    for r in runs:
        for m in r.get("metrics") or []:
            if metric_key(m) == key and isinstance(m.get("value"), (int, float)):
                vals.append(float(m["value"]))
                break
    n = len(vals)
    if n < NOISE_MIN_RUNS:
        return {"n": n, "sd": None, "band": None, "mean": (sum(vals) / n) if n else None}
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    return {"n": n, "sd": sd, "band": NOISE_K * sd, "mean": mean}


def _pct(base, cand):
    if base in (0, None) or cand is None:
        return None
    return (cand - base) / abs(base) * 100.0


def metric_deltas(base, cand, comparable=True, base_group=None, cand_group=None):
    """List of per-metric comparison rows."""
    bi = {metric_key(m): m for m in base.get("metrics") or []}
    ci = {metric_key(m): m for m in cand.get("metrics") or []}
    keys = list(ci.keys()) + [k for k in bi if k not in ci]
    rows = []
    for k in keys:
        b, c = bi.get(k), ci.get(k)
        ref = c or b
        row = {"key": k, "name": ref["name"], "segment": ref.get("segment"), "unit": ref.get("unit") or "",
               "better": ref.get("better"), "base": b["value"] if b else None, "cand": c["value"] if c else None,
               "limit": (c or {}).get("limit"), "role": ref.get("role"), "delta": None, "pct": None, "state": None,
               "noise": None}
        if b is None or c is None:
            row["state"] = "missing_base" if b is None else "missing_cand"
            rows.append(row)
            continue
        delta = c["value"] - b["value"]
        row["delta"] = delta
        row["pct"] = _pct(b["value"], c["value"])
        if not comparable:
            row["state"] = "not_comparable"
            rows.append(row)
            continue
        if delta == 0:
            row["state"] = "same"
        elif row["better"] is None:
            row["state"] = "neutral"
        else:
            ns = None
            for grp in (base_group, cand_group):
                if grp:
                    s = noise_stats(grp, k)
                    if s["band"] is not None and (ns is None or s["band"] > ns["band"]):
                        ns = s
                    elif ns is None:
                        ns = s
            row["noise"] = ns
            if ns and ns.get("band") is not None and abs(delta) <= ns["band"]:
                row["state"] = "within_noise"
            else:
                good = (delta < 0) if row["better"] == "lower" else (delta > 0)
                row["state"] = "better" if good else "worse"
        if row["limit"] is not None and row["better"] == "lower" and c["value"] > row["limit"]:
            row["over_limit"] = True
        rows.append(row)
    return rows


def compare_runs(base, cand, base_group=None, cand_group=None):
    env = compare_env(base, cand)
    rows = metric_deltas(base, cand, env["comparable"], base_group, cand_group)
    return {"base": base.get("id"), "cand": cand.get("id"), "env": env, "metrics": rows,
            "summary": summarize_rows(rows)}


def summarize_rows(rows):
    s = {"better": 0, "worse": 0, "same": 0, "within_noise": 0, "neutral": 0, "not_comparable": 0, "missing": 0}
    for r in rows:
        st = r["state"]
        if st in ("missing_base", "missing_cand"):
            s["missing"] += 1
        elif st in s:
            s[st] += 1
    return s
