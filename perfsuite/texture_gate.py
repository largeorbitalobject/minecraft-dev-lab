#!/usr/bin/env python3
"""Fail-closed close-zoom texture gate, independent of Atlas source and frame timings.

The count is the DETAIL-CUT census: detT (native) + detS (surface), separately
retained for lastMap and lastRegional and summed for the first textured draw.
This is drawn tiles, not LOD-0 coverage, stored tiles, or screenshot inference.
"""
import argparse
import json
import math
from pathlib import Path
import re

LIMIT_MS = 60000


def is_texture_scenario(result):
    return bool(re.fullmatch(r"atlas-ps-q-texture-(fd|ct224|gpc|mac)",
                             (result.get("scenario") or {}).get("name", "")))


def census(message, name):
    m = re.search(r"\b" + name + r"=(\S+)", message)
    if not m:
        raise ValueError("missing " + name)
    values = dict(x.split(":", 1) for x in m.group(1).split(",") if ":" in x)
    out = {}
    for key in ("view", "detT", "detS"):
        if not re.fullmatch(r"\d+", values.get(key, "")):
            raise ValueError("missing/invalid " + name + "." + key)
        out[key] = int(values[key])
    out["textured"] = out["detT"] + out["detS"]
    return out


def fixture_name(meta):
    fixture = meta.get("fixture")
    return fixture if isinstance(fixture, str) else (fixture or {}).get("fixture")


def fresh_realism(archive):
    """A cached clean sidecar cannot authorize changed logs or a newer parser."""
    import sys
    sys.path.insert(0, "/root/minecraft-dev-lab-config/bin")
    try:
        from harness_realism import analyze_run
        return analyze_run(archive)
    except (ImportError, OSError, ValueError, TypeError, KeyError) as exc:
        return {"detail_timing_representative": None, "stations": [], "analysis_failed": True,
                "verdict_notes": ["REALISM_UNMEASURED fresh analysis failed: " + type(exc).__name__]}


