import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import helpers  # noqa: F401
import make_fixtures as mf
from mcdevlab_viewer import collector, server
from mcdevlab_viewer.app import App
from test_collector import FakeApi, make_cfg


class Served(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.data = tempfile.mkdtemp(), tempfile.mkdtemp()
        mf.build(cls.root)
        col = collector.Collector(make_cfg(cls.root), cls.data, api=FakeApi(), log=lambda m: None)
        col.poll_once()
        cls.app = App(cls.data)
        cls.srv = server.make_server(cls.data, 0, "127.0.0.1")
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.root, ignore_errors=True)
        shutil.rmtree(cls.data, ignore_errors=True)

    def get(self, path, raw=False):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=10) as r:
            body = r.read()
            return (r, body) if raw else json.loads(body)


class AppLogic(Served):
    def test_compare_commits_flags_regression_beyond_noise(self):
        d = self.app.compare("aaaa1111", "bbbb2222")
        self.assertEqual(d["mode"], "commits")
        cell = next(c for c in d["cells"] if c["host"] == "ctl")
        self.assertTrue(cell["env"]["comparable"])
        p95 = next(r for r in cell["deltas"] if r["name"] == "frame_ms_p95")
        self.assertEqual(p95["state"], "worse")
        self.assertEqual(p95["noise"]["n"], 3)                       # three runs of the baseline code
        self.assertIsNotNone(p95["noise"]["band"])
        fps = next(r for r in cell["deltas"] if r["name"] == "fps_avg")
        self.assertEqual(fps["state"], "worse")                      # higher is better, fell
        self.assertTrue(cell["log_diff_available"])

    def test_compare_across_gpus_refuses_deltas(self):
        d = self.app.compare("ctl--flyover-spawn--run-a-0", "amd--flyover-spawn--run-c-0")
        self.assertEqual(d["mode"], "runs")
        res = d["result"]
        self.assertFalse(res["env"]["comparable"])
        fields = {m["field"] for m in res["env"]["mismatches"] if m["severity"] == "hard"}
        self.assertTrue({"host", "GL renderer"} <= fields)
        self.assertEqual({r["state"] for r in res["metrics"] if r["better"]}, {"not_comparable"})   # c is faster, still not judged

    def test_missing_side_is_shown_not_hidden(self):
        d = self.app.compare("aaaa1111", "cccc3333")
        by = {(c["host"], c["gate"]): c for c in d["cells"]}
        self.assertIsNone(by[("amd", "flyover-spawn")]["base"])     # baseline never ran on amd
        self.assertIsNotNone(by[("amd", "flyover-spawn")]["cand"])
        self.assertEqual(by[("ctl", "flyover-spawn")]["cand"]["verdict"], "FAIL")
        env = by[("ctl", "flyover-spawn")]["env"]
        self.assertFalse(env["comparable"])                          # a crashed run's zeros are not "100% better"
        self.assertIn("run verdict", [m["field"] for m in env["mismatches"]])
        self.assertEqual({r["state"] for r in by[("ctl", "flyover-spawn")]["deltas"] if r["better"]}, {"not_comparable"})

    def test_auto_baseline_is_an_earlier_pass_of_a_different_build(self):
        d = self.app.run_detail("ctl--flyover-spawn--run-b-0")
        self.assertEqual(d["compare"]["base"].split("--")[-1][:5], "run-a")
        d0 = self.app.run_detail("ctl--flyover-spawn--run-a-0")
        self.assertIsNone(d0["base"])                                # nothing earlier to compare with

    def test_log_diff_between_runs(self):
        d = self.app.log_diff("ctl--flyover-spawn--run-a-0", "ctl--flyover-spawn--run-b-0")
        sigs = [g["sig"] for g in d["new"]]
        self.assertEqual(len(sigs), 1)
        self.assertTrue(sigs[0].startswith("ERROR [examplemod/] Mesh upload failed for chunk <n> at <hex>"))

    def test_crash_signature_on_failed_run(self):
        r = self.app.get_run("ctl--flyover-spawn--run-d-0")
        self.assertEqual(r["crash"]["exception"], "java.lang.NullPointerException")
        self.assertEqual(r["crash"]["culprit"]["mod"], "examplemod")

    def test_history_groups(self):
        h = self.app.history()
        g = next(x for x in h["groups"] if x["host"] == "ctl")
        self.assertEqual(g["n"], 5)
        self.assertIn("spawn:frame_ms_p95", [m["key"] for m in g["metrics"]])
        self.assertEqual([r["label"] for r in g["runs"]][:3], ["aaaa1111"] * 3)

    def test_fleet_board(self):
        f = self.app.fleet()
        ctl = next(h for h in f["hosts"] if h["id"] == "ctl")
        self.assertEqual(ctl["state"], "idle")
        self.assertEqual(ctl["recipes"][0]["key"], "flyover-spawn")
        self.assertEqual(ctl["recipes"][0]["verdict"], "FAIL")      # latest run on the gate


