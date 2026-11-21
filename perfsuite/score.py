#!/usr/bin/env python3
"""Orbital Atlas perf suite scorer: one JSON row per archived atlas-ps-* run, plus per-segment, timeline and mapPerf rows.

  score.py [--out /root/atlas-beta-program/perf-dossier/data]

Reads /root/mcdevlab-shared/harness/results-archive/<host>/*atlas-ps-*/ (result.json, meta.json, props.txt, progress.jsonl,
latest.log or mapperf.log). Writes (overwrites) in --out:
  runs.jsonl       one row per run: identity (run_id = archive dir name, archive path), build, arm, rep, verdict, env, budget,
                   derived LOD/re-sharpen timings, burst/zoom smoothness, heap/GC peaks
  segments.jsonl   one row per run x measure segment, every metric the harness recorded (raw)
  timelines.jsonl  one row per minimap_state / map_state poll: t_ms, phase, span, parsed miniLevels / wmLevels / budget fields
  mapperf.jsonl    one row per mapPerf line: view, t, fps, frame p50/p95/max, atlas_cpu p50/p95, gc_ms, budget, budgetLevels,
                   gapFill, miniLevels, wmLevels
  runs.csv         flat CSV of runs.jsonl scalar fields
Nothing here interprets a difference; summarize.py does medians/spreads/flags.
"""
import argparse, csv, glob, json, os, re, statistics
from texture_gate import fresh_realism, score_archive
from texture_tiles import disjoint_models, measure as selected_texture_tiles

ARCH = "/root/mcdevlab-shared/harness/results-archive"
HOSTS = ("fd", "gpc", "mac", "ct224")


def num(x):
    try:
        if x in (None, "", "NaN", "nan", "-"):
            return None
        return float(x)
    except Exception:
        return None


def kv_group(text, name):
    """Parse 'name[a=1 b=2 ...]' -> dict (first occurrence)."""
    m = re.search(r"(?<![A-Za-z])" + re.escape(name) + r"\[([^\]]*)\]", text)
    if not m:
        return None
    out = {}
    for tok in m.group(1).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def parse_minilevels(msg):
    g = kv_group(msg, "miniLevels")
    if not g:
        return None
    if g.get("mode") != "regional":
        return {"mode": g.get("mode"), "stale": True}   # the census only updates in the regional minimap; ordinary-mode values are stale
    d = {k: num(g.get(k)) for k in ("cells", "l0", "l1", "l2", "l3p", "none", "maxL")}
    d["mode"] = g.get("mode")
    c = d.get("cells") or 0
    if c > 0:
        d["l0_share"] = round((d["l0"] or 0) / c, 4)
        d["mid_share"] = round(((d["l1"] or 0) + (d["l2"] or 0)) / c, 4)
        d["coarse_share"] = round((d["l3p"] or 0) / c, 4)
        d["blank_share"] = round((d["none"] or 0) / c, 4)
    return d


def parse_wmlevels(msg):
    g = kv_group(msg, "wmLevels")
    if not g or "cells" not in g:
        return None
    d = {k: num(g.get(k)) for k in ("cells", "l0", "l1", "l2", "l3p", "none", "maxL", "span", "ageMs")}
    c = d.get("cells") or 0
    if c > 0:
        d["l0_share"] = round((d["l0"] or 0) / c, 4)
        d["mid_share"] = round(((d["l1"] or 0) + (d["l2"] or 0)) / c, 4)
        d["coarse_share"] = round((d["l3p"] or 0) / c, 4)
        d["blank_share"] = round((d["none"] or 0) / c, 4)
    t = kv_group(msg, "wmLevelsT")
    if t:
        d["T"] = {k: num(v) for k, v in t.items()}
    m = re.search(r"wmMode=(\w+)", msg)
    d["wmMode"] = m.group(1) if m else None
    return d


