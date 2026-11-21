"""Turn raw per-host evidence (file listings + small inline files) into normalised run drafts.

Pure functions over `SourceView` objects, so they are unit-testable from fixtures with no network.
Each adapter returns drafts: run dicts (see model.py) plus private keys
  _mirror : [{rel, kind: shot|log|file, root, name?}]    large files the collector fetches once
  _inline : {name: text}                                  small evidence files copied into the run dir
that the collector strips before writing run.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import parsers
from .model import (canon_host, canon_verdict, new_run, parse_iso, slug, utc_iso)

LOG_CANDIDATES = ["client.log", "latest.log", "logs/latest.log", "game.log", "harness.log"]
VERDICT_BADNESS = {"FAIL": 5, "ERROR": 5, "TIMEOUT": 5, "INCOMPLETE": 4, "BLOCKED": 3, "CANCELLED": 3, "UNKNOWN": 2,
                   "RUNNING": 1, "PASS": 0}


def worst(a, b):
    return a if VERDICT_BADNESS.get(a, 2) >= VERDICT_BADNESS.get(b, 2) else b


class SourceView:
    """One source's remote listing. files: {rel: {s, m}}, text: {rel: inline text}."""

    def __init__(self, root, files, text, now, spec=None):
        self.root, self.files, self.text, self.now, self.spec = root, files, text, now, spec or {}

    def has(self, rel):
        return rel in self.files

    def get_text(self, rel):
        return self.text.get(rel)

    def json(self, rel):
        t = self.text.get(rel)
        if not t:
            return None
        try:
            return json.loads(t)
        except ValueError:
            return None

    def children(self):
        """Top level dir names that contain files."""
        return sorted({r.split("/")[0] for r in self.files if "/" in r})


class Registry:
    """jar sha256 -> {label, commit}, learned from every candidate.json / installed.json / summary seen."""

    def __init__(self, data=None):
        self.by_sha = dict(data or {})

    def learn(self, sha, label=None, commit=None):
        if not sha:
            return
        cur = self.by_sha.setdefault(sha, {"label": None, "commit": None})
        if label and not cur["label"]:
            cur["label"] = label
        if commit and not cur["commit"]:
            cur["commit"] = commit

    def learn_obj(self, o):
        if not isinstance(o, dict):
            return
        c = o.get("candidate") if isinstance(o.get("candidate"), dict) else o
        sha = c.get("sha256") or c.get("jar_sha256") or o.get("jar_sha256")
        self.learn(sha, c.get("label") or o.get("label"), c.get("source_commit") or c.get("commit") or o.get("source_commit"))

    def lookup_prefix(self, prefix):
        """(sha, entry) for a commit prefix or label seen in a request id, else (None, None)."""
        prefix = (prefix or "").lower()
        if len(prefix) < 7:
            return None, None
        for sha, e in self.by_sha.items():
            if (e.get("commit") or "").lower().startswith(prefix) or (e.get("label") or "").lower() == prefix:
                return sha, e
        return None, None

    def fill(self, cand):
        sha = cand.get("jar_sha256")
        if sha and sha in self.by_sha:
            r = self.by_sha[sha]
            cand["label"] = cand.get("label") or r["label"]
            cand["commit"] = cand.get("commit") or r["commit"]
        if not cand.get("label") and cand.get("commit"):
            cand["label"] = cand["commit"][:8]
        if not cand.get("label") and sha:
            cand["label"] = sha[:8]
        return cand


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------

def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_heap(h):
    if isinstance(h, list) and h:
        h = h[0]
    return re.sub(r"^-Xmx", "", str(h)) if h else None


def _apply_fp(run, fp, view_texts=None):
    """Merge a parsed feature-plan result into a run."""
    run["metrics"].extend(fp["metrics"])
    run["checks"].extend(fp["checks"])
    env = run["environment"]
    if fp["renderer"]:
        env["gl_renderer"] = fp["renderer"]
    if fp["gl_version"]:
        env["gl_version"] = fp["gl_version"]
    scen = env.setdefault("scenario", {})
    scen["name"] = "feature-plan"
    if fp["gates"]:
        scen["gates"] = fp["gates"]
    if fp["route"]:
        scen["route"] = fp["route"]
    if fp["qa"]:
        run["notes"].extend(["QA " + q[:200] for q in fp["qa"][:4]])
    for lim in fp["limits"]:
        run["notes"].append("LIMIT %s: %s" % (lim["name"], lim["detail"][:240]))


