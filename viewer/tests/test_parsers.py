import json
import unittest

import helpers  # noqa: F401
from helpers import real
from mcdevlab_viewer import parsers as P
from mcdevlab_viewer.model import canon_verdict, parse_iso


class FeaturePlan(unittest.TestCase):
    def test_pass_result_real(self):
        r = P.parse_featureplan_result(real("fp-pass-result.txt"))
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("NVIDIA GeForce RTX 4060 Ti", r["renderer"])
        self.assertTrue(r["gates"].startswith("200_overlay_p95"))
        by = {m["name"]: m for m in r["metrics"]}
        self.assertEqual(by["overlay_200.overlay_p95_ns"]["value"], 53849)
        self.assertEqual(by["overlay_200.overlay_p95_ns"]["limit"], 1000000)       # gate_ns limits the overlay p95
        self.assertNotIn("limit", by["overlay_200.frame_p95_ns"])                   # but not the descriptive frame p95
        self.assertEqual(by["overlay_200.overlay_p95_ns"]["better"], "lower")
        self.assertIsNone(by["overlay_200.gate_ns"]["better"])                      # constants are neutral
        self.assertEqual(by["export_512_png.completion_ms"]["unit"], "ms")
        self.assertIsNone(by["export_512_png.bytes"]["better"])
        names = [c["name"] for c in r["checks"]]
        self.assertIn("bearing_hud_full", names)
        hud = next(c for c in r["checks"] if c["name"] == "bearing_hud_full")
        self.assertEqual(hud["detail"], "Bearing 245° WSW")                     # free text, no metric
        self.assertTrue(any(c["status"] == "LIMIT" for c in r["checks"]))           # LIMIT lines are kept as honest scope notes
        self.assertTrue(r["qa"])

    def test_incomplete_with_reason(self):
        r = P.parse_featureplan_result(real("fp-incomplete.txt"))
        self.assertEqual(r["verdict"], "INCOMPLETE")
        self.assertIn("IllegalStateException", r["reason"])
        self.assertIn("TELEPORTED", r["reason"])

    def test_empty_and_garbage(self):
        self.assertEqual(P.parse_featureplan_result("")["verdict"], "UNKNOWN")
        self.assertEqual(P.parse_featureplan_result("\n\n")["verdict"], "UNKNOWN")
        r = P.parse_featureplan_result("FAIL feature-plan ON gate\nFAIL overlay_200 samples=3 overlay_p95_ns=2000000 gate_ns=1000000")
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("overlay_200", r["reason"])

    def test_numbers(self):
        self.assertEqual(P.to_number("12"), 12)
        self.assertEqual(P.to_number("1.5e3"), 1500.0)
        self.assertIsNone(P.to_number("12ms"))
        self.assertIsNone(P.to_number(True))

    def test_progress_txt(self):
        tl = P.parse_progress_txt(real("fp-progress.txt"))
        self.assertEqual(tl[0]["phase"], "TELEPORT")
        self.assertEqual(len(tl), 28)
        settle = next(e for e in tl if e["phase"] == "SETTLE")
        self.assertGreater(settle["dwell_s"], 9.9)
        self.assertIsNone(tl[-1]["dwell_s"])

    def test_metric_direction(self):
        self.assertEqual(P.guess_better("a.frame_p95_ns"), "lower")
        self.assertEqual(P.guess_better("a.fps_avg"), "higher")
        self.assertIsNone(P.guess_better("a.samples"))
        self.assertIsNone(P.guess_better("a.gate_ns"))
        self.assertIsNone(P.guess_better("items"))
        self.assertEqual(P.guess_unit("x.heap_used_mb_max"), "MB")


class Harness(unittest.TestCase):
    def fixture(self):
        import make_fixtures as mf
        import tempfile
        root = tempfile.mkdtemp()
        d = mf.write_run(root, "r", p95=24.0)
        with open(d + "/result.json") as fh:
            return json.load(fh)

    def test_contract_validates(self):
        obj = self.fixture()
        self.assertEqual(P.validate_harness_result(obj), [])
        bad = dict(obj, verdict="MAYBE", metrics=[{"name": "x", "value": "abc"}])
        self.assertTrue(any("verdict" in p for p in P.validate_harness_result(bad)))
        self.assertTrue(any("metrics[0]" in p for p in P.validate_harness_result(bad)))

    def test_mapping(self):
        f = P.harness_to_run_fields(self.fixture())
        self.assertEqual(f["verdict"], "PASS")
        self.assertEqual(f["scenario"]["name"], "flyover-spawn")
        p95 = next(m for m in f["metrics"] if m["name"] == "frame_ms_p95")
        self.assertEqual((p95["segment"], p95["better"], p95["unit"]), ("spawn", "lower", "ms"))
        neutral = next(m for m in f["metrics"] if m["name"] == "examplemod.chunks_meshed")
        self.assertIsNone(neutral["better"])
        self.assertEqual(len(f["series"]["spawn"]["frame_ms"]), 240)
        self.assertEqual(f["screenshots"][0]["file"], "screenshots/spawn.png")
        self.assertAlmostEqual(f["duration_s"], 80.0)

    def test_progress_jsonl_tolerates_partial_line(self):
        text = '{"t_ms":0,"fps":60}\n{"t_ms":1000,"fps":59}\n{"t_ms":2000,"fp'
        s = P.parse_progress_jsonl(text)
        self.assertEqual(len(s), 2)
        cols = P.progress_series(s)
        self.assertEqual(cols["fps"], [60, 59])

    def test_downsample(self):
        self.assertEqual(len(P.downsample(list(range(5000)), 600)), 600)
        self.assertEqual(P.downsample([1, 2], 600), [1, 2])