def parse_mapstate(msg):
    d = {}
    m = re.search(r"budget=(\d+)/(\d+)of(\d+)MiB evictions=(\d+)", msg)
    if m:
        d.update(budget_used_mib=int(m.group(1)), budget_cpu_mib=int(m.group(2)), budget_limit_mib=int(m.group(3)), evictions=int(m.group(4)))
    m = re.search(r"coverage (\d+)/(\d+)", msg)
    if m:
        d.update(cov_n=int(m.group(1)), cov_d=int(m.group(2)))
    for key in ("lastRegional", "lastMap"):
        m = re.search(key + r"=(\S+)", msg)
        if m:
            d[key] = {k: num(v) for k, v in (x.split(":", 1) for x in m.group(1).split(",") if ":" in x)}
    for key in ("localTiles", "regionalTiles", "tiles", "known"):
        m = re.search(r"\b" + key + r"=(\d+)", msg)
        if m:
            d[key] = int(m.group(1))
    m = re.search(r"zoomContext=(\w+)", msg)
    d["zoomContext"] = m.group(1) if m else None
    m = re.search(r"regional=(true|false)", msg)
    d["regional"] = m.group(1) if m else None
    w = parse_wmlevels(msg)
    if w:
        d["wmLevels"] = w
    return d


def parse_mapperf_line(line):
    m = re.search(r"mapPerf (minimap|worldmap) fps=([\d.]+) frame_ms p50=([\d.NaN]+) p95=([\d.NaN]+) max=([\d.NaN]+) atlas_cpu_ms p50=([\d.NaN]+) p95=([\d.NaN]+)", line)
    if not m:
        return None
    t = re.search(r"(\d\d:\d\d:\d\d(?:\.\d+)?)\]", line)
    r = {"view": m.group(1), "clock": t.group(1) if t else None, "fps": num(m.group(2)), "frame_p50": num(m.group(3)),
         "frame_p95": num(m.group(4)), "frame_max": num(m.group(5)), "atlas_cpu_p50": num(m.group(6)), "atlas_cpu_p95": num(m.group(7))}
    g = re.search(r"gc_ms=(\d+)", line)
    r["gc_ms"] = num(g.group(1)) if g else None
    b = re.search(r"budget\(gpu=(\d+)MB cpu=(\d+)MB limit=(\d+)MB cold=(\d+)MB.*?evictions=(\d+)\)", line)
    if b:
        r["budget"] = dict(zip(("gpu_mb", "cpu_mb", "limit_mb", "cold_mb", "evictions"), map(int, b.groups())))
    bl = kv_group(line, "budgetLevels")
    if bl:
        lv = {}
        for k, v in bl.items():
            if re.fullmatch(r"(local|L\d+)", k) and v.count(":") == 2:
                g_, c_, n_ = v.split(":")
                lv[k] = {"gpu": int(g_), "cpu": int(c_), "n": int(n_)}
            elif k in ("farLocalEvictions", "worldFarLocalEvictions", "keepNearFallbacks", "localNearPlayer"):
                lv[k] = num(v)
        r["budgetLevels"] = lv
    for gname in ("gapFill", "chartGap", "miniCover", "regional"):
        g2 = kv_group(line, gname)
        if g2:
            r[gname] = {k: (num(v) if num(v) is not None else v) for k, v in g2.items() if not k.startswith("why")}
    ml = parse_minilevels(line)
    if ml:
        r["miniLevels"] = ml
    wl = parse_wmlevels(line)
    if wl:
        r["wmLevels"] = wl
    sl = re.search(r"slow\[frame=([\d.]+) atlas=([\d.]+)", line)
    if sl:
        r["slow_frame"] = num(sl.group(1)); r["slow_atlas"] = num(sl.group(2))
    return r


def phase_of(step, ctx):
    """Name the scenario phase a poll belongs to from the preceding anchor steps."""
    return ctx.get("phase")


def first_at(polls, key, thr, t0):
    for p in polls:
        v = p.get(key)
        if v is not None and v >= thr:
            return round(p["t_ms"] - t0)
    return None


def walk_steps(res):
    """Yield (step, ctx) with ctx = current phase, span, anchor time."""
    ctx = {"phase": "boot", "span": None, "anchor_ms": 0, "anchor": None}
    for st in res.get("steps", []):
        ty = st.get("type"); a = st.get("args") or {}; t = st.get("started_ms") or 0
        if ty == "measure":
            lab = a.get("label") or ""
            ctx["last_measure"] = lab
        if ty == "screenshot":
            nm = a.get("name") or ""
            if nm.startswith("join_00"):
                ctx.update(phase="join", anchor_ms=t, anchor="join")
            if nm.startswith("land_00"):
                ctx.update(phase="land", anchor_ms=t, anchor="landing")
        if ty == "orbitalatlas:open_map":
            ctx.update(span=a.get("span"), open_ms=t)
            ctx["phase"] = ctx.get("wm_phase_hint") or "wm"
        if ty == "orbitalatlas:close_map":
            ctx["span"] = None
        if ty == "orbitalatlas:fly":
            ctx.update(phase="fly" if (a.get("speed") or 0) >= 1 else "walk", anchor_ms=t, anchor="fly")
        if ty == "teleport":
            ctx.update(anchor_ms=t, anchor="teleport", phase="tp")
            if ctx.get("seen_walk"):
                ctx["phase"] = "revisit"
        if ty == "orbitalatlas:minimap_zoom" and a.get("mode") == "manual":
            ctx.update(phase="mmcave", mm_span=a.get("span"), anchor_ms=t)
        if ctx["phase"] == "walk":
            ctx["seen_walk"] = True
        yield st, ty, a, t, ctx


