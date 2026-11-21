import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import helpers  # noqa: F401
from helpers import real
import make_fixtures as mf
from mcdevlab_viewer import adapters, collector, config as cfgmod, remote
from mcdevlab_viewer.app import App


class FakeApi:
    """Stands in for the Dev Lab fleet API: run_list/run_logs per host."""

    def __init__(self):
        self.jobs = {}
        self.logs = {}
        self.fail_hosts = set()

    def call(self, tool, args):
        if args["host"] in self.fail_hosts:
            raise RuntimeError("api down for " + args["host"])
        if tool == "run_list":
            return {"runs": self.jobs.get(args["host"], []), "resource_holders": [], "action_holders": []}
        if tool == "run_logs":
            return {"text": self.logs.get(args["job"], "")}
        raise RuntimeError(tool)


def job(jid, recipe, state, created, started=None, finished=None, kind="client", error=None, rid=None):
    rc = {"kind": kind, "started": started or created, "finished": finished, "exit_code": 0 if state == "PASSED" else 1, "error": error, "artifacts": []}
    return {"id": jid, "request_id": rid or jid, "instance": "atlas", "recipe": recipe, "state": state, "created": created, "heartbeat": created + 1,
            "receipt": rc if state not in ("RUNNING", "QUEUED") else None}


def make_cfg(root, extra_hosts=None, **kw):
    cfg = cfgmod.load_config(None)
    cfg["hosts"] = {"ctl": {"label": "ctl", "platform": "Linux", "transport": {"type": "local"}, "candidate_files": [os.path.join(root, "candidate.json")],
                            "pause_files": [os.path.join(root, "PAUSE")],
                            "sources": [{"type": "harness", "root": os.path.join(root, "harness", "test-runs"), "maxdepth": 3, "include": ["result.json", "progress.jsonl", "client.log", "harness.log", "*.png"],
                                         "inline": ["result.json", "progress.jsonl"], "inline_max": 4000000}]}}
    cfg["hosts"].update(extra_hosts or {})
    cfg.update(kw)
    return cfg


class CollectorBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = tempfile.mkdtemp()
        mf.build(self.root)
        with open(os.path.join(self.root, "candidate.json"), "w") as fh:
            json.dump({"schema": "mcdevlab.candidate.v1", "sha256": mf.CAND_B["jar_sha256"], "source_commit": mf.CAND_B["commit"], "label": "bbbb2222"}, fh)
        # results older than 10 s so the collector considers the files settled
        self.api = FakeApi()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def collector(self, cfg=None, hosts=None):
        return collector.Collector(cfg or make_cfg(self.root), self.data, api=self.api, hosts=hosts, log=lambda m: None)


