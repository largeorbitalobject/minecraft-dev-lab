"""Collector: polls the fleet (read-only) and mirrors new evidence into data/.

Per host, independent thread:
  * every poll_interval_s (60): Dev Lab API run_list + one remote_agent call that lists every evidence source,
    builds normalised runs, copies new large files once (logs, screenshots + thumbnails), writes run.json;
  * every live_interval_s (5) while a Dev Lab job is RUNNING on that host: live progress (harness
    progress.jsonl series, feature-plan progress.txt timeline, log tail) into data/live/<host>.json.
A host that does not answer only delays its own thread (per-call timeouts); the others keep going.
"""
from __future__ import annotations

import argparse
import copy
import shutil
import json
import os
import sys
import threading
import time
import traceback

from . import adapters, config as cfgmod, harness_state, native_queue_state, logs, parsers, thumbs
from .model import metric_key, utc_iso
from .remote import TransportError, make_transport
from .store import Store

PRIVATE = ("_mirror", "_inline", "_shots_meta", "_log_mtime_window")


def _load_malloc_trim():
    try:
        import ctypes
        return ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return None


_MALLOC_TRIM = _load_malloc_trim() if sys.platform.startswith("linux") else None


def release_memory():
    """A full poll parses every inline result.json/progress.jsonl into transient run dicts (hundreds of MB on ctl).
    glibc keeps freed heap pages, so the server's RSS ratcheted up poll after poll; hand them back to the OS."""
    if _MALLOC_TRIM is not None:
        _MALLOC_TRIM(0)


# ----------------------------------------------------------------------------------------------
# Dev Lab API wrapper
# ----------------------------------------------------------------------------------------------

class ApiClient:
    def __init__(self, pyz=cfgmod.PYZ, fleet=cfgmod.FLEET_JSON):
        self.error = None
        self.api = None
        try:
            if pyz not in sys.path:
                sys.path.insert(0, pyz)
            from mcdevlab.api import LabAPI  # type: ignore
            self.api = LabAPI(fleet)
        except Exception as e:  # pragma: no cover - depends on the node
            self.error = "%s: %s" % (type(e).__name__, e)

    def call(self, tool, args):
        if self.api is None:
            raise RuntimeError("Dev Lab API unavailable: %s" % self.error)
        return self.api.call(tool, args)


class NullApi:
    error = "disabled"

    def call(self, tool, args):
        raise RuntimeError("Dev Lab API disabled")


# ----------------------------------------------------------------------------------------------
# one host
# ----------------------------------------------------------------------------------------------