def effective_verdict(res, receipt):
    # The worker validates every frame/step and process exit before issuing this warning.
    if (res.get("runner_verdict") == "WARN_SLOW_EXIT" and receipt.get("status") == "WARN_SLOW_EXIT"
            and receipt.get("exit_confirmed") is True and receipt.get("minecraft_exit_confirmed") is True
            and receipt.get("no_minecraft_jvm") is True and receipt.get("slow_exit_warning") is True
            and receipt.get("problems") == []):
        return "WARN_SLOW_EXIT"
    return res.get("verdict")


def timing_eligibility(d, res):
    """Whole-run exclusion is conservative when any detail station is tainted.

    A harness PASS still describes its steps/screenshots; it must not silently
    authorize a seeded detail timing as a speed comparison.
    """
    report = fresh_realism(d)
    stations = report.get("stations") or []
    eligible = not report.get("analysis_failed") and (not stations or report.get("detail_timing_representative") is True)
    return {"detail_timing_representative": report.get("detail_timing_representative"),
            "detail_timing_station_count": len(stations),
            "timing_comparison_eligible": eligible,
            "realism_verdict_notes": report.get("verdict_notes") or [],
            "timing_exclusion_reason": None if eligible else "; ".join(report.get("verdict_notes") or ["DETAIL_REALISM_UNMEASURED"])}