class HarnessFirstClass(CollectorBase):
    def test_runs_normalised_and_mirrored(self):
        col = self.collector()
        col.poll_once()
        app = App(self.data)
        runs = {r["id"]: r for r in app.all_runs()}
        self.assertEqual(len(runs), 6)
        rid = "ctl--flyover-spawn--run-a-0"
        self.assertNotIn("metrics", runs[rid])  # the index is light; heavy fields come from get_run
        r = app.get_run(rid)
        self.assertEqual((r["host"], r["gate"], r["verdict"], r["source"]), ("ctl", "flyover-spawn", "PASS", "harness"))
        self.assertEqual(r["candidate"]["label"], "aaaa1111")
        self.assertEqual(len(r["screenshots"]), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.data, "runs", rid, r["screenshots"][0]["full"])))
        self.assertTrue(os.path.isfile(os.path.join(self.data, "runs", rid, r["screenshots"][0]["thumb"])))
        self.assertEqual(r["log"]["source"], "client.log")
        self.assertTrue(any(m["name"] == "viewer.log_errors" for m in r["metrics"]))
        self.assertIn("frame_ms_p95", [m["name"] for m in r["metrics"]])
        self.assertEqual(len(r["series"]["spawn"]["frame_ms"]), 240)
        # the AMD run reported host amd and is filed under it
        self.assertIn("amd--flyover-spawn--run-c-0", runs)
        # host state
        st = col.pollers["ctl"].state
        self.assertEqual(st["state"], "idle")
        self.assertEqual(st["candidate"]["label"], "bbbb2222")

    def test_unchanged_files_are_not_fetched_again(self):
        col = self.collector()
        col.poll_once()
        fetched = []
        tr = col.pollers["ctl"].transport
        orig = tr.call

        def spy(request, timeout):
            for op in request["ops"]:
                if op["op"] == "fetch":
                    fetched.extend(f["rel"] for f in op["files"])
            return orig(request, timeout)
        tr.call = spy
        rid = "ctl--flyover-spawn--run-a-0"
        before = os.stat(os.path.join(self.data, "runs", rid, "run.json")).st_mtime_ns
        col.poll_once()
        self.assertEqual(fetched, [])
        self.assertEqual(os.stat(os.path.join(self.data, "runs", rid, "run.json")).st_mtime_ns, before)   # not even rewritten
        # a new file does get fetched, and only that one
        shutil.copytree(os.path.join(self.root, "harness/test-runs/run-a-0"), os.path.join(self.root, "harness/test-runs/run-new"))
        old = time.time() - 60
        for base, _d, files in os.walk(os.path.join(self.root, "harness/test-runs/run-new")):
            for f in files:
                os.utime(os.path.join(base, f), (old, old))
        col.poll_once()
        self.assertTrue(fetched)
        self.assertTrue(all(f.startswith("run-new/") for f in fetched))

    def test_per_run_cap_keeps_thumbnails_but_skips_full_size(self):
        cfg = make_cfg(self.root)
        cfg["per_run_cap_mb"] = 0
        col = self.collector(cfg)
        col.poll_once()
        r = App(self.data).get_run("ctl--flyover-spawn--run-a-0")
        self.assertEqual(len(r["screenshots"]), 2)
        self.assertTrue(all(s["full"] is None and s["thumb"] for s in r["screenshots"]))

    def test_prune_originals_budget(self):
        col = self.collector()
        col.poll_once()
        freed = collector.prune_originals(col.store, 1, lambda m: None)
        self.assertGreater(freed, 0)
        r = App(self.data).get_run("ctl--flyover-spawn--run-d-0")
        self.assertTrue(all(s["full"] is None for s in r["screenshots"]))
        self.assertTrue(all(s["thumb"] for s in r["screenshots"]))

    def test_running_harness_dir_is_running_and_not_mirrored(self):
        mf.write_running(self.root, "run-live")
        col = self.collector()
        col.poll_once()
        r = App(self.data).get_run("ctl--harness--run-live")
        self.assertEqual(r["verdict"], "RUNNING")
        self.assertIsNone(r["log"])


class UnreachableHost(CollectorBase):
    def test_one_dead_host_does_not_stall_the_rest(self):
        cfg = make_cfg(self.root, {"gpc": {"label": "gpc", "platform": "Windows", "transport": {"type": "argv", "argv": ["sleep", "30"]},
                                           "sources": [{"type": "node_smoke", "root": "C:\\x", "include": ["*.json"]}]}}, host_timeout_s=2)
        self.api.fail_hosts.add("gpc")
        col = self.collector(cfg)
        t = time.time()
        col.poll_once()
        self.assertLess(time.time() - t, 8)
        self.assertEqual(col.pollers["gpc"].state["state"], "unreachable")
        self.assertIn("timed out", col.pollers["gpc"].state["evidence_error"])
        self.assertEqual(col.pollers["ctl"].state["state"], "idle")
        fleet = App(self.data).fleet()
        self.assertEqual({h["id"]: h["state"] for h in fleet["hosts"]}["gpc"], "unreachable")