class Smokes(unittest.TestCase):
    def test_fd(self):
        p = P.parse_smoke_fd(json.loads(real("fd-smoke-summary.json")))
        self.assertEqual(p["verdict"], "PASS")
        self.assertEqual(p["candidate"]["commit"], "e18946e3bfda523605b2be47aeeec79548a6129e")
        self.assertIn("radeonsi", p["gl_renderer"])
        self.assertTrue(all(c["status"] == "PASS" for c in p["checks"] if c["name"].startswith("renderer")))
        self.assertAlmostEqual(p["duration_s"], 61.6)
        self.assertEqual(p["max_heap"], "-Xmx6G")

    def test_gpc_incomplete(self):
        p = P.parse_smoke_gpc(json.loads(real("gpc-summary.json")))
        self.assertEqual(p["verdict"], "INCOMPLETE")
        self.assertEqual(p["reason"], "client exited without a feature-plan result.txt")
        self.assertIn("RTX 4080", p["gl_renderer"])
        m = {x["name"]: x["value"] for x in p["metrics"]}
        self.assertEqual(m["log_error_count"], 444)
        self.assertEqual(m["gl_error_count"], 728)
        self.assertEqual(p["mode"], "feature-plan")
        self.assertEqual(p["started_ts"], parse_iso("2026-10-05T06:58:57.8538157Z"))

    def test_mac_dryrun_refused(self):
        p = P.parse_smoke_gpc(json.loads(real("mac-dryrun.json")))
        self.assertEqual(p["verdict"], "BLOCKED")
        self.assertTrue(p["dry_run"])
        failed = [c["name"] for c in p["checks"] if c["status"] == "FAIL"]
        self.assertEqual(failed, ["gui_session_unlocked", "owner_idle_ge_600s"])
        self.assertEqual(p["hardware"]["cpu"], "Apple M4 Pro")

    def test_c39(self):
        p = P.parse_c39_validation(json.loads(real("c39-validation.json")))
        m = {x["name"]: x["value"] for x in p["metrics"]}
        self.assertEqual(m["frame_ms_p95"], 43.204958)
        self.assertEqual(m["hitches"], 9)
        self.assertEqual(p["verdict"], "PASS")
        self.assertFalse(p["matched_comparison_ok"])

    def test_ct224_v2(self):
        p = P.parse_smoke_ct224({"status": "REFUSED_OR_FAILED", "seconds": 0, "gpu": {}, "wrapper_exit": 1})
        self.assertEqual(p["verdict"], "ERROR")

    def test_verdict_mapping(self):
        for raw, want in (("PASSED", "PASS"), ("DRYRUN_OK", "PASS"), ("REFUSED", "BLOCKED"), ("NOT_VALIDATED", "BLOCKED"),
                          ("CRASH", "ERROR"), ("TIMED_OUT", "TIMEOUT"), ("weird", "UNKNOWN"), (None, "UNKNOWN")):
            self.assertEqual(canon_verdict(raw), want)


class Jobs(unittest.TestCase):
    JOB = {"id": "job-1", "request_id": "r-1", "instance": "atlas", "recipe": "client-smoke", "state": "FAILED", "created": 10.0,
           "receipt": {"kind": "client", "started": 11.0, "finished": 20.0, "exit_code": 0, "error": {"type": "ClientFailureLatched", "message": "latched"}, "artifacts": []}}

    def test_latched_failure_is_fail_not_error(self):
        j = P.parse_job(self.JOB)
        self.assertEqual(P.job_verdict(j), "FAIL")
        self.assertEqual(j["started"], 11.0)
        j2 = P.parse_job(dict(self.JOB, receipt={"kind": "client", "error": None, "exit_code": 1}))
        self.assertEqual(P.job_verdict(j2), "ERROR")

    def test_running_job_without_receipt(self):
        j = P.parse_job({"id": "job-2", "request_id": "r", "recipe": "x", "state": "RUNNING", "created": 5.0})
        self.assertEqual(j["started"], 5.0)
        self.assertEqual(P.job_verdict(j), "RUNNING")

    def test_extract_reason(self):
        self.assertIn("failures: harness_exit_0", P.extract_reason("a\nRESULT FAIL run=1 duration=1s failures=['harness_exit_0: exit 1']"))
        self.assertTrue(P.extract_reason("x\nTraceback (most recent call last):\n  File \"a\", line 1\nFileNotFoundError: [Errno 2] nope").startswith("FileNotFoundError"))
        self.assertIn("refused", P.extract_reason("Feature probe refused: label\n{\n \"status\": \"X\"\n}"))
        self.assertIn("owner_idle", P.extract_reason('[smoke] REFUSED: owner_idle_ge_600s\n{"status": "REFUSED"}'))
        self.assertIsNone(P.extract_reason(""))


if __name__ == "__main__":
    unittest.main()