def _set_times(run, started, finished, duration=None):
    run["started_ts"] = started
    run["finished_ts"] = finished
    if duration is not None:
        run["duration_s"] = duration
    elif started is not None and finished is not None:
        run["duration_s"] = round(finished - started, 3)


# ----------------------------------------------------------------------------------------------
# feature-plan run dirs (CT224, fd, Mac)
# ----------------------------------------------------------------------------------------------

def build_featureplan_dirs(host, view, reg, ctx):
    drafts = []
    for d in view.children():
        names = {r[len(d) + 1:] for r in view.files if r.startswith(d + "/")}
        if not ({"input.json", "exit.json"} & names or any(n.startswith("probe/") for n in names)):
            continue
        t = lambda n: view.get_text(d + "/" + n)
        run = new_run(host, "feature-plan", "feature-plan", d)
        suffix = re.sub(r"^feature-plan-", "", d)
        run["id"] = "%s--feature-plan--%s" % (slug(host), slug(suffix))
        run["dirname"] = d
        run["evidence"]["dir"] = "%s/%s" % (view.root.rstrip("/\\"), d)
        inp = view.json(d + "/input.json") or {}
        proc = view.json(d + "/process.json") or {}
        ex = view.json(d + "/exit.json") or {}
        fv = view.json(d + "/final-verification.json") or {}
        probe_names = sorted(n for n in names if n.startswith("probe/"))
        # candidate
        sha = inp.get("jar_sha256")
        reg.learn(sha, None, fv.get("source_commit"))
        run["candidate"].update({"jar_sha256": sha, "commit": fv.get("source_commit")})
        # times
        started = proc.get("started")
        if started is None and view.files.get(d + "/input.json"):
            started = view.files[d + "/input.json"]["m"]
        fin_m = view.files.get(d + "/exit.json", {}).get("m")
        elapsed = ex.get("elapsed_seconds")
        finished = (started + elapsed) if (started is not None and isinstance(elapsed, (int, float))) else fin_m
        _set_times(run, started, finished, elapsed if isinstance(elapsed, (int, float)) else None)
        # verdict
        result = t("probe/result.txt")
        incomplete = t("probe/INCOMPLETE.txt")
        harness_inc = t("probe/HARNESS_INCOMPLETE.txt")
        fp = None
        if result:
            fp = parsers.parse_featureplan_result(result)
            run["_inline"] = {"result.txt": result}
        elif incomplete:
            fp = parsers.parse_featureplan_result(incomplete)
            run["_inline"] = {"INCOMPLETE.txt": incomplete}
        if fp:
            run["verdict_raw"], run["verdict"], run["reason"] = fp["verdict_raw"], fp["verdict"], fp["reason"]
            _apply_fp(run, fp)
        elif harness_inc:
            run["verdict_raw"], run["verdict"] = "INCOMPLETE", "INCOMPLETE"
            run["reason"] = harness_inc.strip().splitlines()[0][:300]
            run["_inline"] = {"HARNESS_INCOMPLETE.txt": harness_inc}
        elif ex:
            run["verdict_raw"] = "EXIT_%s" % ex.get("exit_code")
            run["verdict"] = "TIMEOUT" if ex.get("exit_code") == 124 else "ERROR"
            run["reason"] = "client exited %s without a feature-plan result" % ex.get("exit_code")
        else:
            newest = max([view.files[d + "/" + n]["m"] for n in names] or [0])
            if view.now - newest < 180:
                run["verdict_raw"], run["verdict"] = "RUNNING", "RUNNING"
            else:
                run["verdict_raw"], run["verdict"] = None, "UNKNOWN"
                run["reason"] = "no exit.json and no result: client was stopped or the node lost the run"
        if ex:
            run["exit"] = ex
            if ex.get("exit_code") == 124 and run["verdict"] != "PASS":
                run["verdict"] = "TIMEOUT" if run["verdict"] in ("INCOMPLETE", "UNKNOWN") else run["verdict"]
                run["reason"] = run["reason"] or "bounded runtime timeout (exit 124)"
        note = t("fixture-gate-note.txt")
        if note:
            run["notes"].append("fixture gate note: " + note.strip()[:300])
        hi = t("probe/HARNESS_INCOMPLETE.txt")
        if hi and fp:
            run["notes"].append("harness: " + hi.strip()[:200])
        if fv.get("visual_review"):
            run["notes"].append("visual review: " + str(fv["visual_review"])[:300])
        # progress timeline
        prog = t("probe/progress.txt")
        if prog:
            run["timeline"] = parsers.parse_progress_txt(prog)
            run["_inline"] = dict(run.get("_inline") or {}, **{"progress.txt": prog})
        # environment
        env = run["environment"]
        gi = inp.get("gpu_identity") or {}
        env.update({"gl_renderer": env.get("gl_renderer") or gi.get("renderer"), "gl_version": env.get("gl_version") or gi.get("version"),
                    "gl_vendor": gi.get("vendor"), "max_heap": _strip_heap(inp.get("heap")),
                    "manifest_sha256": inp.get("manifest_sha256"),
                    "settings": {"play_profile": inp.get("play_profile_argument"),
                                 "exclusions": sorted(inp.get("test_client_exclusions") or []),
                                 "network": inp.get("network"), "fixture_repair": inp.get("fixture_repair_approved")}})
        scen = env.setdefault("scenario", {"name": "feature-plan"})
        scen.setdefault("name", "feature-plan")
        scen["fixture"] = inp.get("fixture")
        scen["level_sha256"] = inp.get("source_level_sha256")
        scen["timeout_seconds"] = inp.get("timeout_seconds")
        # mirror list
        mir = []
        if "client.log" in names:
            mir.append({"rel": d + "/client.log", "kind": "log", "root": view.root})
        for n in probe_names:
            if n.lower().endswith(".png"):
                mir.append({"rel": d + "/" + n, "kind": "shot", "root": view.root, "name": os.path.basename(n)})
        run["_mirror"] = mir
        run["evidence"]["files"] = sorted(n for n in names if not n.startswith("probe/") or not n.endswith(".png"))
        run["evidence"]["screenshot_files"] = len([n for n in probe_names if n.lower().endswith(".png")])
        drafts.append(run)
        ctx.setdefault("by_dir", {})[(host, d)] = run
    return drafts