class StateAndJobs(CollectorBase):
    def test_paused_and_running_states(self):
        open(os.path.join(self.root, "PAUSE"), "w").close()
        col = self.collector()
        col.poll_once()
        self.assertEqual(col.pollers["ctl"].state["state"], "paused")
        now = time.time()
        self.api.jobs["ctl"] = [job("job-run", "client-smoke", "RUNNING", now - 42, rid="req-live")]
        col.poll_once()
        st = col.pollers["ctl"].state
        self.assertEqual(st["state"], "running")
        self.assertEqual(st["job"]["recipe"], "client-smoke")
        self.assertTrue(st["paused_while_running"])
        stale = job("job-stale", "client-smoke", "RUNNING", now - 400)
        stale.update(state="UNKNOWN", heartbeat=now - 400, note="Supervisor heartbeat is stale")
        self.api.jobs["ctl"] = [stale]
        col.poll_once()
        self.assertEqual(col.pollers["ctl"].state["state"], "stale")

    def test_job_failure_overrides_passing_evidence_and_is_explained(self):
        # evidence PASS (harness) but the Dev Lab job latched a failure
        started = mf.T0 + 0
        self.api.jobs["ctl"] = [job("job-1", "client-smoke", "FAILED", started - 5, started, started + 80,
                                    error={"type": "ClientFailureLatched", "message": "Normal process exit does not erase the original client failure"})]
        col = self.collector()
        col.poll_once()
        r = App(self.data).get_run("ctl--flyover-spawn--run-a-0")
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(r["verdict_raw"], "PASS")
        self.assertIn("ClientFailureLatched", r["reason"])
        self.assertTrue(any(n.startswith("evidence says PASS") for n in r["notes"]))
        self.assertEqual(r["recipe"], "client-smoke")
        self.assertEqual(r["request_id"], "job-1")

    def test_job_without_evidence_gets_reason_from_its_log(self):
        self.api.jobs["ctl"] = [job("job-9", "client-smoke", "FAILED", 1000.0, 1000.0, 1001.0)]
        self.api.logs["job-9"] = "wrapper start\nFeature probe refused: Run label must start feature-plan-\n{\n \"status\": \"X\"\n}"
        col = self.collector()
        col.poll_once()
        r = next(x for x in App(self.data).all_runs() if x.get("job_id") == "job-9")
        self.assertEqual(r["verdict"], "ERROR")
        self.assertIn("Feature probe refused", r["reason"])
        self.assertEqual(r["source"], "dev-lab-job")

    def test_greedy_matching_does_not_steal_runs(self):
        a = mf.T0
        self.api.jobs["ctl"] = [job("job-fast-fail", "client-smoke", "FAILED", a - 9, a - 9, a - 8),
                                job("job-real", "client-smoke", "PASSED", a - 0.5, a - 0.5, a + 80)]
        col = self.collector()
        col.poll_once()
        r = App(self.data).get_run("ctl--flyover-spawn--run-a-0")
        self.assertEqual(r["job_id"], "job-real")
        self.assertEqual(r["verdict"], "PASS")

    def test_api_down_still_reports_evidence(self):
        self.api.fail_hosts.add("ctl")
        col = self.collector()
        col.poll_once()
        st = col.pollers["ctl"].state
        self.assertFalse(st["api_ok"])
        self.assertTrue(st["evidence_ok"])
        self.assertEqual(st["state"], "idle")
        self.assertEqual(len(App(self.data).all_runs()), 6)


class Live(CollectorBase):
    def test_harness_progress_streams_incrementally(self):
        mf.write_running(self.root, "run-live")
        now = time.time()
        self.api.jobs["ctl"] = [job("job-live", "client-smoke", "RUNNING", now - 30)]
        col = self.collector()
        hp = col.pollers["ctl"]
        live = hp.live_poll()
        self.assertTrue(live["active"])
        self.assertEqual(live["kind"], "harness")
        self.assertEqual(len(live["progress"]["t_ms"]), 30)
        self.assertEqual(live["last"]["phase"], "MEASURE")
        self.assertIn("live warning 1", "\n".join(live["log_tail"]))
        # the harness keeps appending (including a half-written last line)
        p = os.path.join(self.root, "harness/test-runs/run-live/progress.jsonl")
        with open(p, "a") as fh:
            for i in range(30, 35):
                fh.write(json.dumps({"t_ms": i * 1000, "step": 1, "step_type": "measure", "phase": "MEASURE", "fps": 50, "frame_ms_p95": 22, "heap_used_mb": 2300}) + "\n")
            fh.write('{"t_ms": 35000, "fp')
        live = hp.live_poll()
        self.assertEqual(len(live["progress"]["t_ms"]), 35)        # no duplicates, partial line held back
        self.assertEqual(live["progress"]["t_ms"][-1], 34000)
        # API view
        app = App(self.data)
        self.assertTrue(app.live("ctl", "job-live")["active"])
        self.assertFalse(app.live("ctl", "other-job")["active"])
        # job ends: live goes inactive
        self.api.jobs["ctl"] = [job("job-live", "client-smoke", "PASSED", now - 30, now - 30, now)]
        live = hp.live_poll()
        self.assertFalse(live["active"])
        self.assertFalse(app.live("ctl", "current")["active"])

    def test_featureplan_phase_timeline_live(self):
        fp = os.path.join(self.root, "fp")
        run = os.path.join(fp, "feature-plan-x-20261005T000000Z")
        os.makedirs(os.path.join(run, "probe"))
        shutil.copy(os.path.join(helpers_dir(), "fp-progress.txt"), os.path.join(run, "probe", "progress.txt"))
        with open(os.path.join(run, "client.log"), "w") as fh:
            fh.write("[00:00:01] [Render thread/INFO] [x/]: hello\n[00:00:02] [Render thread/ERROR] [x/]: bad\n")
        cfg = make_cfg(self.root)
        cfg["hosts"]["ctl"]["sources"] = [{"type": "featureplan_dirs", "root": fp, "maxdepth": 3, "include": ["*/probe/*", "*/client.log"], "inline": ["*/probe/*.txt"]}]
        self.api.jobs["ctl"] = [job("job-fp", "client-smoke", "RUNNING", time.time() - 10)]
        col = self.collector(cfg)
        live = col.pollers["ctl"].live_poll()
        self.assertEqual(live["kind"], "featureplan")
        self.assertEqual(len(live["timeline"]), 28)
        self.assertTrue(any("bad" in ln for ln in live["log_tail"]))