def evaluate(result, meta, realism=None, receipt=None):
    """Return scorecard even for absent telemetry; callers must honor FAIL.

    Time starts at the open_map request (as in DETAIL-CUT/tab.py), not after
    map readiness. A first positive observation after 60 s cannot prove the
    deadline was met. Polling intervals make this an observed upper bound.
    """
    fixture = fixture_name(meta)
    card = {"schema": "atlas.perfsuite.texture.v1", "verdict": "FAIL",
            "station": "plain_surface", "limit_ms": LIMIT_MS, "fixture": fixture,
            "count_source": "map_state lastMap.detT+detS + lastRegional.detT+detS",
            "first_textured_ms": None, "first_textured_lower_bound_ms": None, "max_textured_tiles": None,
            "observed_ms": None, "samples": [], "reasons": [], "notes": []}
    reasons = card["reasons"]
    receipt = receipt or {}
    confirmed_slow_exit = (result.get("runner_verdict") == "WARN_SLOW_EXIT"
                           and receipt.get("status") == "WARN_SLOW_EXIT"
                           and all(receipt.get(key) is True for key in
                                   ("exit_confirmed", "minecraft_exit_confirmed", "no_minecraft_jvm", "slow_exit_warning"))
                           and receipt.get("problems") == [])
    card["harness_verdict"] = result.get("verdict")
    if result.get("verdict") != "PASS" and not confirmed_slow_exit:
        reasons.append("HARNESS_NOT_PASS")
    if not fixture or not fixture.endswith("-consistent"):
        reasons.append("CONSISTENT_FIXTURE_REQUIRED")
    props = meta.get("props") or ""
    props = props.splitlines() if isinstance(props, str) else props
    effective_props = {line.partition("=")[0].strip(): line.partition("=")[2].strip()
                       for line in props if isinstance(line, str) and "=" in line and not line.lstrip().startswith("#")}
    card["product_profile"] = effective_props.get("orbitalatlas.playProfile") == "true"
    if not card["product_profile"]:
        reasons.append("PRODUCT_PROFILE_REQUIRED")
    steps = result.get("steps", [])
    opens = [(i, s) for i, s in enumerate(steps) if s.get("type") == "orbitalatlas:open_map"]
    if len(opens) != 1 or (opens[0][1].get("args") or {}).get("span") != 128:
        reasons.append("ONE_CLOSE_ZOOM_128_OPEN_REQUIRED")
        return card
    open_pos, opened = opens[0]
    t0 = opened.get("started_ms")
    if not isinstance(t0, (int, float)) or not math.isfinite(t0) or t0 < 0 or opened.get("status") != "PASS":
        reasons.append("INVALID_MAP_OPEN")
        return card
    # lastMap/lastRegional remain visible in diagnostics after closing the map.
    # Only observations within this open, before a close/teleport/quit, count.
    active_steps = []
    for step in steps[open_pos + 1:]:
        teleport_command = step.get("type") == "command" and re.search(
            r"(?:^|\s)/?(?:tp|teleport)\s", (step.get("args") or {}).get("cmd", ""))
        if step.get("type") in ("orbitalatlas:close_map", "quit", "teleport") or teleport_command:
            break
        active_steps.append(step)
    for step in active_steps:
        if step.get("type") != "orbitalatlas:map_state":
            continue
        try:
            stamp = step.get("started_ms")
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < t0:
                raise ValueError("invalid timestamp")
            if step.get("status") != "PASS":
                raise ValueError("map_state did not pass")
            message = step.get("message") or ""
            fine, regional = census(message, "lastMap"), census(message, "lastRegional")
            situation = re.search(r"\bsituation=(\S+)", message)
            if not situation or situation.group(1) != "OUTDOORS":
                raise ValueError("plain surface must report OUTDOORS")
            count = fine["textured"] + regional["textured"]
            card["samples"].append({"since_open_ms": stamp - t0,
                                    "lastMap": fine, "lastRegional": regional,
                                    "textured_tiles": count})
        except ValueError as exc:
            reasons.append("INVALID_TEXTURE_TELEMETRY step=%s %s" % (step.get("index", "?"), exc))
    samples = sorted(card["samples"], key=lambda p: p["since_open_ms"])
    if not samples:
        reasons.append("MISSING_TEXTURE_TELEMETRY")
    else:
        card["max_textured_tiles"] = max(p["textured_tiles"] for p in samples)
        card["observed_ms"] = samples[-1]["since_open_ms"]
        positive = next((p for p in samples if p["textured_tiles"] > 0), None)
        card["first_textured_ms"] = positive["since_open_ms"] if positive else None
        if positive:
            earlier = [p["since_open_ms"] for p in samples if p["since_open_ms"] < positive["since_open_ms"]]
            card["first_textured_lower_bound_ms"] = max(earlier) if earlier else 0
        if card["observed_ms"] < LIMIT_MS:
            reasons.append("INCOMPLETE_60S_OBSERVATION")
        if positive is None:
            reasons.append("ZERO_TEXTURED_TILES_IN_60S" if card["observed_ms"] >= LIMIT_MS else "NO_TEXTURED_TILE_OBSERVED")
        elif positive["since_open_ms"] > LIMIT_MS:
            reasons.append("FIRST_TEXTURED_TILE_AFTER_60S")
    # Harness collection adds realism.json and/or result.realism. High recapture
    # counts disqualify detail timings even when they produce quick textures.
    realism = (result.get("realism") or {}) if realism is None else realism
    card["detail_timing_representative"] = realism.get("detail_timing_representative")
    if realism.get("detail_timing_representative") is not True:
        reasons.append("DETAIL_TIMING_NOT_REPRESENTATIVE" if realism.get("detail_timing_representative") is False else "REALISM_UNMEASURED")
    notes = realism.get("verdict_notes", realism.get("notes", []))
    if isinstance(notes, str):
        notes = [notes]
    for note in notes:
        if "SEEDED_RECAPTURE" in str(note):
            card["notes"].append(str(note))
            if "DETAIL_TIMING_NOT_REPRESENTATIVE" not in reasons:
                reasons.append("DETAIL_TIMING_NOT_REPRESENTATIVE")
    if not reasons:
        card["verdict"] = "PASS"
    return card


def score_archive(archive, write=False):
    archive = Path(archive)
    result = json.loads((archive / "result.json").read_text())
    if not is_texture_scenario(result):
        return None
    meta = json.loads((archive / "meta.json").read_text()) if (archive / "meta.json").is_file() else {}
    realism = fresh_realism(archive)
    receipt = json.loads((archive / "recipe-receipt.json").read_text()) if (archive / "recipe-receipt.json").is_file() else {}
    card = evaluate(result, meta, realism, receipt)
    if write:
        target = archive / "texture-scorecard.json"
        if target.exists():
            old = target.read_text()
            if old != json.dumps(card, indent=2) + "\n":
                backup = target.with_name(target.name + ".bak-20261117-realism")
                if not backup.exists():
                    backup.write_text(old)
        target.write_text(json.dumps(card, indent=2) + "\n")
    return card


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    card = score_archive(args.archive, args.write)
    if card is None:
        ap.error("archive is not a q-texture run")
    print(json.dumps(card, indent=2))
    return 0 if card["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