def score_run(d, host):
    try:
        res = json.load(open(os.path.join(d, "result.json")))
    except Exception:
        return None
    sc = (res.get("scenario") or {}).get("name") or ""
    if not sc.startswith("atlas-ps-"):
        return None
    meta = {}
    try:
        meta = json.load(open(os.path.join(d, "meta.json")))
    except Exception:
        pass
    receipt = {}
    try:
        receipt = json.load(open(os.path.join(d, "recipe-receipt.json")))
    except Exception:
        pass
    verdict = effective_verdict(res, receipt)
    arm = meta.get("arm") or os.path.basename(d).rsplit("-", 4)[-4:]
    m = re.match(r"(?P<arm>[a-z0-9]+)-(?P<b>.+)-(?P<j>[jn])-r(?P<rep>\d+)$", meta.get("arm") or "")
    sid = re.sub(r"^atlas-ps-|-(fd|gpc|mac|ct224)$", "", sc)
    if sid == "q-basepack-off":
        sid = "q-basepack"   # Atlas-off twin: same route and measure labels; arm 'off' tells them apart
    uncapped = any(x.get("type") == "set_option" and (x.get("args") or {}).get("key") == "maxFps" for x in res.get("steps", []))
    env = res.get("environment") or {}
    row = {"run_id": os.path.basename(d), "archive": d, "host": host, "scenario": sid, "scenario_name": sc,
           "build": meta.get("candidate") or (res.get("candidate") or {}).get("label"), "candidate_sha256": meta.get("candidate_sha256"),
           "source_commit": meta.get("source_commit"), "arm_raw": meta.get("arm"),
           "arm": ((m.group("arm") if m else meta.get("arm")) or "?") + ("" if uncapped else "@fps60")
                  + ("+jfr" if (os.path.exists(os.path.join(d, "profile.jfr")) or os.path.exists(os.path.join(d, "JFR_ATTACHED.txt")) or (m and m.group("j") == "j")) else ""),
           "fps_uncapped": uncapped, "rep": int(m.group("rep")) if m else None, "jfr": (m.group("j") == "j") if m else None,
           "verdict": verdict, "harness_verdict": res.get("verdict"),
           "exit_warning": res.get("exit_warning") if verdict == "WARN_SLOW_EXIT" else None,
           "exit_saved": (res.get("exit") or {}).get("saved"), "exit_graceful": (res.get("exit") or {}).get("graceful"),
           "reason": (res.get("reason") or "")[:300], "started_utc": res.get("started_utc"),
           "finished_utc": res.get("finished_utc"), "fixture": meta.get("fixture") if isinstance(meta.get("fixture"), str) else (meta.get("fixture") or {}).get("fixture"),
           "perf_comparable": host != "ct224", "props": (meta.get("props") or "").strip().splitlines(),
           "env": {k: env.get(k) for k in ("cpu", "cpu_threads", "gl_renderer", "os", "java", "max_memory_mb", "window")}}
    row.update(timing_eligibility(d, res))
    # budget as logged at start (last value wins: fd re-scales at render init)
    logp = os.path.join(d, "latest.log")
    lines = []
    if os.path.isfile(logp):
        lines = open(logp, errors="replace").read().splitlines()
    elif os.path.isfile(os.path.join(d, "mapperf.log")):
        lines = open(os.path.join(d, "mapperf.log"), errors="replace").read().splitlines()
    bud = [l for l in lines if "Orbital Atlas sceneMeshBudgetMb=" in l]
    if bud:
        mm = re.search(r"sceneMeshBudgetMb=(\d+) \((.*?)\)", bud[-1])
        row["mesh_budget_mb"] = int(mm.group(1)) if mm else None
        row["mesh_budget_note"] = mm.group(2) if mm else None
    perf = [r for r in (parse_mapperf_line(l) for l in lines if "mapPerf" in l) if r]
    # segments
    segs = {}
    for x in res.get("metrics", []):
        segs.setdefault(x.get("segment"), {})[x["name"].replace("orbitalatlas.", "oa.")] = x.get("value")
    # timelines
    polls = []
    for st, ty, a, t, ctx in walk_steps(res):
        if ty in ("orbitalatlas:minimap_state", "orbitalatlas:map_state"):
            msg = st.get("message") or ""
            p = {"t_ms": t, "kind": "mm" if ty.endswith("minimap_state") else "wm", "phase": ctx["phase"], "span": ctx.get("span"),
                 "mm_span": ctx.get("mm_span"), "since_anchor_ms": t - (ctx.get("anchor_ms") or 0), "anchor": ctx.get("anchor"),
                 "since_open_ms": (t - ctx["open_ms"]) if ctx.get("span") and ctx.get("open_ms") is not None else None,
                 "status": st.get("status")}
            if p["kind"] == "mm":
                ml = parse_minilevels(msg)
                if ml:
                    p["miniLevels"] = ml
                mm2 = re.search(r"span=(\d+)", msg)
                p["mini_span"] = int(mm2.group(1)) if mm2 else None
                mm3 = re.search(r"context=(\w+)", msg)
                p["context"] = mm3.group(1) if mm3 else None
            else:
                p.update(parse_mapstate(msg))
            polls.append(p)
    # derived: minimap re-sharpen after landing / revisit / join
    der = {}
    for ph in ("join", "land", "revisit"):
        pp = [dict(t_ms=p["since_anchor_ms"], **{k: v for k, v in (p.get("miniLevels") or {}).items() if k.endswith("share")})
              for p in polls if p["kind"] == "mm" and p["phase"] == ph and p.get("miniLevels") and not p["miniLevels"].get("stale")]
        if row["scenario"] != "q-basepack":
            pp = []
        if pp:
            der[ph + "_mm_l0_share_first"] = pp[0].get("l0_share")
            der[ph + "_mm_l0_share_last"] = pp[-1].get("l0_share")
            der[ph + "_mm_blank_share_first"] = pp[0].get("blank_share")
            der[ph + "_mm_ms_to_l0_50"] = first_at(pp, "l0_share", 0.5, 0)
            der[ph + "_mm_ms_to_l0_90"] = first_at(pp, "l0_share", 0.9, 0)
            der[ph + "_mm_polls"] = len(pp)
    # flight: minimap regional census after each 10 s window (LOD mix while flying at 30 b/s)
    fl = [p["miniLevels"] for p in polls if p["kind"] == "mm" and p["phase"] == "fly" and p.get("miniLevels") and not p["miniLevels"].get("stale")]
    if fl:
        for k in ("l0_share", "mid_share", "coarse_share", "blank_share"):
            v = [x.get(k) for x in fl if x.get(k) is not None]
            der["fly_mm_" + k + "_med"] = round(statistics.median(v), 4) if v else None
        der["fly_mm_polls"] = len(fl)
    # world map per span: first/last census of each open (cold wm1 then warm wm2 etc. in order of appearance)
    opens = []
    for p in polls:
        if p["kind"] != "wm" or p.get("span") is None:
            continue
        key = (p["span"], p.get("since_open_ms") is not None and p["since_open_ms"] < 50)
        if not opens or opens[-1]["span"] != p["span"] or (p.get("since_open_ms") or 0) < (opens[-1]["polls"][-1].get("since_open_ms") or 0):
            opens.append({"span": p["span"], "polls": []})
        opens[-1]["polls"].append(p)
    wm = []
    for o in opens:
        ps = o["polls"]
        e = {"span": o["span"], "n": len(ps), "t_ms": [p.get("since_open_ms") for p in ps]}
        lr = [p.get("lastRegional") or {} for p in ps]
        e["regional_view_tiles"] = [x.get("view") for x in lr]
        e["regional_coarse_tiles"] = [x.get("coarse") for x in lr]
        e["regional_coarseMaxL"] = [x.get("coarseMaxL") for x in lr]
        e["budget_used_mib"] = [p.get("budget_used_mib") for p in ps]
        e["evictions"] = [p.get("evictions") for p in ps]
        wl = [p.get("wmLevels") for p in ps]
        if any(wl):
            e["wm_l0_share"] = [(w or {}).get("l0_share") for w in wl]
            e["wm_blank_share"] = [(w or {}).get("blank_share") for w in wl]
            e["wm_coarse_share"] = [(w or {}).get("coarse_share") for w in wl]
            e["wm_mid_share"] = [(w or {}).get("mid_share") for w in wl]
            e["wm_T"] = (wl[-1] or {}).get("T")
        e["phase"] = ps[0].get("phase")
        wm.append(e)
    der["wm_opens"] = wm
    # zoom burst: progress.jsonl frame max during the burst window (steps between the first 768 re-pose and the spin)
    prog = []
    pp = os.path.join(d, "progress.jsonl")
    if os.path.isfile(pp):
        for l in open(pp, errors="replace"):
            try:
                prog.append(json.loads(l))
            except Exception:
                pass
    st = res.get("steps", [])
    burst = [x for x in st if x.get("type") == "orbitalatlas:open_map" and (x.get("args") or {}).get("span") in (768,)]
    if burst and prog:
        b0 = burst[0].get("started_ms") or 0
        spin = next((x for x in st if x.get("type") == "orbitalatlas:map_spin"), None)
        b1 = (spin.get("started_ms") if spin else b0 + 12000) - 1000
        win = [x for x in prog if b0 <= (x.get("t_ms") or 0) <= b1]
        if win:
            der["zoom_burst_s"] = round((b1 - b0) / 1000, 1)
            der["zoom_burst_frame_max_ms"] = max(x.get("frame_ms_max") or 0 for x in win)
            der["zoom_burst_frame_p95_med_ms"] = statistics.median(x.get("frame_ms_p95") or 0 for x in win)
            der["zoom_burst_fps_med"] = statistics.median(x.get("fps") or 0 for x in win)
    if prog:
        der["heap_peak_mb"] = max(x.get("heap_used_mb") or 0 for x in prog)
        der["run_seconds"] = round((prog[-1].get("t_ms") or 0) / 1000)
    gcs = [v.get("gc_pause_ms_total") for v in segs.values() if v.get("gc_pause_ms_total") is not None]
    der["gc_pause_ms_sum_segments"] = round(sum(gcs), 1) if gcs else None
    # capacity: first mapPerf window with farLocal evictions, the local tile count then; max local n
    cap = {}
    loc = [(i, r["budgetLevels"]) for i, r in enumerate(perf) if r.get("budgetLevels")]
    if loc:
        cap["local_n_max"] = max((b.get("local") or {}).get("n", 0) for _, b in loc)
        cap["local_cpu_mb_max"] = round(max((b.get("local") or {}).get("cpu", 0) for _, b in loc) / 1e6, 1)
        cap["local_gpu_mb_max"] = round(max((b.get("local") or {}).get("gpu", 0) for _, b in loc) / 1e6, 1)
        firstev = next(((i, b) for i, b in loc if (b.get("farLocalEvictions") or 0) > 0 or (b.get("worldFarLocalEvictions") or 0) > 0), None)
        if firstev:
            b = firstev[1]
            cap["local_n_at_first_far_eviction"] = (b.get("local") or {}).get("n")
            cap["local_bytes_per_tile_cpu"] = round((b["local"]["cpu"] / b["local"]["n"]), 0) if (b.get("local") or {}).get("n") else None
            cap["local_bytes_per_tile_gpu"] = round((b["local"]["gpu"] / b["local"]["n"]), 0) if (b.get("local") or {}).get("n") else None
        lastb = loc[-1][1]
        cap["levels_last"] = {k: v for k, v in lastb.items() if isinstance(v, dict)}
        cap["farLocalEvictions_last"] = lastb.get("farLocalEvictions"); cap["worldFarLocalEvictions_last"] = lastb.get("worldFarLocalEvictions")
    bud2 = [r["budget"] for r in perf if r.get("budget")]
    if bud2:
        cap["budget_limit_mb"] = bud2[-1]["limit_mb"]
        cap["budget_gpu_mb_max"] = max(b["gpu_mb"] for b in bud2); cap["budget_cpu_mb_max"] = max(b["cpu_mb"] for b in bud2)
        cap["budget_evictions_last"] = bud2[-1]["evictions"]
    der["capacity"] = cap
    if sid == "q-texture":
        texture = score_archive(d)
        der["texture"] = texture
        model_tiles = selected_texture_tiles(res, texture, disjoint_models(d, meta))
        der["texture_model_tiles"] = {key: value for key, value in model_tiles.items() if key != "samples"}
        row["texture_first_ms"] = texture["first_textured_ms"]
        row["texture_verdict"] = texture["verdict"]
        row["detail_timing_representative"] = texture.get("detail_timing_representative")
        if texture["verdict"] != "PASS":
            row["verdict"] = "FAIL"
            row["reason"] = "; ".join(filter(None, [row["reason"], *texture["reasons"], *texture["notes"]]))
    row["derived"] = der
    return row, segs, polls, perf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/atlas-beta-program/perf-dossier/data")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    runs, nseg, npoll, nperf = [], 0, 0, 0
    fr = open(os.path.join(a.out, "runs.jsonl.tmp"), "w"); fs = open(os.path.join(a.out, "segments.jsonl.tmp"), "w")
    ft = open(os.path.join(a.out, "timelines.jsonl.tmp"), "w"); fp = open(os.path.join(a.out, "mapperf.jsonl.tmp"), "w")
    for host in HOSTS:
        for d in sorted(glob.glob(os.path.join(ARCH, host, "*atlas-ps-*"))):
            if os.path.exists(os.path.join(d, "INVALID.txt")):
                continue
            r = score_run(d, host)
            if not r:
                continue
            row, segs, polls, perf = r
            runs.append(row)
            fr.write(json.dumps(row) + "\n")
            for s, v in segs.items():
                fs.write(json.dumps({"run_id": row["run_id"], "host": host, "scenario": row["scenario"], "build": row["build"], "arm": row["arm"],
                                     "rep": row["rep"], "segment": s, "metrics": v}) + "\n"); nseg += 1
            for p in polls:
                ft.write(json.dumps(dict(run_id=row["run_id"], host=host, scenario=row["scenario"], build=row["build"], arm=row["arm"], **p)) + "\n"); npoll += 1
            for p in perf:
                fp.write(json.dumps(dict(run_id=row["run_id"], host=host, scenario=row["scenario"], build=row["build"], arm=row["arm"], **p)) + "\n"); nperf += 1
    for f in (fr, fs, ft, fp):
        f.close()
    for n in ("runs", "segments", "timelines", "mapperf"):
        os.replace(os.path.join(a.out, n + ".jsonl.tmp"), os.path.join(a.out, n + ".jsonl"))
    keys = ["run_id", "host", "scenario", "build", "arm", "rep", "jfr", "verdict", "reason", "started_utc", "fixture", "mesh_budget_mb", "perf_comparable", "texture_first_ms", "texture_verdict", "detail_timing_representative", "timing_comparison_eligible", "timing_exclusion_reason", "archive"]
    with open(os.path.join(a.out, "runs.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(keys)
        for r in runs:
            w.writerow([r.get(k) for k in keys])
    print("runs=%d segments=%d polls=%d mapperf=%d -> %s" % (len(runs), nseg, npoll, nperf, a.out))


if __name__ == "__main__":
    main()