class HostPoller:
    def __init__(self, host_id, hcfg, cfg, store, registry, reg_lock, api, log=None):
        self.id = host_id
        self.h = hcfg
        self.cfg = cfg
        self.store = store
        self.reg = registry
        self.reg_lock = reg_lock
        self.api = api
        self.transport = make_transport(host_id, hcfg)
        self.log = log or (lambda *a: None)
        self.cache = store.read_json(store.p("cache", host_id + ".json"), {}) or {}
        self.state = {"host": host_id, "state": "unknown"}
        self.live = {"offsets": {}, "samples": [], "group": None}
        self.last_full = 0.0
        self.running_job = None
        self.lock = threading.Lock()

    # -- helpers ---------------------------------------------------------------------------------
    def _call(self, ops, timeout=None):
        return self.transport.call({"ops": ops}, timeout or self.cfg["host_timeout_s"])

    def _known(self, i):
        c = self.cache.get(str(i)) or {}
        return {rel: [v["s"], v["m"]] for rel, v in c.items() if "t" in v}

    def _update_cache(self, i, res, spec):
        c = self.cache.setdefault(str(i), {})
        listed = {f["p"]: f for f in res.get("files", [])}
        for rel in list(c):
            if rel not in listed:
                del c[rel]
        for rel, f in listed.items():
            cur = c.get(rel)
            if rel in (res.get("inline") or {}):
                txt = res["inline"][rel]
                c[rel] = {"s": f["s"], "m": f["m"], "t": txt if len(txt) <= 262144 else None, "_mem": txt}
            elif cur and (cur["s"], cur["m"]) == (f["s"], f["m"]):
                continue
            else:
                c[rel] = {"s": f["s"], "m": f["m"]}
        view_text = {}
        for rel, v in c.items():
            t = v.get("_mem") if v.get("_mem") is not None else v.get("t")
            if t is not None:
                view_text[rel] = t
        return listed, view_text

    def _persist_cache(self):
        out = {}
        for i, files in self.cache.items():
            out[i] = {rel: {k: v for k, v in e.items() if k != "_mem" and not (k == "t" and v is None)}
                      for rel, e in files.items()}
        self.store.write_json(self.store.p("cache", self.id + ".json"), out)

    # -- state from jobs -------------------------------------------------------------------------
    def _jobs(self):
        res = self.api.call("run_list", {"host": self.id, "limit": 100})
        return res.get("runs") or [], res

    INFLIGHT_DIR = "/root/mcdevlab-shared/harness/inflight"

    def _controller(self):
        return harness_state.observe(self.h.get("controller_archive"), self.h.get("controller_host", self.id))

    def _attach_native_queue(self, st):
        observation = native_queue_state.observe(self.h.get("controller_host", self.id))
        if observation is not None:
            st["native_queue"] = observation

    def _apply_controller(self, st, observation):
        st["controller"] = observation
        if observation["state"] != "idle":
            st["state"] = observation["state"]
            st["job"] = observation.get("job")
            self.running_job = st.get("job") if st["state"] == "running" else None

    def _inflight(self):
        """A run started by a direct harness wrapper (gpc-harness[-large], mac-harness[-large]) bypasses the Dev Lab job
        queue; while it is live the wrapper keeps harness/inflight/<host>.json. Returned as one RUNNING job-like dict
        (only for state/live display, never handed to the run builder), or None when absent or its wrapper is gone."""
        path = os.path.join(self.INFLIGHT_DIR, self.id + ".json")
        try:
            with open(path) as f:
                m = json.load(f)
            os.kill(int(m["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return {"id": m.get("id"), "request_id": None, "recipe": m.get("scenario"), "state": "RUNNING",
                "created": m.get("started_utc"), "receipt": {"started": m.get("started_utc")}, "heartbeat": None,
                "source": "harness-wrapper", "arm": m.get("arm"), "label": m.get("label")}

    def _job_logs(self, jobs):
        """Cached log text for jobs that ended badly (the only place a refusal reason is written)."""
        out = {}
        budget = 8
        for j in jobs:
            if j.get("state") not in ("FAILED", "BLOCKED", "CANCELLED", "UNKNOWN"):
                continue
            jid = j["id"]
            path = self.store.p("jobs", self.id, jid + ".json")
            cached = self.store.read_json(path)
            if cached is not None:
                out[jid] = cached.get("text")
                continue
            if budget <= 0 or j.get("state") == "UNKNOWN":
                continue
            budget -= 1
            try:
                r = self.api.call("run_logs", {"host": self.id, "job": jid, "bytes": 8192})
                text = r.get("text") or ""
                self.store.write_json(path, {"job": jid, "text": text[-8192:], "triage": r.get("triage")})
                out[jid] = text
            except Exception as e:
                self.log("%s: run_logs %s failed: %s" % (self.id, jid, e))
        return out

    # -- full poll -------------------------------------------------------------------------------
    def full_poll(self):
        with self.lock:
            return self._full_poll()

    def _full_poll(self):
        t0 = time.time()
        st = {"host": self.id, "label": self.h.get("label", self.id), "platform": self.h.get("platform"),
              "hardware": self.h.get("hardware"), "owner_machine": bool(self.h.get("owner_machine")),
              "checked_ts": t0, "api_ok": False, "evidence_ok": False, "api_error": None, "evidence_error": None}
        jobs, joblist = [], {}
        try:
            jobs, joblist = self._jobs()
            st["api_ok"] = True
        except Exception as e:
            st["api_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        views, now, extra = [], time.time(), {}
        try:
            ops = []
            for i, spec in enumerate(self.h["sources"]):
                ops.append({"id": "s%d" % i, "op": "tree", "root": spec["root"], "maxdepth": spec.get("maxdepth", 3),
                            "include": spec.get("include", ["*"]), "inline": spec.get("inline", []),
                            "inline_max": spec.get("inline_max", 262144), "exclude_dirs": spec.get("exclude_dirs", []),
                            "known": self._known(i), "max_files": 8000})
            ops.append({"id": "meta", "op": "read",
                        "paths": list(self.h.get("candidate_files", [])) + list(self.h.get("pause_files", [])),
                        "max_bytes": 65536})
            results, _b, _e = self._call(ops)
            for i, spec in enumerate(self.h["sources"]):
                res = results.get("s%d" % i) or {}
                if res.get("error"):
                    st.setdefault("source_errors", []).append("%s: %s" % (spec["root"], res["error"]))
                    continue
                if not res.get("exists"):
                    continue
                listed, texts = self._update_cache(i, res, spec)
                now = res.get("now", now)
                views.append((spec, adapters.SourceView(spec["root"], {k: {"s": v["s"], "m": v["m"]} for k, v in listed.items()},
                                                        texts, res.get("now", now), spec)))
                if res.get("truncated"):
                    st.setdefault("source_errors", []).append("%s: listing truncated" % spec["root"])
                for er in (res.get("errors") or [])[:3]:
                    st.setdefault("source_errors", []).append(er)
            extra = (results.get("meta") or {}).get("files") or {}
            st["evidence_ok"] = True
            self._persist_cache()
        except TransportError as e:
            st["evidence_error"] = str(e)[:300]
        except Exception as e:
            st["evidence_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            self.log(traceback.format_exc())
        # candidate + pause
        cand = None
        paused_by = None
        with self.reg_lock:
            for p in self.h.get("candidate_files", []):
                e = extra.get(p)
                if e and e.get("exists") and e.get("text"):
                    try:
                        o = json.loads(e["text"])
                        self.reg.learn_obj(o)
                        c = o.get("candidate") if isinstance(o.get("candidate"), dict) else o
                        cand = {"jar_sha256": c.get("sha256") or c.get("jar_sha256") or o.get("jar_sha256"),
                                "label": c.get("label") or o.get("label"),
                                "commit": c.get("source_commit") or o.get("source_commit"),
                                "as_of": o.get("installed_at") or o.get("installed_utc") or c.get("built_utc"), "file": p}
                        self.reg.fill(cand)
                    except ValueError:
                        pass
                    break
        for p in self.h.get("pause_files", []):
            if (extra.get(p) or {}).get("exists"):
                paused_by = p
        # runs
        runs = []
        if st["evidence_ok"] or jobs:
            try:
                job_logs = self._job_logs(jobs) if st["api_ok"] else {}
                with self.reg_lock:
                    runs, ctx = adapters.build_host(self.id, views, self.reg, jobs, job_logs, now)
                if ctx.get("smoke_notes"):
                    st["wrapper_refusals"] = ctx["smoke_notes"][-5:]
                self._write_runs(runs, views, now)
            except Exception as e:
                st["build_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
                self.log(traceback.format_exc())
        # state
        running = [j for j in jobs if j.get("state") in ("RUNNING", "QUEUED")]
        if not running:
            wrapped = self._inflight()
            if wrapped:
                running = [wrapped]
        stale = [j for j in jobs if j.get("state") == "UNKNOWN"]
        if running:
            j = running[0]
            rc = j.get("receipt") or {}
            st["state"] = "running"
            st["job"] = {"id": j["id"], "request_id": j.get("request_id"), "recipe": j.get("recipe"),
                         "state": j.get("state"), "started": rc.get("started") or j.get("created"),
                         "heartbeat": j.get("heartbeat")}
            if j.get("source") == "harness-wrapper":
                st["job"].update({"source": "harness-wrapper", "arm": j.get("arm"), "label": j.get("label")})
            self.running_job = st["job"]
        elif stale:
            j = stale[0]
            st["state"] = "stale"
            st["job"] = {"id": j["id"], "request_id": j.get("request_id"), "recipe": j.get("recipe"), "state": "UNKNOWN",
                         "started": j.get("created"), "heartbeat": j.get("heartbeat"), "note": j.get("note")}
            self.running_job = None
        elif not st["api_ok"] and not st["evidence_ok"]:
            st["state"] = "unreachable"
            self.running_job = None
        elif paused_by:
            st["state"] = "paused"
            self.running_job = None
        else:
            st["state"] = "idle"
            self.running_job = None
        controller = self._controller()
        if controller and not running and not stale:
            # Controller terminal history cannot erase an API job or remote pause.
            self._apply_controller(st, controller)
        if paused_by:
            st["paused_by"] = paused_by
            if st["state"] == "running":
                st["paused_while_running"] = True
        if cand:
            st["candidate"] = cand
        if st["state"] == "unreachable":
            prev = self.store.read_json(self.store.p("hosts", self.id + ".json"), {}) or {}
            st["last_seen_ts"] = prev.get("last_seen_ts") or prev.get("checked_ts")
            st["candidate"] = st.get("candidate") or prev.get("candidate")
        else:
            st["last_seen_ts"] = t0
        self._attach_native_queue(st)
        st["run_count"] = len(runs)
        st["poll_ms"] = int((time.time() - t0) * 1000)
        self.state = st
        self.store.write_json(self.store.p("hosts", self.id + ".json"), st)
        self.last_full = time.time()
        return st

    # -- persisting runs -------------------------------------------------------------------------
    def _write_runs(self, runs, views, now):
        fp_index = {}
        for spec, view in views:
            for rel, f in view.files.items():
                fp_index[(view.root, rel)] = (f["s"], f["m"])
        keep_n = int(self.cfg.get("max_runs_per_host", 120))
        runs = sorted(runs, key=lambda r: r.get("started_ts") or 0)[-keep_n:]
        for r in runs:
            try:
                self._persist_run(r, fp_index, now)
            except Exception as e:
                self.log("%s: persist %s failed: %s\n%s" % (self.id, r.get("id"), e, traceback.format_exc()))
        self._retire_stale_running({r.get("id") for r in runs})

    def _retire_stale_running(self, current_ids):
        """Drop provisional RUNNING records this host no longer produces. While a run is live the
        collector files it under a provisional id; once it finishes it reappears under its final id,
        and without this the provisional record stayed listed as RUNNING forever."""
        prefix = self.id + "--"
        for rid in list(self.store.run_ids()):
            if not rid.startswith(prefix) or rid in current_ids:
                continue
            run = self.store.read_json(self.store.p("runs", rid, "run.json"), None)
            if run and run.get("verdict") == "RUNNING":
                shutil.rmtree(self.store.run_dir(rid), ignore_errors=True)
                self.log("%s: retired stale provisional run %s" % (self.id, rid))

    def _persist_run(self, draft, fp_index, now):
        st = self.store
        run = {k: copy.deepcopy(v) for k, v in draft.items() if k not in PRIVATE}
        rid = run["id"]
        rdir = st.run_dir(rid)
        os.makedirs(rdir, exist_ok=True)
        mirror = st.load_mirror(rid)
        mirror.setdefault("files", {})
        cap = int(self.cfg.get("per_run_cap_mb", 30)) * 1024 * 1024
        # small evidence files
        names = []
        for name, text in (draft.get("_inline") or {}).items():
            if text is None:
                continue
            safe = os.path.basename(name)
            path = os.path.join(rdir, "files", safe)
            data = text.encode("utf-8")
            try:
                with open(path, "rb") as fh:
                    same = fh.read() == data
            except OSError:
                same = False
            if not same:
                st.write_bytes(path, data)
            names.append(safe)
        run["evidence"]["inline_files"] = sorted(names)
        # large files (skip while the run is live; skip files modified in the last 10 s)
        todo = []
        if run["verdict"] != "RUNNING":
            for m in draft.get("_mirror") or []:
                fp = fp_index.get((m["root"], m["rel"]))
                if fp is None:
                    continue
                if now - fp[1] < 10:
                    continue
                ent = mirror["files"].get(m["rel"])
                if ent and ent.get("fp") == list(fp) and ent.get("ok"):
                    continue
                todo.append((m, fp))
        stored_bytes = st.dir_size(rdir) if todo else 0
        if todo:
            self._fetch_and_store(rid, rdir, mirror, todo, cap, stored_bytes)
        # compose screenshots/log from the mirror manifest
        shots = []
        shot_meta = {s["src"].rsplit("/", 1)[-1]: s for s in (draft.get("_shots_meta") or [])}
        for m in draft.get("_mirror") or []:
            ent = mirror["files"].get(m["rel"])
            if not ent or not ent.get("ok"):
                continue
            if m["kind"] == "shot":
                meta = shot_meta.get(os.path.basename(m["rel"]), {})
                shots.append({"name": m.get("name") or os.path.basename(m["rel"]), "thumb": ent.get("thumb"),
                              "full": ent.get("full"), "width": ent.get("w"), "height": ent.get("h"),
                              "bytes": ent.get("bytes"), "step": meta.get("step")})
            elif m["kind"] == "log":
                run["log"] = ent.get("loginfo")
                run["crash"] = ent.get("crash")
                env = run["environment"]
                for k, v in (ent.get("env") or {}).items():
                    if v and not env.get(k):
                        env[k] = v
                if ent.get("mods") and not env.get("mods"):
                    env["mods"] = ent["mods"]
                c = (ent.get("loginfo") or {}).get("counts") or {}
                for name, key in (("viewer.log_errors", "error"), ("viewer.log_warns", "warn"),
                                  ("viewer.log_exceptions", "exceptions")):
                    if key in c:
                        run["metrics"].append({"name": name, "segment": None, "unit": "", "value": c[key],
                                               "better": "lower", "role": "derived", "check": "log"})
        run["screenshots"] = shots
        pending = [m for m in (draft.get("_mirror") or []) if not (mirror["files"].get(m["rel"]) or {}).get("ok")]
        run["mirror"] = {"complete": not pending, "pending": len(pending), "failed": [
            {"rel": rel, "err": e.get("err")} for rel, e in mirror["files"].items() if not e.get("ok")][:5]}
        st.write_json(os.path.join(rdir, "mirror.json"), mirror)
        st.write_json(os.path.join(rdir, "run.json"), run)

    def _fetch_and_store(self, rid, rdir, mirror, todo, cap, stored_bytes):
        st = self.store
        # group by root, batches of <= 24MB worth (sizes known from listing)
        batches, cur, cur_bytes = [], [], 0
        for m, fp in todo:
            if cur and (cur_bytes + fp[0] > 24 * 1024 * 1024 or len(cur) >= 40):
                batches.append(cur)
                cur, cur_bytes = [], 0
            cur.append((m, fp))
            cur_bytes += fp[0]
        if cur:
            batches.append(cur)
        for batch in batches:
            ops = [{"id": "f", "op": "fetch", "max_bytes": 64 * 1024 * 1024,
                    "files": [{"root": m["root"], "rel": m["rel"]} for m, _fp in batch]}]
            try:
                results, blobs, errs = self._call(ops, self.cfg["fetch_timeout_s"])
            except TransportError as e:
                for m, fp in batch:
                    mirror["files"][m["rel"]] = {"fp": list(fp), "ok": False, "err": str(e)[:160]}
                continue
            meta = (results.get("f") or {}).get("files") or []
            for (m, fp), fm in zip(batch, meta):
                idx = fm.get("idx")
                if not fm.get("ok") or idx not in blobs:
                    mirror["files"][m["rel"]] = {"fp": list(fp), "ok": False, "err": fm.get("err") or errs.get(idx) or "no data"}
                    continue
                data = blobs[idx]
                # the file may have changed between listing and fetch: store what we got with its own size
                ent = {"fp": [fm.get("s", len(data)), fm.get("m", fp[1])], "ok": True, "bytes": len(data)}
                try:
                    if m["kind"] == "shot":
                        base = parsers_safe_name(m.get("name") or os.path.basename(m["rel"]))
                        stem = base.rsplit(".", 1)[0]
                        tb, ext, (w, h) = thumbs.make_thumb(data)
                        st.write_bytes(os.path.join(rdir, "thumbs", stem + "." + ext), tb)
                        ent.update({"thumb": "thumbs/%s.%s" % (stem, ext), "w": w, "h": h})
                        stored_bytes += len(tb)
                        if stored_bytes + len(data) <= cap:
                            st.write_bytes(os.path.join(rdir, "shots", stem + ".png"), data)
                            ent["full"] = "shots/%s.png" % stem
                            stored_bytes += len(data)
                        else:
                            ent["full"] = None
                            ent["full_skipped"] = "per-run cap"
                    elif m["kind"] == "log":
                        text = data.decode("utf-8", "replace")
                        lines = logs.to_lines(text)
                        gz = st.write_log_lines(rid, lines)
                        stored_bytes += gz
                        counts = logs.count_levels(lines)
                        ent.update({"loginfo": {"path": "log.txt.gz", "lines": len(lines), "bytes": len(data), "gz_bytes": gz,
                                                "counts": counts, "source": os.path.basename(m["rel"])},
                                    "crash": logs.crash_signature(lines), "env": logs.log_environment(lines),
                                    "mods": logs.parse_mod_list(lines)})
                except Exception as e:
                    ent = {"fp": list(fp), "ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:120])}
                mirror["files"][m["rel"]] = ent

    # -- live ------------------------------------------------------------------------------------
    def live_poll(self):
        with self.lock:
            return self._live_poll()

    def _live_poll(self):
        t0 = time.time()
        if (self.running_job or {}).get("source") == "large-harness-controller":
            observation = self._controller()
            st = self.state
            if observation:
                st.pop("job", None)
                st["state"] = "idle"
                self.running_job = None
                self._apply_controller(st, observation)
            else:
                st["state"] = "stale"
                st["controller"] = {"reason": "Controller observation disappeared; remote exit unconfirmed"}
                self.running_job = None
            if st.get("paused_by") and st["state"] == "idle":
                st["state"] = "paused"
            st["checked_ts"] = t0
            self.store.write_json(self.store.p("hosts", self.id + ".json"), st)
            live = {"host": self.id, "active": st["state"] == "running", "updated_ts": t0,
                    "job": st.get("job"), "kind": "controller", "timeline": [], "progress": None,
                    "log_tail": [], "shots": [], "files": [], "errors": [],
                    "controller": st.get("controller")}
            self.store.write_json(self.store.p("live", self.id + ".json"), live)
            return live
        job = None
        try:
            jobs, _ = self._jobs()
            running = [j for j in jobs if j.get("state") in ("RUNNING", "QUEUED")]
            job = running[0] if running else self._inflight()
        except Exception as e:
            self.state["api_error"] = str(e)[:200]
            return {"active": False}
        path = self.store.p("live", self.id + ".json")
        if job is None:
            self.running_job = None
            prev = self.store.read_json(path, {}) or {}
            if prev.get("active"):
                prev["active"] = False
                prev["ended_ts"] = t0
                self.store.write_json(path, prev)
            return {"active": False}
        rc = job.get("receipt") or {}
        live = {"host": self.id, "active": True, "updated_ts": t0,
                "job": {"id": job["id"], "request_id": job.get("request_id"), "recipe": job.get("recipe"),
                        "state": job.get("state"), "started": rc.get("started") or job.get("created"),
                        "heartbeat": job.get("heartbeat")},
                "kind": "log", "timeline": [], "progress": None, "log_tail": [], "shots": [], "files": [], "errors": []}
        roles_by_type = {
            "featureplan_dirs": ([{"role": "progress_txt", "include": ["*/probe/progress.txt"], "tail_bytes": 16384},
                                  {"role": "log", "include": ["*/client.log"], "tail_bytes": 49152},
                                  {"role": "shot", "include": ["*/probe/*.png"], "tail_bytes": 0}], "first"),
            "fd_runs": ([{"role": "log", "include": ["*/smoke-direct.log"], "tail_bytes": 49152}], "first"),
            "harness": ([{"role": "progress_jsonl", "include": ["progress.jsonl"], "tail_bytes": 262144},
                         {"role": "log", "include": ["client.log", "latest.log", "harness.log"], "tail_bytes": 49152},
                         {"role": "shot", "include": ["*.png"], "tail_bytes": 0}], "dir"),
            "node_smoke": ([{"role": "log", "include": ["latest.log"], "tail_bytes": 49152}], "root"),
            "instance_fp": ([{"role": "progress_txt", "include": ["progress.txt"], "tail_bytes": 16384},
                             {"role": "shot", "include": ["*.png"], "tail_bytes": 0}], "root"),
        }
        ops, opmap = [], {}
        for i, spec in enumerate(self.h["sources"]):
            rl = roles_by_type.get(spec["type"])
            if not rl:
                continue
            offsets = {k: v for k, v in self.live["offsets"].items()}
            oid = "l%d" % i
            opmap[oid] = spec
            ops.append({"id": oid, "op": "live", "root": spec["root"], "maxdepth": spec.get("maxdepth", 3),
                        "exclude_dirs": spec.get("exclude_dirs", []), "roles": rl[0], "group": rl[1],
                        "since_s": 1800, "offsets": offsets})
        best = None
        try:
            results, _b, _e = self._call(ops, 30) if ops else ({}, {}, {})
            for oid, spec in opmap.items():
                r = results.get(oid) or {}
                if not r.get("group") and not r.get("files"):
                    continue
                newest = max([f["m"] for f in r.get("files", [])] or [0])
                # prefer runs that started after the job did
                if best is None or newest > best[0]:
                    best = (newest, spec, r)
        except TransportError as e:
            live["errors"].append("live read failed: %s" % str(e)[:200])
        if best is not None:
            _n, spec, r = best
            live["source_type"] = spec["type"]
            live["group"] = r.get("group")
            if self.live.get("group") != (spec["root"], r.get("group")):
                self.live = {"offsets": {}, "samples": [], "group": (spec["root"], r.get("group"))}
            live["files"] = [f["p"] for f in r["files"] if f.get("role") != "shot"]
            live["shots"] = [f["p"].rsplit("/", 1)[-1] for f in r["files"] if f.get("role") == "shot"]
            for rel, t in (r.get("tails") or {}).items():
                role = next((f["role"] for f in r["files"] if f["p"] == rel), None)
                if not t.get("exists"):
                    continue
                text = t.get("text") or ""
                if role == "progress_jsonl":
                    if t.get("reset") or t.get("start") == 0 and not self.live["offsets"].get(rel):
                        self.live["samples"] = []
                    # drop a trailing partial line
                    complete = text if text.endswith("\n") else text[:text.rfind("\n") + 1]
                    self.live["samples"].extend(parsers.parse_progress_jsonl(complete))
                    self.live["offsets"][rel] = t.get("start", 0) + len(complete.encode("utf-8"))
                    live["kind"] = "harness"
                    sm = self.live["samples"][-3000:]
                    live["progress"] = parsers.progress_series(sm, 600)
                    if sm:
                        live["last"] = sm[-1]
                elif role == "progress_txt":
                    live["timeline"] = parsers.parse_progress_txt(text)
                    if live["kind"] != "harness":
                        live["kind"] = "featureplan"
                elif role == "log":
                    body = text
                    if t.get("start", 0) > 0 and "\n" in body:
                        body = body.split("\n", 1)[1]
                    lines = logs.to_lines(body)
                    live["log_tail"] = lines[-160:]
                    live["log_file"] = rel
        if not live["log_tail"]:
            try:
                r = self.api.call("run_logs", {"host": self.id, "job": job["id"], "bytes": 6000})
                live["log_tail"] = (r.get("text") or "").splitlines()[-120:]
                live["log_file"] = "dev-lab job output"
            except Exception as e:
                live["errors"].append("job log: %s" % str(e)[:160])
        self.store.write_json(path, live)
        self.running_job = live["job"]
        return live


def parsers_safe_name(name):
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)


# ----------------------------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------------------------

class Collector:
    def __init__(self, cfg, data_dir, api=None, hosts=None, log=None):
        self.cfg = cfg
        self.store = Store(data_dir)
        self.api = api if api is not None else ApiClient()
        self.log = log or (lambda m: print("[collector] " + m, file=sys.stderr, flush=True))
        self.reg_lock = threading.Lock()
        from .adapters import Registry
        self.registry = Registry(self.store.read_json(self.store.p("candidates.json"), {}))
        self.pollers = {}
        for hid, h in cfg["hosts"].items():
            if hosts and hid not in hosts:
                continue
            self.pollers[hid] = HostPoller(hid, h, cfg, self.store, self.registry, self.reg_lock, self.api, self.log)
        self.stop = threading.Event()
        self.threads = []

    def _save_registry(self):
        with self.reg_lock:
            self.store.write_json(self.store.p("candidates.json"), self.registry.by_sha)

    def poll_once(self):
        """One full pass over every host, in parallel, each bounded by its own timeouts."""
        ths = []
        for hp in self.pollers.values():
            th = threading.Thread(target=self._safe_full, args=(hp,), daemon=True)
            th.start()
            ths.append(th)
        for th in ths:
            th.join(self.cfg["host_timeout_s"] + self.cfg["fetch_timeout_s"] + 60)
        self._finish_cycle()

    def _safe_full(self, hp):
        try:
            hp.full_poll()
        except Exception:
            self.log("%s: full poll crashed\n%s" % (hp.id, traceback.format_exc()))

    def _finish_cycle(self):
        self._save_registry()
        try:
            prune_originals(self.store, int(self.cfg.get("total_originals_budget_mb", 2000)) * 1024 * 1024, self.log)
        except Exception:
            self.log("prune failed\n" + traceback.format_exc())
        self.store.write_json(self.store.p("state.json"), {
            "updated_ts": time.time(), "hosts": sorted(self.pollers),
            "poll_interval_s": self.cfg["poll_interval_s"], "live_interval_s": self.cfg["live_interval_s"]})

    def _host_loop(self, hp):
        interval, live_iv = self.cfg["poll_interval_s"], self.cfg["live_interval_s"]
        while not self.stop.is_set():
            t0 = time.time()
            try:
                if hp.running_job is not None and time.time() - hp.last_full < interval:
                    prev = hp.running_job
                    live = hp.live_poll()
                    if prev is not None and not live.get("active"):
                        hp.full_poll()  # job just ended: mirror its evidence right away
                else:
                    hp.full_poll()
                    if hp.running_job is not None:
                        hp.live_poll()
            except Exception:
                self.log("%s: loop error\n%s" % (hp.id, traceback.format_exc()))
            if hp.id == next(iter(self.pollers)):
                self._finish_cycle()
            release_memory()
            wait = live_iv if hp.running_job is not None else interval
            self.stop.wait(max(0.5, wait - (time.time() - t0)))

    def run_forever(self):
        for hp in self.pollers.values():
            th = threading.Thread(target=self._host_loop, args=(hp,), name="poll-" + hp.id, daemon=True)
            th.start()
            self.threads.append(th)

    def shutdown(self):
        self.stop.set()


def prune_originals(store, budget_bytes, log=None):
    """Drop full-size screenshot copies of the oldest runs once the originals exceed the budget."""
    entries, total = [], 0
    for rid in store.run_ids():
        sd = store.p("runs", rid, "shots")
        if not os.path.isdir(sd):
            continue
        size = store.dir_size(sd)
        run = store.load_run(rid) or {}
        entries.append((run.get("finished_ts") or run.get("started_ts") or 0, rid, size))
        total += size
    if total <= budget_bytes:
        return 0
    freed = 0
    for _ts, rid, size in sorted(entries):
        if total - freed <= budget_bytes:
            break
        sd = store.p("runs", rid, "shots")
        for f in os.listdir(sd):
            try:
                os.unlink(os.path.join(sd, f))
            except OSError:
                pass
        mirror = store.load_mirror(rid)
        for ent in mirror["files"].values():
            if ent.get("full"):
                ent["full"] = None
                ent["full_skipped"] = "pruned (storage budget)"
        store.write_json(store.p("runs", rid, "mirror.json"), mirror)
        run = store.load_run(rid)
        if run:
            for s in run.get("screenshots", []):
                s["full"] = None
            store.write_json(store.p("runs", rid, "run.json"), run)
        freed += size
        if log:
            log("pruned originals of %s (%d bytes)" % (rid, size))
    return freed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dev Lab Viewer collector")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="poll every host once and exit")
    ap.add_argument("--host", action="append", help="limit to these host ids")
    ap.add_argument("--no-api", action="store_true", help="skip the Dev Lab fleet API")
    args = ap.parse_args(argv)
    cfg = cfgmod.load_config(args.config)
    col = Collector(cfg, args.data, api=NullApi() if args.no_api else None, hosts=args.host)
    if args.once:
        t = time.time()
        col.poll_once()
        for hid, hp in col.pollers.items():
            s = hp.state
            print("%-13s %-11s api=%s evidence=%s runs=%s %dms %s" % (
                hid, s.get("state"), s.get("api_ok"), s.get("evidence_ok"), s.get("run_count"), s.get("poll_ms", 0),
                s.get("evidence_error") or s.get("api_error") or ""))
        print("done in %.1fs" % (time.time() - t))
        return 0
    col.run_forever()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        col.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