# ----------------------------------------------------------------------------------------------
# CT224 smoke v2 (enrichment only)
# ----------------------------------------------------------------------------------------------

def learn_smoke_v2(host, view, reg):
    reg.learn_obj(view.json("candidate.json"))
    for rel in view.files:
        if rel.endswith("/summary.json"):
            s = view.json(rel) or {}
            if s.get("jar_sha256"):
                reg.learn(s["jar_sha256"], None, None)


def build_smoke_v2(host, view, reg, ctx):
    """Attach the wrapper summary to its feature-plan dir; refused/failed wrapper runs with no evidence dir are
    covered by the Dev Lab job record, so no separate run is made for them."""
    for rel in sorted(view.files):
        if not rel.endswith("/summary.json"):
            continue
        s = view.json(rel)
        if not isinstance(s, dict):
            continue
        p = parsers.parse_smoke_ct224(s)
        ed = p.get("evidence_dir")
        run = ctx.get("by_dir", {}).get((host, os.path.basename(ed.rstrip("/\\")))) if ed else None
        if run is None:
            if (p.get("verdict") or "") != "PASS":
                ctx.setdefault("smoke_notes", []).append({"host": host, "dir": rel.split("/")[0], "status": p.get("verdict_raw"),
                                                         "wrapper_log": view.get_text(rel.split("/")[0] + "/wrapper.log"),
                                                         "seconds": s.get("seconds"), "mtime": view.files[rel]["m"]})
            continue
        run["smoke"] = {"wrapper_exit": p["wrapper_exit"], "seconds": p["duration_s"], "status": p["verdict_raw"],
                        "candidate_jar_path": s.get("candidate_jar")}
        if p.get("harness_exit"):
            run["environment"]["settings"] = dict(run["environment"].get("settings") or {})
        run["verdict"] = worst(run["verdict"], p["verdict"]) if p["verdict"] != "UNKNOWN" else run["verdict"]
    return []


# ----------------------------------------------------------------------------------------------
# fd runs
# ----------------------------------------------------------------------------------------------

def learn_fd_runs(host, view, reg):
    for rel in view.files:
        if rel.endswith("/summary.json"):
            reg.learn_obj(view.json(rel))