class Http(Served):
    def test_every_endpoint(self):
        rid = "ctl--flyover-spawn--run-b-0"
        self.assertEqual(self.get("/api/status")["runs"], 6)
        self.assertEqual(len(self.get("/api/fleet")["hosts"]), 2)
        self.assertEqual(self.get("/api/runs?host=ctl&gates=1")["total"], 5)
        self.assertEqual(self.get("/api/runs?verdict=FAIL")["total"], 1)
        self.assertEqual(self.get("/api/runs?commit=bbbb")["total"], 1)
        d = self.get("/api/run/" + rid)
        self.assertEqual(d["run"]["id"], rid)
        self.assertTrue(d["compare"])
        self.assertIsNone(self.get("/api/run/" + rid + "?base=none")["compare"])
        self.assertEqual(self.get("/api/log/" + rid + "?level=ERROR")["total"], 2)
        self.assertEqual(len(self.get("/api/logdiff?base=ctl--flyover-spawn--run-a-0&cand=" + rid)["new"]), 1)
        self.assertEqual(self.get("/api/compare?base=aaaa1111&cand=bbbb2222")["mode"], "commits")
        self.assertEqual(self.get("/api/compare?base=ctl--flyover-spawn--run-a-0&cand=" + rid)["mode"], "runs")
        self.assertGreaterEqual(len(self.get("/api/candidates")["candidates"]), 3)
        self.assertTrue(self.get("/api/history?host=ctl")["groups"])
        live = self.get("/api/live/ctl/current")
        self.assertFalse(live["active"])
        self.assertEqual(live["poll_ms"], 10000)
        self.assertEqual(self.get("/healthz"), {"ok": True})

    def test_errors_are_json(self):
        for path, code in (("/api/run/nope", 404), ("/api/nope", 404), ("/api/logdiff", 400), ("/api/compare?base=x", 400), ("/api/log/nope", 404)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path)
            self.assertEqual(cm.exception.code, code, path)
            self.assertIn("error", json.loads(cm.exception.read()))

    def test_static_and_files(self):
        r, body = self.get("/", raw=True)
        self.assertIn(b"Dev Lab Viewer", body)
        self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
        r, body = self.get("/static/fonts/archivo-700.woff2", raw=True)
        self.assertEqual(r.headers["Content-Type"], "font/woff2")
        self.assertGreater(len(body), 10000)
        rid = "ctl--flyover-spawn--run-b-0"
        run = self.get("/api/run/" + rid)["run"]
        r, body = self.get("/files/%s/%s" % (rid, run["screenshots"][0]["thumb"]), raw=True)
        self.assertTrue(r.headers["Content-Type"].startswith("image/"))
        r, body = self.get("/files/%s/%s" % (rid, run["screenshots"][0]["full"]), raw=True)
        self.assertEqual(body[:4], b"\x89PNG")
        r, body = self.get("/files/%s/files/result.json" % rid, raw=True)
        self.assertEqual(json.loads(body)["schema"], "mcdevlab.harness.result.v1")

    def test_no_path_traversal(self):
        for path in ("/files/../../etc/passwd", "/files/x/../../../etc/passwd", "/static/../app.py", "/static/..%2f..%2fetc/passwd",
                     "/files/ctl--flyover-spawn--run-b-0/mirror.json", "/files/ctl--flyover-spawn--run-b-0/%2e%2e/%2e%2e/etc/passwd"):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", path)
            resp = c.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 404, path)
            self.assertNotIn(b"root:", body)
            c.close()

    def test_head_and_gzip(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("HEAD", "/api/fleet")
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (200, b""))
        c.request("GET", "/api/run/ctl--flyover-spawn--run-b-0", headers={"Accept-Encoding": "gzip"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        c.close()

    def test_live_json_and_sse_stream(self):
        live = {"host": "ctl", "active": True, "job": {"id": "job-x", "recipe": "r"}, "kind": "harness", "progress": {"t_ms": [0, 1000]}, "updated_ts": 1.0}
        self.app.store.write_json(self.app.store.p("live", "ctl.json"), live)
        try:
            d = self.get("/api/live/ctl/job-x")
            self.assertTrue(d["active"])
            self.assertEqual(d["poll_ms"], 2000)
            self.assertFalse(self.get("/api/live/ctl/other")["active"])
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/live/ctl/job-x/stream")
            r = c.getresponse()
            self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
            first = r.fp.readline().decode()
            self.assertTrue(first.startswith("data: "))
            self.assertEqual(json.loads(first[6:])["job"]["id"], "job-x")
            live["active"] = False
            self.app.store.write_json(self.app.store.p("live", "ctl.json"), live)       # stream ends once the job is over
            rest = r.fp.read()
            self.assertIn(b'"active":false', rest)
            c.close()
        finally:
            os.unlink(self.app.store.p("live", "ctl.json"))

    def test_server_is_read_only(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        for method in ("POST", "PUT", "DELETE"):
            c.request(method, "/api/fleet", body=b"{}")
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 501, method)               # BaseHTTPRequestHandler: no do_POST etc.
        c.close()



class RunCache(Served):
    """The index holds light runs; full runs go through a size-bounded LRU and are reloaded when run.json changes."""

    def test_index_is_light_and_full_run_matches_disk(self):
        app = App(self.data)
        for light in app.all_runs():
            self.assertFalse(set(light) & {"metrics", "progress", "steps", "series"})
            on_disk = app.store.load_run(light["id"])
            self.assertEqual(app.get_run(light["id"]), on_disk)
            self.assertEqual(app.summary(light), app.summary(on_disk))

    def test_lru_stays_within_budget(self):
        app = App(self.data, {"run_cache_mb": 0.001})   # ~1 KB: room for one run at most
        ids = [r["id"] for r in app.all_runs()]
        for rid in ids:
            self.assertIsNotNone(app.get_run(rid))
        self.assertEqual(len(app._full), 1)
        self.assertEqual(app._full_bytes, os.path.getsize(app.store.p("runs", ids[-1], "run.json")))
        app = App(self.data)
        for rid in ids:
            app.get_run(rid)
        self.assertEqual(len(app._full), len(ids))
        self.assertLessEqual(app._full_bytes, app._full_budget)

    def test_changed_run_json_is_reloaded(self):
        app = App(self.data)
        rid = "ctl--flyover-spawn--run-a-0"
        path = app.store.p("runs", rid, "run.json")
        before = app.get_run(rid)
        changed = dict(before, reason="edited")
        app.store.write_json(path, changed)
        try:
            st = os.stat(path)
            os.utime(path, (st.st_atime, st.st_mtime + 5))
            app.refresh(force=True)
            self.assertEqual(app.get_run(rid)["reason"], "edited")
            self.assertEqual(app.get_light(rid)["reason"], "edited")
        finally:
            app.store.write_json(path, before)

if __name__ == "__main__":
    unittest.main()
