import copy
import unittest

import helpers  # noqa: F401
from mcdevlab_viewer import compare as C


def run(rid="r", host="ctl", gate="g", renderer="NVIDIA X", vals=None, **env):
    vals = vals or {"frame_ms_p95": 24.0}
    e = {"gl_renderer": renderer, "os": "Linux amd64 7.0", "minecraft": "1.21.1", "max_heap": "6G", "window": [1280, 800],
         "scenario": {"name": "s", "sha256": "abc"}}
    e.update(env)
    return {"id": rid, "host": host, "gate": gate, "environment": e, "candidate": {"jar_sha256": rid},
            "metrics": [{"name": k, "segment": "seg", "unit": "ms", "value": v, "better": "higher" if k == "fps_avg" else "lower"} for k, v in vals.items()]}


class Env(unittest.TestCase):
    def test_same_env_is_comparable(self):
        self.assertTrue(C.compare_env(run("a"), run("b"))["comparable"])

    def test_renderer_host_scenario_settings_refuse(self):
        base = run("a")
        for change, field in ((dict(renderer="AMD Y"), "GL renderer"), (dict(host="amd"), "host"), (dict(max_heap="8G"), "max heap"),
                              (dict(window=[1920, 1080]), "window"), (dict(scenario={"name": "other", "sha256": "abc"}), "scenario"),
                              (dict(scenario={"name": "s", "sha256": "zzz"}), "scenario sha256"), (dict(minecraft="1.20.1"), "Minecraft"),
                              (dict(gate="x"), "gate")):
            cand = run("b", **{k: v for k, v in change.items() if k not in ("host", "gate")}) if "host" not in change and "gate" not in change else None
            if cand is None:
                cand = run("b")
                cand.update({k: v for k, v in change.items() if k in ("host", "gate")})
            env = C.compare_env(base, cand)
            self.assertFalse(env["comparable"], field)
            self.assertIn(field, [m["field"] for m in env["mismatches"] if m["severity"] == "hard"])

    def test_driver_detail_is_not_a_renderer_change(self):
        a = run("a", renderer="AMD Radeon Graphics (radeonsi, gfx1151, LLVM 20.1.8, DRM 3.64, 7.0.0-34-generic)")
        b = run("b", renderer="AMD Radeon Graphics (radeonsi, gfx1151, LLVM 21.0.0, DRM 3.65, 7.1.0-1-generic)")
        self.assertTrue(C.compare_env(a, b)["comparable"])

    def test_missing_facts_are_unverified_not_refused(self):
        a, b = run("a"), run("b")
        del b["environment"]["gl_renderer"]
        env = C.compare_env(a, b)
        self.assertTrue(env["comparable"])
        self.assertIn("GL renderer", [u["field"] for u in env["unverified"]])

    def test_candidate_mod_difference_is_expected(self):
        a, b = run("a"), run("b")
        a["environment"]["mods"] = [{"id": "m", "version": "1"}, {"id": "own", "version": "1.0"}]
        b["environment"]["mods"] = [{"id": "m", "version": "1"}, {"id": "own", "version": "1.1"}]
        env = C.compare_env(a, b)
        self.assertTrue(env["comparable"])
        self.assertTrue(any("assumed candidate mod: own" in n for n in env["notes"]))
        b["environment"]["mods"][0]["version"] = "2"           # a second mod changed: not the candidate any more
        self.assertFalse(C.compare_env(a, b)["comparable"])

    def test_failed_run_is_not_a_measurement(self):
        a, b = run("a"), run("b")
        a["verdict"], b["verdict"] = "PASS", "FAIL"
        env = C.compare_env(a, b)
        self.assertFalse(env["comparable"])
        self.assertIn("run verdict", [m["field"] for m in env["mismatches"]])
        b["verdict"] = "PASS"
        self.assertTrue(C.compare_env(a, b)["comparable"])

    def test_source_flag_refuses(self):
        a, b = run("a"), run("b")
        b["environment"]["matched_comparison_ok"] = False
        self.assertFalse(C.compare_env(a, b)["comparable"])


class Deltas(unittest.TestCase):
    def rows(self, a, b, **kw):
        return {r["name"]: r for r in C.compare_runs(a, b, **kw)["metrics"]}

    def test_direction(self):
        a = run("a", vals={"frame_ms_p95": 24.0, "fps_avg": 58.0})
        b = run("b", vals={"frame_ms_p95": 31.0, "fps_avg": 62.0})
        r = self.rows(a, b)
        self.assertEqual(r["frame_ms_p95"]["state"], "worse")     # lower is better, went up
        self.assertEqual(r["fps_avg"]["state"], "better")         # higher is better, went up
        self.assertAlmostEqual(r["frame_ms_p95"]["delta"], 7.0)
        self.assertAlmostEqual(r["frame_ms_p95"]["pct"], 29.1666, places=3)

    def test_neutral_and_same_and_missing(self):
        a, b = run("a", vals={"x": 1.0, "y": 5.0}), run("b", vals={"x": 1.0, "z": 3.0})
        a["metrics"][1]["better"] = None
        r = self.rows(a, b)
        self.assertEqual(r["x"]["state"], "same")
        self.assertEqual(r["y"]["state"], "missing_cand")
        self.assertEqual(r["z"]["state"], "missing_base")

    def test_not_comparable_withholds_judgement(self):
        a, b = run("a", renderer="NVIDIA X"), run("b", renderer="AMD Y", vals={"frame_ms_p95": 12.0})
        res = C.compare_runs(a, b)
        self.assertFalse(res["env"]["comparable"])
        row = res["metrics"][0]
        self.assertEqual(row["state"], "not_comparable")          # no better/worse even though 12 < 24
        self.assertEqual(res["summary"]["not_comparable"], 1)
        self.assertEqual(row["delta"], -12.0)                      # raw number kept, never judged

    def test_noise_needs_three_runs(self):
        base = run("a", vals={"frame_ms_p95": 24.0})
        cand = run("b", vals={"frame_ms_p95": 24.8})
        two = [run("a1", vals={"frame_ms_p95": 24.0}), run("a2", vals={"frame_ms_p95": 24.6})]
        r = self.rows(base, cand, base_group=two)["frame_ms_p95"]
        self.assertEqual(r["state"], "worse")                       # n=2: noise unknown, still judged
        self.assertEqual(r["noise"]["n"], 2)
        self.assertIsNone(r["noise"]["band"])
        three = two + [run("a3", vals={"frame_ms_p95": 23.5})]
        r = self.rows(base, cand, base_group=three)["frame_ms_p95"]
        self.assertEqual(r["state"], "within_noise")                # 0.8 < 2 sd
        self.assertEqual(r["noise"]["n"], 3)
        big = run("b2", vals={"frame_ms_p95": 31.0})
        r = self.rows(base, big, base_group=three)["frame_ms_p95"]
        self.assertEqual(r["state"], "worse")

    def test_limit_flag(self):
        a, b = run("a", vals={"m": 1.0}), run("b", vals={"m": 2.0})
        b["metrics"][0]["limit"] = 1.5
        self.assertTrue(self.rows(a, b)["m"].get("over_limit"))

    def test_key_metrics_picks_gate_metrics(self):
        r = run("a", gate="feature-plan")
        r["metrics"] = [{"name": "overlay_200.overlay_p95_ns", "segment": None, "value": 1, "better": "lower"},
                        {"name": "other.thing_ns", "segment": None, "value": 2, "better": "lower"}]
        self.assertEqual([m["name"] for m in C.key_metrics(r)][0], "overlay_200.overlay_p95_ns")


if __name__ == "__main__":
    unittest.main()