def build_fd_runs(host, view, reg, ctx):
    drafts = []
    for d in view.children():
        s = view.json(d + "/summary.json")
        if not isinstance(s, dict):
            continue
        p = parsers.parse_smoke_fd(s)
        script = p.get("smoke_script") or ""
        ed = (s.get("evidence_dir") or "").rstrip("/\\")
        target = ctx.get("by_dir", {}).get((host, os.path.basename(ed))) if ed else None
        probe_texts = {n: view.get_text(d + "/" + n) for n in [os.path.basename(r) for r in view.files if r.startswith(d + "/result-")]}
        if target is not None:
            # enrich the feature-plan run that carries the PNGs and the client log
            target["source"] = "feature-plan+fd"
            target["fd_run_id"] = d
            target["checks"].extend(p["checks"])
            for m in p["metrics"]:
                if not any(x["name"] == m["name"] for x in target["metrics"]):
                    target["metrics"].append(m)
            c = p["candidate"]
            target["candidate"].update({k: v for k, v in c.items() if v and not target["candidate"].get(k)})
            env = target["environment"]
            env["gl_renderer"] = env.get("gl_renderer") or p["gl_renderer"]
            env["gl_vendor"] = env.get("gl_vendor") or p["gl_vendor"]
            target["verdict"] = worst(target["verdict"], p["verdict"])
            if p["verdict"] != "PASS" and not target["reason"]:
                target["reason"] = "; ".join(p["failures"])[:300] or None
            target["_inline"] = dict(target.get("_inline") or {}, **{"fd-summary.json": view.get_text(d + "/summary.json")})
            continue
        gate = "feature-plan" if "feature-plan" in script else "smoke"
        run = new_run(host, gate, "fd-smoke", d)
        run["run_id_raw"] = d
        run["id"] = "%s--%s--%s" % (slug(host), gate, slug(d))
        run["verdict_raw"], run["verdict"] = p["verdict_raw"], p["verdict"]
        run["candidate"].update(p["candidate"])
        _set_times(run, p["started_ts"], p["finished_ts"], p["duration_s"])
        run["checks"].extend(p["checks"])
        run["metrics"].extend(p["metrics"])
        label = p.get("run_label") or d
        args = p.get("args") or []
        run["environment"].update({"gl_renderer": p["gl_renderer"], "gl_version": p["gl_version"], "gl_vendor": p["gl_vendor"],
                                   "max_heap": _strip_heap(p["max_heap"]),
                                   "scenario": {"name": label, "sha256": _sha(" ".join(args)) if args else None,
                                                "args": " ".join(args)}})
        if p["failures"]:
            run["reason"] = "; ".join(str(f) for f in p["failures"])[:400]
        for n, txt in probe_texts.items():
            if txt:
                word = txt.split(None, 1)[0] if txt.strip() else "?"
                run["checks"].append({"name": "probe " + n.replace("result-", "").replace(".txt", ""),
                                      "status": word if word in ("PASS", "FAIL", "INCOMPLETE") else "INFO",
                                      "detail": txt.strip()[:300]})
        run["_inline"] = {"summary.json": view.get_text(d + "/summary.json")}
        mir = []
        if view.has(d + "/smoke-direct.log"):
            mir.append({"rel": d + "/smoke-direct.log", "kind": "log", "root": view.root})
        run["_mirror"] = mir
        run["evidence"]["files"] = sorted(r[len(d) + 1:] for r in view.files if r.startswith(d + "/"))
        drafts.append(run)
    return drafts


# ----------------------------------------------------------------------------------------------
# GPC / Mac node summaries
# ----------------------------------------------------------------------------------------------

def learn_node_smoke(host, view, reg):
    for rel in view.files:
        if rel.endswith(".json"):
            reg.learn_obj(view.json(rel))


