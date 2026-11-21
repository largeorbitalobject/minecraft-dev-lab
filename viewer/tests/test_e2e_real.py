"""End to end against the real fleet: one collector pass over real nodes (read-only), the server on a test port,
every API endpoint fetched. Skipped when this is not the Proxmox host (no `pct`, no Dev Lab zipapp)."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request

import helpers  # noqa: F401
from mcdevlab_viewer import collector, config as cfgmod, server

ON_PVE = shutil.which("pct") is not None and os.path.exists(cfgmod.PYZ) and os.path.exists(cfgmod.FLEET_JSON)


@unittest.skipUnless(ON_PVE, "needs the Proxmox host and the Dev Lab zipapp")
class RealFleet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp()
        cls.col = collector.Collector(cfgmod.load_config(None), cls.data, log=lambda m: None)
        t = time.time()
        cls.col.poll_once()
        cls.poll_s = time.time() - t
        cls.srv = server.make_server(cls.data, 0, "127.0.0.1")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data, ignore_errors=True)

    def get(self, path, raw=False):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=30) as r:
            b = r.read()
            return b if raw else json.loads(b)

    def test_collector_pass_was_bounded_and_found_evidence(self):
        self.assertLess(self.poll_s, 120)
        states = {h: p.state for h, p in self.col.pollers.items()}
        ok = [h for h, s in states.items() if s.get("evidence_ok")]
        self.assertGreaterEqual(len(ok), 3, states)
        self.assertTrue(states["proxmox-test"]["evidence_ok"], states["proxmox-test"])
        runs = self.get("/api/runs?limit=2000")["runs"]
        self.assertGreater(len(runs), 30)
        self.assertGreater(len({r["host"] for r in runs}), 3)

    def test_every_endpoint_on_real_data(self):
        st = self.get("/api/status")
        self.assertGreater(st["runs"], 30)
        fleet = self.get("/api/fleet")
        self.assertEqual({h["id"] for h in fleet["hosts"]} >= {"ctl", "proxmox-test", "amd", "gpc", "mac"}, True)
        fp = [r for r in self.get("/api/runs?gate=feature-plan&host=proxmox-test&limit=100")["runs"] if r["verdict"] == "PASS"]
        self.assertGreaterEqual(len(fp), 2)
        newest, older = fp[0], fp[1]
        d = self.get("/api/run/" + newest["id"])
        self.assertTrue(d["run"]["metrics"])
        self.assertTrue(d["run"]["screenshots"])
        self.assertTrue(d["run"]["log"])
        thumb = self.get("/files/%s/%s" % (newest["id"], d["run"]["screenshots"][0]["thumb"]), raw=True)
        self.assertGreater(len(thumb), 1000)
        ld = self.get("/api/logdiff?base=%s&cand=%s" % (older["id"], newest["id"]))
        self.assertIn("new", ld)
        cmpd = self.get("/api/compare?base=%s&cand=%s" % (older["id"], newest["id"]))
        self.assertEqual(cmpd["mode"], "runs")
        labels = {r["candidate"]["label"] for r in fp if r["candidate"]["label"]}
        self.assertGreaterEqual(len(labels), 2)
        a, b = sorted(labels)[:2]
        self.assertEqual(self.get("/api/compare?base=%s&cand=%s" % (a, b))["mode"], "commits")
        self.assertTrue(self.get("/api/history")["groups"])
        self.assertTrue(self.get("/api/candidates")["candidates"])
        self.assertIn("lines", self.get("/api/log/%s?tail=5" % newest["id"]))
        for h in ("ctl", "proxmox-test", "amd", "gpc", "mac"):
            self.assertIn("active", self.get("/api/live/%s/current" % h))
        index = self.get("/", raw=True)
        self.assertIn(b"Dev Lab Viewer", index)


if __name__ == "__main__":
    unittest.main()
