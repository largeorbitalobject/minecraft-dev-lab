"""API logic over the collector's data directory. Pure functions, no HTTP, no network.

Everything here reads files the collector wrote; nothing here talks to a fleet node.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict

from . import compare as cmp
from . import logs as logmod
from .model import metric_key
from .store import Store

CLIENT_KINDS = ("client",)
FINISHED = ("PASS", "FAIL", "ERROR", "TIMEOUT", "INCOMPLETE")

# run.json fields only needed for one run's detail / compare. The index keeps every run without them
# (they are ~85% of run.json) and loads full runs on demand into a size-bounded LRU.
HEAVY_KEYS = ("metrics", "progress", "steps", "series")
DERIVED = "_v"  # index-only key: values computed from the heavy fields at load time


def _short_reason(r, n=220):
    return (r[:n] + "...") if r and len(r) > n else r


class App:
    def __init__(self, data_dir, config=None):
        self.store = Store(data_dir)
        self.config = config or {}
        self._runs = {}      # id -> (mtime, light run): run.json minus HEAVY_KEYS, see _light()
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._diff_cache = {}
        self._full = OrderedDict()   # id -> (mtime, full run, run.json bytes), LRU
        self._full_bytes = 0
        self._full_budget = int(float(self.config.get("run_cache_mb", 48)) * 1024 * 1024)
        self._full_lock = threading.Lock()
        self._mods = {}      # interned compact mod lists, shared by every run with the same list

    # -- index ------------------------------------------------------------------------------------
    def refresh(self, force=False):
        now = time.time()
        with self._lock:
            if not force and now - self._last_refresh < 1.0:
                return
            self._last_refresh = now
            seen = set()
            base = self.store.p("runs")
            try:
                names = os.listdir(base)
            except OSError:
                names = []
            for rid in names:
                path = os.path.join(base, rid, "run.json")
                try:
                    mt = os.stat(path).st_mtime
                except OSError:
                    continue
                seen.add(rid)
                cur = self._runs.get(rid)
                if cur is None or cur[0] != mt:
                    run = self.store.read_json(path)
                    if run:
                        self._runs[rid] = (mt, self._light(run))
            for rid in list(self._runs):
                if rid not in seen:
                    del self._runs[rid]
            if len(self._mods) > 2 * len(self._runs) + 64:
                self._mods = {}

    def _light(self, run):
        """Index copy of a run: no HEAVY_KEYS, environment.mods reduced to what compare_env reads (id, str(version)),
        plus DERIVED values the list endpoints need from the heavy fields. Never returned by the API as-is."""
        light = {k: v for k, v in run.items() if k not in HEAVY_KEYS}
        env = run.get("environment")
        if isinstance(env, dict) and env.get("mods"):
            env = dict(env)
            compact = tuple((m["id"], str(m.get("version"))) for m in env["mods"] if isinstance(m, dict) and m.get("id"))
            mods = self._mods.get(compact)
            if mods is None:
                mods = self._mods[compact] = [{"id": i, "version": v} for i, v in compact]
            env["mods"] = mods
            light["environment"] = env
        light[DERIVED] = {"n_metrics": len(run.get("metrics") or []), "key_metrics": cmp.key_metrics(run)}
        return light

    def all_runs(self):
        """Light runs (see _light). Use full() for metrics/progress/steps/series."""
        self.refresh()
        return [r for _m, r in self._runs.values()]

    def get_light(self, rid):
        self.refresh()
        cur = self._runs.get(rid)
        return cur[1] if cur else None

    def get_run(self, rid):
        """The full run.json, through the size-bounded LRU."""
        self.refresh()
        cur = self._runs.get(rid)
        if cur is None:
            return None
        mt = cur[0]
        with self._full_lock:
            hit = self._full.get(rid)
            if hit is not None and hit[0] == mt:
                self._full.move_to_end(rid)
                return hit[1]
        path = self.store.p("runs", rid, "run.json")
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            run = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not run:
            return None
        with self._full_lock:
            old = self._full.pop(rid, None)
            if old is not None:
                self._full_bytes -= old[2]
            self._full[rid] = (mt, run, len(raw))
            self._full_bytes += len(raw)
            while self._full_bytes > self._full_budget and len(self._full) > 1:
                _rid, ent = self._full.popitem(last=False)
                self._full_bytes -= ent[2]
        return run

    def full(self, run):
        """Full version of a light (or already full) run."""
        if run is None or DERIVED not in run:
            return run
        return self.get_run(run["id"]) or run

    @staticmethod
    def _n_metrics(run):
        d = run.get(DERIVED)
        return d["n_metrics"] if d is not None else len(run.get("metrics") or [])

    @staticmethod
    def _key_metrics(run):
        d = run.get(DERIVED)
        return d["key_metrics"] if d is not None else cmp.key_metrics(run)

    # -- summaries --------------------------------------------------------------------------------
    @staticmethod
    def has_evidence(run):
        return run.get("source") != "dev-lab-job"

    @staticmethod
    def is_gate(run):
        """A run worth comparing: a client test (not a candidate push / install / status op)."""
        return run.get("kind") == "client" or bool(App._n_metrics(run)) or run.get("source") in ("harness", "c39")

    def summary(self, run, with_key=True):
        env = run.get("environment") or {}
        s = {
            "id": run["id"], "host": run["host"], "gate": run["gate"], "recipe": run.get("recipe"), "kind": run.get("kind"),
            "source": run.get("source"), "verdict": run["verdict"], "verdict_raw": run.get("verdict_raw"),
            "reason": _short_reason(run.get("reason")), "started_ts": run.get("started_ts"),
            "finished_ts": run.get("finished_ts"), "duration_s": run.get("duration_s"), "candidate": run.get("candidate"),
            "request_id": run.get("request_id"), "job_id": run.get("job_id"),
            "gl_renderer": env.get("gl_renderer"), "os": env.get("os"),
            "n_metrics": self._n_metrics(run), "n_checks": len(run.get("checks") or []),
            "n_shots": len(run.get("screenshots") or []), "has_log": bool(run.get("log")),
            "exception": ((run.get("crash") or {}).get("exception")), "fatal": bool((run.get("crash") or {}).get("fatal")),
            "is_gate": self.is_gate(run),
        }
        if with_key:
            s["key_metrics"] = [{"key": metric_key(m), "name": m["name"], "segment": m.get("segment"), "unit": m.get("unit"),
                                 "value": m["value"], "better": m.get("better"), "limit": m.get("limit")}
                                for m in self._key_metrics(run)]
        return s

    # -- fleet board ------------------------------------------------------------------------------
    def fleet(self):
        self.refresh()
        state = self.store.read_json(self.store.p("state.json"), {}) or {}
        hosts = []
        runs = self.all_runs()
        by_host = {}
        for r in runs:
            by_host.setdefault(r["host"], []).append(r)
        host_files = sorted(f[:-5] for f in os.listdir(self.store.p("hosts")) if f.endswith(".json"))
        order = {"ctl": 0, "proxmox-test": 1, "amd": 2, "gpc": 3, "mac": 4}
        for hid in sorted(set(host_files) | set(by_host), key=lambda h: (order.get(h, 9), h)):
            hs = self.store.read_json(self.store.p("hosts", hid + ".json"), {}) or {"host": hid, "state": "unknown"}
            hruns = sorted(by_host.get(hid, []), key=lambda r: r.get("started_ts") or 0, reverse=True)
            recipes = {}
            for r in hruns:
                key = r.get("recipe") or r["gate"]
                if r["verdict"] == "RUNNING" or key in recipes:
                    continue
                recipes[key] = {"key": key, "gate": r["gate"], "kind": r.get("kind"), "verdict": r["verdict"],
                                "run_id": r["id"], "label": (r.get("candidate") or {}).get("label"),
                                "ts": r.get("finished_ts") or r.get("started_ts"), "reason": _short_reason(r.get("reason"), 140)}
            rec_list = sorted(recipes.values(), key=lambda x: (x["kind"] != "client", -(x["ts"] or 0)))
            live = self.store.read_json(self.store.p("live", hid + ".json"), None)
            live_short = None
            if live and live.get("active"):
                live_short = {"kind": live.get("kind"), "job": live.get("job"), "updated_ts": live.get("updated_ts"),
                              "last": live.get("last"), "phase": (live.get("timeline") or [{}])[-1].get("phase") if live.get("timeline") else
                              (live.get("last") or {}).get("phase")}
            last_gate = next((r for r in hruns if self.is_gate(r) and r["verdict"] != "RUNNING"), None)
            hosts.append({
                "id": hid, "label": hs.get("label", hid), "platform": hs.get("platform"), "hardware": hs.get("hardware"),
                "owner_machine": hs.get("owner_machine"), "state": hs.get("state", "unknown"), "job": hs.get("job"),
                "paused_by": hs.get("paused_by"), "candidate": hs.get("candidate"), "checked_ts": hs.get("checked_ts"),
                "last_seen_ts": hs.get("last_seen_ts"), "api_error": hs.get("api_error"),
                "evidence_error": hs.get("evidence_error"), "source_errors": hs.get("source_errors"),
                "wrapper_refusals": hs.get("wrapper_refusals"),
                "run_count": len(hruns), "recipes": rec_list, "live": live_short,
                "last_gate_run": self.summary(last_gate, with_key=False) if last_gate else None,
            })
        return {"generated_ts": time.time(), "collector": state, "hosts": hosts, "total_runs": len(runs)}

    # -- run lists --------------------------------------------------------------------------------
    def runs(self, host=None, gate=None, recipe=None, verdict=None, kind=None, commit=None, limit=200, gates_only=False):
        out = []
        for r in self.all_runs():
            if host and r["host"] != host:
                continue
            if gate and r["gate"] != gate:
                continue
            if recipe and r.get("recipe") != recipe:
                continue
            if verdict and r["verdict"] != verdict:
                continue
            if kind and r.get("kind") != kind:
                continue
            if gates_only and not self.is_gate(r):
                continue
            if commit and not self._commit_match(r, commit):
                continue
            out.append(r)
        out.sort(key=lambda r: r.get("started_ts") or 0, reverse=True)
        return [self.summary(r) for r in out[:limit]], len(out)

    @staticmethod
    def _commit_match(run, x):
        c = run.get("candidate") or {}
        x = x.lower()
        return bool(x) and ((c.get("commit") or "").lower().startswith(x) or (c.get("label") or "").lower() == x
                            or (c.get("jar_sha256") or "").lower().startswith(x))

    # -- single run -------------------------------------------------------------------------------
    def siblings(self, run):
        """Same host + gate, other runs, newest first."""
        return sorted([r for r in self.all_runs() if r["host"] == run["host"] and r["gate"] == run["gate"] and r["id"] != run["id"]
                       and r["verdict"] != "RUNNING"], key=lambda r: r.get("started_ts") or 0, reverse=True)

    def auto_baseline(self, run):
        sibs = [r for r in self.siblings(run) if (r.get("started_ts") or 0) <= (run.get("started_ts") or 1e18)]
        jar = (run.get("candidate") or {}).get("jar_sha256")
        for want_diff in (True, False):
            for r in sibs:
                if r["verdict"] != "PASS":
                    continue
                same = (r.get("candidate") or {}).get("jar_sha256") == jar and jar
                if want_diff and same:
                    continue
                if cmp.compare_env(r, run)["comparable"] or not want_diff:
                    return r
        return None

    def group_for(self, run):
        """Runs of identical code on the same host+gate with a comparable environment (noise sample)."""
        jar = (run.get("candidate") or {}).get("jar_sha256")
        if not jar:
            return [self.full(run)]
        grp = [r for r in self.all_runs() if r["host"] == run["host"] and r["gate"] == run["gate"]
               and (r.get("candidate") or {}).get("jar_sha256") == jar and r["verdict"] in FINISHED
               and (r["id"] == run["id"] or cmp.compare_env(run, r)["comparable"])]
        return [self.full(r) for r in grp]

    def run_detail(self, rid, base="auto"):
        run = self.get_run(rid)
        if run is None:
            return None
        sibs = self.siblings(run)
        base_run = None
        if base == "auto":
            base_run = self.auto_baseline(run)
        elif base and base != "none":
            base_run = self.get_run(base)
        compare = None
        if base_run is not None:
            base_run = self.full(base_run)
            compare = cmp.compare_runs(base_run, run, self.group_for(base_run), self.group_for(run))
            compare["base_summary"] = self.summary(base_run, with_key=False)
        out = {"run": run, "base_candidates": [self.summary(r, with_key=False) for r in sibs[:40]], "compare": compare,
               "base": base_run["id"] if base_run else None, "has_log": bool(run.get("log"))}
        mirror = self.store.load_mirror(rid)
        out["mirror"] = {"files": len(mirror.get("files", {})),
                         "full_skipped": [k for k, v in mirror.get("files", {}).items() if v.get("full_skipped")][:10]}
        return out

    # -- logs -------------------------------------------------------------------------------------
    def log_lines(self, rid):
        return self.store.read_log_lines(rid)

    def log_view(self, rid, tail=300, level=None, grep=None):
        lines = self.log_lines(rid)
        if lines is None:
            return None
        ents = logmod.entries(lines)
        rows = []
        want = None
        if level:
            order = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
            want = set(order[order.index(level.upper()):]) if level.upper() in order else None
        rx = re.compile(re.escape(grep), re.I) if grep else None
        for e in ents:
            if want and e.level not in want:
                continue
            text = e.raw if not e.cont else e.raw + "\n" + "\n".join(e.cont[:12])
            if rx and not rx.search(text):
                continue
            rows.append({"n": e.idx + 1, "level": e.level, "text": text[:2000]})
        total = len(rows)
        return {"total": total, "lines": rows[-tail:], "counts": (self.get_light(rid) or {}).get("log")}

    def log_diff(self, base_id, cand_id):
        key = (base_id, cand_id)
        bl, cl = self.log_lines(base_id), self.log_lines(cand_id)
        if bl is None or cl is None:
            return {"error": "log missing", "base_has_log": bl is not None, "cand_has_log": cl is not None}
        bm = self.store.p("runs", base_id, "log.txt.gz")
        cm = self.store.p("runs", cand_id, "log.txt.gz")
        stamp = (os.path.getmtime(bm), os.path.getmtime(cm))
        hit = self._diff_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        d = logmod.diff_logs(bl, cl)
        d["base"], d["cand"] = base_id, cand_id
        self._diff_cache[key] = (stamp, d)
        if len(self._diff_cache) > 64:
            self._diff_cache.pop(next(iter(self._diff_cache)))
        return d

    # -- compare ----------------------------------------------------------------------------------
    def _pick(self, runs):
        """Representative run per (host, gate): the most recent finished one, preferring runs with metrics."""
        fin = [r for r in runs if r["verdict"] in FINISHED]
        if not fin:
            fin = [r for r in runs if r["verdict"] not in ("RUNNING",)]
        if not fin:
            return None
        fin.sort(key=lambda r: (bool(self._n_metrics(r)), r.get("started_ts") or 0))
        return fin[-1]

    def compare(self, base, cand):
        a, b = self.get_light(base), self.get_light(cand)
        if a is not None and b is not None:
            a, b = self.full(a), self.full(b)
            c = cmp.compare_runs(a, b, self.group_for(a), self.group_for(b))
            return {"mode": "runs", "base": self.summary(a, with_key=False), "cand": self.summary(b, with_key=False),
                    "result": c, "log_diff_available": bool(a.get("log") and b.get("log"))}
        gates = [r for r in self.all_runs() if self.is_gate(r) and r.get("kind") == "client"]
        bruns = [r for r in gates if self._commit_match(r, base)]
        cruns = [r for r in gates if self._commit_match(r, cand)]
        keys = sorted({(r["host"], r["gate"]) for r in bruns + cruns},
                      key=lambda k: ({"ctl": 0, "proxmox-test": 1, "amd": 2, "gpc": 3, "mac": 4}.get(k[0], 9), k[1]))
        cells = []
        for host, gate in keys:
            br = self._pick([r for r in bruns if r["host"] == host and r["gate"] == gate])
            cr = self._pick([r for r in cruns if r["host"] == host and r["gate"] == gate])
            cell = {"host": host, "gate": gate, "base": self.summary(br) if br else None, "cand": self.summary(cr) if cr else None,
                    "n_base_runs": len([r for r in bruns if r["host"] == host and r["gate"] == gate]),
                    "n_cand_runs": len([r for r in cruns if r["host"] == host and r["gate"] == gate]),
                    "env": None, "deltas": [], "summary": None, "log_diff_available": False}
            if br and cr:
                c = cmp.compare_runs(self.full(br), self.full(cr), self.group_for(br), self.group_for(cr))
                keyset = {metric_key(m) for m in self._key_metrics(cr)} | {metric_key(m) for m in self._key_metrics(br)}
                cell["env"] = c["env"]
                cell["deltas"] = [r for r in c["metrics"] if r["key"] in keyset]
                cell["summary"] = c["summary"]
                cell["log_diff_available"] = bool(br.get("log") and cr.get("log"))
            cells.append(cell)
        def label(runs, x):
            labs = sorted({(r.get("candidate") or {}).get("label") or x for r in runs})
            return labs[0] if labs else x
        return {"mode": "commits", "base": {"query": base, "label": label(bruns, base), "runs": len(bruns)},
                "cand": {"query": cand, "label": label(cruns, cand), "runs": len(cruns)}, "cells": cells,
                "hosts": sorted({c["host"] for c in cells}), "gates": sorted({c["gate"] for c in cells})}

    def candidates(self):
        """Distinct candidates seen in runs, newest first (for the compare pickers)."""
        seen = {}
        for r in self.all_runs():
            c = r.get("candidate") or {}
            key = c.get("jar_sha256") or c.get("commit") or c.get("label")
            if not key or not self.is_gate(r) or not self.has_evidence(r):
                continue
            e = seen.setdefault(key, {"label": c.get("label"), "commit": c.get("commit"), "jar_sha256": c.get("jar_sha256"),
                                      "runs": 0, "last_ts": 0, "hosts": set()})
            e["runs"] += 1
            e["last_ts"] = max(e["last_ts"], r.get("started_ts") or 0)
            e["hosts"].add(r["host"])
        out = []
        for e in seen.values():
            e["hosts"] = sorted(e["hosts"])
            out.append(e)
        out.sort(key=lambda e: -e["last_ts"])
        return out

    # -- history ----------------------------------------------------------------------------------
    def history(self, host=None, gate=None, limit=24):
        groups = {}
        for r in self.all_runs():
            if not self.is_gate(r) or r.get("kind") != "client" or r["verdict"] == "RUNNING" or not self.has_evidence(r) or r["gate"] == "dry-run":
                continue
            if host and r["host"] != host:
                continue
            if gate and r["gate"] != gate:
                continue
            groups.setdefault((r["host"], r["gate"]), []).append(r)
        out = []
        order = {"ctl": 0, "proxmox-test": 1, "amd": 2, "gpc": 3, "mac": 4}
        for (h, g), runs in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0][1])):
            runs.sort(key=lambda r: r.get("started_ts") or 0)
            runs = runs[-limit:]
            metric_meta, rows = {}, []
            for r in runs:
                vals = {}
                for m in self._key_metrics(r):
                    k = metric_key(m)
                    vals[k] = m["value"]
                    metric_meta.setdefault(k, {"key": k, "name": m["name"], "unit": m.get("unit"), "better": m.get("better")})
                c = r.get("candidate") or {}
                rows.append({"id": r["id"], "ts": r.get("started_ts"), "verdict": r["verdict"], "label": c.get("label"),
                             "commit": c.get("commit"), "jar": c.get("jar_sha256"), "metrics": vals,
                             "renderer": (r.get("environment") or {}).get("gl_renderer")})
            out.append({"host": h, "gate": g, "n": len(runs), "runs": rows, "metrics": list(metric_meta.values())})
        return {"groups": out}

    # -- live -------------------------------------------------------------------------------------
    def live(self, host, job=None):
        live = self.store.read_json(self.store.p("live", host + ".json"), None)
        if live is None:
            return {"host": host, "active": False, "job": None}
        if job and job not in ("current", "latest") and (live.get("job") or {}).get("id") != job:
            return {"host": host, "active": False, "job": None, "note": "that job is not the live one on this host"}
        return live

    def status(self):
        st = self.store.read_json(self.store.p("state.json"), {}) or {}
        return {"collector": st, "runs": len(self.all_runs()), "now": time.time(), "version": "0.1.0"}