def build_node_smoke(host, view, reg, ctx):
    seen = {}
    for rel in sorted(view.files):
        if not rel.endswith(".json") or rel == "installed.json":
            continue
        s = view.json(rel)
        if not isinstance(s, dict) or not (s.get("status") and (s.get("run_id") or s.get("started_utc"))):
            continue
        key = s.get("run_id") or rel
        # keep the copy with the most information (current file beats history copy)
        if key in seen and view.files[rel]["m"] < seen[key][1]:
            continue
        seen[key] = (s, view.files[rel]["m"], rel)
    drafts = []
    for key, (s, m, rel) in seen.items():
        p = parsers.parse_smoke_gpc(s)
        gate = "dry-run" if p["dry_run"] else ("feature-plan" if p.get("mode") == "feature-plan" else "smoke")
        stamp = re.sub(r"[^0-9A-Za-z]+", "", str(key))
        run = new_run(host, gate, "node-smoke", stamp)
        run["id"] = "%s--%s--%s" % (slug(host), gate, slug(key))
        run["run_id_raw"] = key
        run["verdict_raw"], run["verdict"], run["reason"] = p["verdict_raw"], p["verdict"], p["reason"]
        run["candidate"].update(p["candidate"])
        started = p["started_ts"] or (p["finished_ts"] - 2 if p["finished_ts"] else None)
        _set_times(run, started, p["finished_ts"], p["duration_s"])
        run["metrics"].extend(p["metrics"])
        run["checks"].extend(p["checks"])
        env = run["environment"]
        env["gl_renderer"] = p["gl_renderer"]
        if p["gpus"]:
            env["gpus"] = p["gpus"]
        hw = p.get("hardware") or {}
        if hw:
            env["cpu"] = hw.get("cpu")
            env["os"] = ("macOS " + str(hw.get("macos"))) if hw.get("macos") else None
            env["model"] = hw.get("model")
        env["scenario"] = {"name": gate if gate != "feature-plan" else "feature-plan",
                           "fixture": (p.get("world") or p.get("target") or "").replace("world:", "") or None}
        env["settings"] = {"profile": p.get("profile")} if p.get("profile") else {}
        if p.get("idle_minutes") is not None:
            run["notes"].append("owner desktop idle %.1f min at start" % p["idle_minutes"])
        if p.get("crash_marker_line"):
            run["notes"].append("crash marker: " + str(p["crash_marker_line"])[:200])
        if p.get("last_diag_line"):
            run["notes"].append("last diag: " + str(p["last_diag_line"])[:200])
        if p.get("feature_plan_result"):
            fp = parsers.parse_featureplan_result(p["feature_plan_result"])
            _apply_fp(run, fp)
            if p["verdict"] == "PASS" and fp["verdict"] != "UNKNOWN":
                run["verdict"] = worst(run["verdict"], fp["verdict"])
        run["_inline"] = {"summary.json": view.get_text(rel)}
        run["_mirror"] = []
        run["_log_mtime_window"] = True
        drafts.append(run)
    # latest.log belongs to the newest real (non-dry) run whose window contains the log's mtime
    log = view.files.get("latest.log")
    if log:
        cands = [r for r in drafts if r["gate"] != "dry-run" and r["started_ts"] and r["finished_ts"]
                 and r["started_ts"] - 60 <= log["m"] <= r["finished_ts"] + 900]
        if cands:
            tgt = max(cands, key=lambda r: r["finished_ts"])
            tgt["_mirror"].append({"rel": "latest.log", "kind": "log", "root": view.root})
    return drafts


def build_instance_fp(host, view, reg, ctx):
    """Single dir overwritten each run: attach to the node-smoke run whose window contains the files."""
    if not view.files:
        return []
    newest = max(f["m"] for f in view.files.values())
    cands = [r for r in ctx.get("drafts", {}).get(host, []) if r["gate"] in ("feature-plan", "smoke") and r["started_ts"]
             and r["finished_ts"] and r["started_ts"] - 60 <= newest <= r["finished_ts"] + 300]
    if not cands:
        return []
    run = max(cands, key=lambda r: r["finished_ts"])
    result = view.get_text("result.txt") or view.get_text("INCOMPLETE.txt")
    if result:
        fp = parsers.parse_featureplan_result(result)
        if fp["verdict"] != "UNKNOWN":
            run["gate"] = "feature-plan"
            _apply_fp(run, fp)
            if fp["verdict"] != "PASS" and not run.get("reason"):
                run["reason"] = fp["reason"]
            run["verdict"] = worst(run["verdict"], fp["verdict"]) if run["verdict"] != "UNKNOWN" else fp["verdict"]
        run["_inline"] = dict(run.get("_inline") or {}, **{("result.txt" if view.get_text("result.txt") else "INCOMPLETE.txt"): result})
    prog = view.get_text("progress.txt")
    if prog:
        run["timeline"] = parsers.parse_progress_txt(prog)
        run["_inline"] = dict(run.get("_inline") or {}, **{"progress.txt": prog})
    for rel in sorted(view.files):
        if rel.lower().endswith(".png"):
            run["_mirror"].append({"rel": rel, "kind": "shot", "root": view.root, "name": os.path.basename(rel)})
    return []


# ----------------------------------------------------------------------------------------------
# c39 validations
# ----------------------------------------------------------------------------------------------