def helpers_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "real")


class FeaturePlanDirs(CollectorBase):
    def test_feature_plan_dir_with_job_and_log(self):
        fp = os.path.join(self.root, "fp")
        rd = os.path.join(fp, "feature-plan-on-20261005-r9")
        os.makedirs(os.path.join(rd, "probe"))
        shutil.copy(os.path.join(helpers_dir(), "fp-pass-result.txt"), os.path.join(rd, "probe", "result.txt"))
        shutil.copy(os.path.join(helpers_dir(), "fp-progress.txt"), os.path.join(rd, "probe", "progress.txt"))
        with open(os.path.join(rd, "probe", "hud.png"), "wb") as fh:
            fh.write(mf.png())
        shutil.copy(os.path.join(helpers_dir(), "client-log-head.log"), os.path.join(rd, "client.log"))
        t = time.time() - 600
        for name, obj in (("input.json", {"jar_sha256": mf.CAND_A["jar_sha256"], "heap": ["-Xmx6G"], "fixture": "Mini", "source_level_sha256": "ab",
                                          "gpu_identity": {"renderer": "NVIDIA GeForce RTX 4060 Ti/PCIe/SSE2", "version": "4.6"}, "manifest_sha256": "m"}),
                          ("process.json", {"pid": 1, "started": t}), ("exit.json", {"exit_code": 0, "elapsed_seconds": 300.0})):
            with open(os.path.join(rd, name), "w") as fh:
                json.dump(obj, fh)
        for base, _d, files in os.walk(rd):
            for f in files:
                os.utime(os.path.join(base, f), (t, t))
        cfg = make_cfg(self.root)
        cfg["hosts"]["ctl"]["sources"] = [cfgmod._fp(fp)]
        self.api.jobs["ctl"] = [job("job-fp", "client-smoke", "PASSED", t - 2, t, t + 300)]
        col = self.collector(cfg)
        col.poll_once()
        r = App(self.data).get_run("ctl--feature-plan--on-20261005-r9")
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["recipe"], "client-smoke")
        self.assertEqual(r["environment"]["max_heap"], "6G")
        self.assertEqual(r["environment"]["scenario"]["gates"].split(",")[0], "200_overlay_p95<=1ms")
        self.assertEqual(len(r["timeline"]), 28)
        self.assertEqual(len(r["screenshots"]), 1)
        self.assertIsNotNone(r["crash"])
        self.assertEqual(r["candidate"]["jar_sha256"], mf.CAND_A["jar_sha256"])
        self.assertGreater(len(r["environment"].get("mods", [])), 100)
        self.assertTrue(any(m["name"] == "overlay_200.overlay_p95_ns" for m in r["metrics"]))
        self.assertIn("result.txt", r["evidence"]["inline_files"])


if __name__ == "__main__":
    unittest.main()