def build_c39(host, view, reg, ctx):
    latest = view.json("latest.json") or {}
    stats_by_sha = {p.get("packet_sha256"): p.get("stats") for p in latest.get("packets") or []}
    out_host = view.spec.get("host_override") or host
    drafts = []
    for rel in sorted(view.files):
        if rel == "latest.json" or not rel.endswith(".json"):
            continue
        v = view.json(rel)
        if not isinstance(v, dict) or v.get("schema") != "mcdevlab.c39-validation.v1":
            continue
        p = parsers.parse_c39_validation(v, stats_by_sha.get(v.get("packet_sha256")))
        sha = (v.get("packet_sha256") or rel)[:12]
        run = new_run(out_host, "c39-frame-metrics", "c39", sha)
        run["id"] = "%s--c39-frame-metrics--%s" % (slug(out_host), slug(sha))
        run["kind"] = "test"
        run["verdict_raw"], run["verdict"], run["reason"] = p["verdict_raw"], p["verdict"], p["reason"]
        if p["verdict"] == "BLOCKED" and not p["reason"]:
            run["reason"] = "packet not validated"
        run["candidate"].update({"commit": p.get("producer"), "label": (p.get("producer") or "")[:8] or None})
        _set_times(run, p["started_ts"], p["started_ts"], 0)
        run["metrics"].extend(p["metrics"])
        run["environment"] = {"matched_comparison_ok": p["matched_comparison_ok"], "scenario": {"name": "c39 map_open frame metrics"},
                              "gl_renderer": None}
        for k in ("scope", "wire_format", "stable_whole_window", "gpu_time", "pixel_coverage"):
            if p["labels"].get(k):
                run["notes"].append("%s: %s" % (k, p["labels"][k]))
        c = p.get("consumer") or {}
        if c.get("commit"):
            run["notes"].append("validated by consumer %s" % c["commit"])
        run["checks"].append({"name": "validation", "status": "PASS" if p["verdict"] == "PASS" else "FAIL", "detail": str(p.get("verdict_raw"))})
        run["_inline"] = {"validation.json": view.get_text(rel)}
        run["_mirror"] = []
        drafts.append(run)
    return drafts


# ----------------------------------------------------------------------------------------------
# candidate archive (ctl/archive/<commit>/candidate.json) - registry only
# ----------------------------------------------------------------------------------------------

def learn_candidate_archive(host, view, reg):
    for rel in view.files:
        if rel.endswith("candidate.json"):
            reg.learn_obj(view.json(rel))


def build_candidate_archive(host, view, reg, ctx):
    return []


# ----------------------------------------------------------------------------------------------
# generic harness runs
# ----------------------------------------------------------------------------------------------

def build_harness(host, view, reg, ctx):
    """Directories holding result.json (mcdevlab.harness.result.v1) and/or progress.jsonl."""
    dirs = {}
    for rel, f in view.files.items():
        base = os.path.basename(rel)
        d = os.path.dirname(rel)
        if base in ("result.json", "progress.jsonl"):
            dirs.setdefault(d, {})[base] = f
    drafts = []
    for d, found in sorted(dirs.items()):
        def rel_in(name, d=d):
            return (d + "/" + name) if d else name
        res_text = view.get_text(rel_in("result.json"))
        prog_text = view.get_text(rel_in("progress.jsonl"))
        obj = None
        if res_text:
            try:
                obj = json.loads(res_text)
            except ValueError:
                obj = None
        stamp = d.replace("/", "-") or "root"
        if obj is not None:
            f = parsers.harness_to_run_fields(obj)
            rhost = canon_host(f["host_raw"], host)
            gate = (f["scenario"]["name"] or "harness") if f["scenario"]["name"] else "harness"
            run = new_run(rhost, gate, "harness", stamp)
            problems = parsers.validate_harness_result(obj)
            for pr in problems:
                run["notes"].append("contract: " + pr)
            for k in ("verdict_raw", "verdict", "reason", "candidate", "started_ts", "finished_ts", "duration_s", "environment",
                      "metrics", "series", "steps", "checks", "log_markers", "exit"):
                run[k] = f[k]
            run["environment"]["scenario"] = f["scenario"]
            run["environment"].setdefault("matched_comparison_ok", None)
            if f["host_raw"]:
                run["environment"]["node"] = f["host_raw"]
            reg.learn(f["candidate"].get("jar_sha256"), f["candidate"].get("label"), f["candidate"].get("commit"))
            shots = []
            mir = []
            for s in f["screenshots"]:
                fname = s["file"].replace("\\", "/")
                shots.append({"name": s["name"], "step": s.get("step"), "width": s.get("width"), "height": s.get("height"),
                              "src": fname})
                mir.append({"rel": rel_in(fname), "kind": "shot", "root": view.root, "name": s["name"]})
            run["_shots_meta"] = shots
            run["_inline"] = {"result.json": res_text}
        else:
            # progress only: running now, or the harness died before writing result.json
            rhost = host
            run = new_run(rhost, "harness", "harness", stamp)
            samples = parsers.parse_progress_jsonl(prog_text or "")
            last = samples[-1] if samples else {}
            newest = found.get("progress.jsonl", {}).get("m", 0)
            if view.now - newest < 120:
                run["verdict_raw"], run["verdict"] = "RUNNING", "RUNNING"
            else:
                run["verdict_raw"], run["verdict"] = None, "UNKNOWN"
                run["reason"] = "progress.jsonl stopped updating and no result.json was written (harness crashed or was killed)"
            run["started_ts"] = newest - (last.get("t_ms", 0) / 1000.0) if samples else None
            run["finished_ts"] = newest if run["verdict"] != "RUNNING" else None
            run["environment"] = {"scenario": {"name": "harness"}}
            mir = []
        if prog_text:
            samples = parsers.parse_progress_jsonl(prog_text)
            if samples:
                run["progress"] = parsers.progress_series(samples)
                run["_inline"] = dict(run.get("_inline") or {})
        # logs
        for cand in LOG_CANDIDATES:
            if view.has(rel_in(cand)):
                mir.append({"rel": rel_in(cand), "kind": "log", "root": view.root})
                break
        run["_mirror"] = mir
        run["id"] = "%s--%s--%s" % (slug(run["host"]), slug(run["gate"]), slug(stamp))
        run["evidence"]["dir"] = "%s/%s" % (view.root.rstrip("/\\"), d)
        drafts.append(run)
    return drafts


# ----------------------------------------------------------------------------------------------
# Dev Lab jobs: merge with evidence, create job-only runs
# ----------------------------------------------------------------------------------------------

def _overlap(job, run, slack=180):
    if run["started_ts"] is None:
        return None
    js = job["started"] or job["created"]
    jf = job["finished"] or (js + 3600 * 3)
    if run["started_ts"] < js - slack or run["started_ts"] > jf + slack:
        return None
    return abs(run["started_ts"] - js)


def merge_jobs(host, drafts, jobs, now, job_logs=None, reg=None):
    """Attach Dev Lab job records to evidence runs and create runs for jobs without evidence.

    Matching is global and greedy on start-time distance (so an instantly refused job next to a real run never
    steals it): a job and a run pair up when the run started inside the job's window (plus slack), the job
    is a client/dry-run recipe, and neither is already paired.
    """
    job_logs = job_logs or {}
    parsed = [parsers.parse_job(j) for j in jobs]
    runs = list(drafts)
    pairs = []
    for j in parsed:
        is_dry = "dryrun" in (j["recipe"] or "")
        if j["kind"] != "client" and not is_dry:
            continue
        for r in drafts:
            if (r["gate"] == "dry-run") != is_dry:
                continue
            d = _overlap(j, r)
            if d is not None:
                pairs.append((d, j["created"] or 0, id(j), id(r), j, r))
    used_j, used_r = set(), set()
    for d, _c, jid, rid, j, r in sorted(pairs, key=lambda x: (x[0], x[1])):
        if jid in used_j or rid in used_r:
            continue
        used_j.add(jid)
        used_r.add(rid)
        _attach_job(r, j, job_logs.get(j["job_id"]))
        j["_matched"] = True
    for j in parsed:
        if j.get("_matched"):
            continue
        run = new_run(host, j["recipe"] or "job", "dev-lab-job", j["job_id"][-12:] if j["job_id"] else "job")
        run["id"] = "%s--%s--%s" % (slug(host), slug(j["recipe"] or "job"), slug((j["job_id"] or "job").replace("job-", "")[:12]))
        run["kind"] = j["kind"]
        _attach_job(run, j, job_logs.get(j["job_id"]))
        run["verdict_raw"] = j["state"]
        run["verdict"] = parsers.job_verdict(j)
        run["started_ts"] = j["started"]
        run["finished_ts"] = j["finished"]
        if j["started"] and j["finished"]:
            run["duration_s"] = round(j["finished"] - j["started"], 3)
        run["reason"] = _job_reason(j, job_logs.get(j["job_id"])) if run["verdict"] != "PASS" else None
        run["environment"] = {"scenario": {"name": j["recipe"]}}
        hint = re.search(r"-([0-9a-f]{7,40})(?:-\d+)?$", j.get("request_id") or "")
        if hint and reg is not None:
            sha, ent = reg.lookup_prefix(hint.group(1))
            run["candidate"].update({"label": (ent or {}).get("label") or hint.group(1)[:8], "commit": (ent or {}).get("commit"),
                                     "jar_sha256": sha, "inferred": "from request id"})
        run["_mirror"] = []
        runs.append(run)
    _attribute_gates(runs)
    return runs


def _attribute_gates(runs):
    """A refused/failed client job leaves no evidence, so it carries the recipe name as its gate. Where the same
    recipe produced evidence runs, attribute its later failed attempts to that evidence gate so the history of
    one gate includes the attempts that never started. Attempts from before the recipe first produced evidence
    (an older recipe version) keep the recipe name."""
    first, gate_of = {}, {}
    for r in runs:
        if r["source"] != "dev-lab-job" and r.get("recipe") and r.get("started_ts") and r["gate"] != "dry-run":
            k = r["recipe"]
            gate_of.setdefault(k, {})
            gate_of[k][r["gate"]] = gate_of[k].get(r["gate"], 0) + 1
            first[k] = min(first.get(k, 1e18), r["started_ts"])
    for r in runs:
        if r["source"] == "dev-lab-job" and r.get("kind") == "client" and r.get("recipe") in gate_of:
            k = r["recipe"]
            if (r.get("started_ts") or 0) >= first[k] - 300:
                r["gate"] = max(gate_of[k], key=gate_of[k].get)
                r["environment"] = {"scenario": {"name": r["gate"]}}


def _job_reason(j, log):
    if j.get("error_message"):
        return "%s: %s" % (j.get("error_type"), j["error_message"])[:400]
    if log:
        r = parsers.extract_reason(log)
        if r:
            return r
    if j.get("exit_code") not in (None, 0):
        return "exit code %s" % j["exit_code"]
    return None


def _attach_job(run, j, log):
    run["job_id"] = j["job_id"]
    run["request_id"] = j["request_id"]
    run["recipe"] = j["recipe"]
    run["kind"] = j["kind"] if run["source"] == "dev-lab-job" else run["kind"]
    run["job"] = {"id": j["job_id"], "request_id": j["request_id"], "instance": j["instance"], "state": j["state"],
                  "recipe": j["recipe"], "kind": j["kind"], "exit_code": j["exit_code"], "error_type": j["error_type"],
                  "error_message": j["error_message"], "artifacts": j["artifacts"], "created": j["created"],
                  "started": j["started"], "finished": j["finished"], "log_bytes": j["log_bytes"]}
    if run["source"] == "dev-lab-job":
        return
    jv = parsers.job_verdict(j)
    ev = run["verdict"]
    if j["state"] in ("RUNNING", "QUEUED") and ev in ("UNKNOWN", "RUNNING"):
        run["verdict"], run["verdict_raw"] = "RUNNING", "RUNNING"
        return
    if jv in ("FAIL", "ERROR", "BLOCKED", "CANCELLED", "UNKNOWN") and ev == "PASS":
        run["notes"].append("evidence says PASS but the Dev Lab job is %s (%s): %s" % (
            j["state"], j["error_type"] or "no error type", (j["error_message"] or "")[:200]))
        run["verdict"] = jv
        run["reason"] = _job_reason(j, log)
    elif jv in ("FAIL", "ERROR", "BLOCKED", "CANCELLED") and ev in ("UNKNOWN",):
        run["verdict"] = jv
        run["reason"] = run.get("reason") or _job_reason(j, log)
    elif jv in ("FAIL", "ERROR", "BLOCKED", "CANCELLED") and not run.get("reason"):
        run["reason"] = _job_reason(j, log)
    if jv in ("FAIL", "ERROR", "BLOCKED", "CANCELLED") and ev != "PASS":
        run["notes"].append("Dev Lab job %s: %s" % (j["state"], _job_reason(j, log) or "no detail"))


# ----------------------------------------------------------------------------------------------
# dispatch
# ----------------------------------------------------------------------------------------------

LEARNERS = {"smoke_v2": learn_smoke_v2, "fd_runs": learn_fd_runs, "node_smoke": learn_node_smoke,
            "candidate_archive": learn_candidate_archive}
BUILDERS = [("featureplan_dirs", build_featureplan_dirs), ("smoke_v2", build_smoke_v2), ("fd_runs", build_fd_runs),
            ("node_smoke", build_node_smoke), ("instance_fp", build_instance_fp), ("c39", build_c39),
            ("harness", build_harness), ("candidate_archive", build_candidate_archive)]


def build_host(host, views, reg, jobs=None, job_logs=None, now=None):
    """views: list of (source_spec, SourceView). Returns (runs, ctx)."""
    drafts = []
    ctx = {"drafts": {host: drafts}, "by_dir": {}}
    for spec, view in views:
        fn = LEARNERS.get(spec["type"])
        if fn:
            fn(host, view, reg)
    for stype, fn in BUILDERS:
        for spec, view in views:
            if spec["type"] == stype:
                drafts.extend(fn(host, view, reg, ctx) or [])
    seen, uniq = set(), []
    for r in drafts:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    runs = merge_jobs(host, uniq, jobs or [], now or 0, job_logs, reg)
    for r in runs:
        reg.fill(r["candidate"])
    return runs, ctx
