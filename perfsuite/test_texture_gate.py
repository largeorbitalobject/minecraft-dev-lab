#!/usr/bin/env python3
"""Semantic boundary checks for the texture scorecard, including its consumers."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gen
import run
import score
import summarize
import texture_gate as gate


def sample(ms, fine=0, regional=0):
    return {"type": "orbitalatlas:map_state", "status": "PASS", "started_ms": 1000 + ms,
            "message": ("situation=OUTDOORS lastMap=view:10,detT:%d,detS:0 "
                        "lastRegional=view:20,detT:0,detS:%d" % (fine, regional))}


def result(samples):
    return {"scenario": {"name": "atlas-ps-q-texture-fd"}, "verdict": "PASS",
            "steps": [{"type": "orbitalatlas:open_map", "status": "PASS",
                       "args": {"span": 128}, "started_ms": 1000}] + samples,
            "realism": {"detail_timing_representative": True, "verdict_notes": []}}


META = {"fixture": "owner-dungeon-20261117-consistent", "props": "orbitalatlas.playProfile=true\n"}


class TextureGateTests(unittest.TestCase):
    def test_zero_full_window_fails(self):
        card = gate.evaluate(result([sample(0), sample(60000)]), META)
        self.assertEqual(card["verdict"], "FAIL")
        self.assertIn("ZERO_TEXTURED_TILES_IN_60S", card["reasons"])
        self.assertIsNone(card["first_textured_ms"])

    def test_surface_count_at_deadline_passes(self):
        card = gate.evaluate(result([sample(0), sample(60000, regional=1)]), META)
        self.assertEqual(card["verdict"], "PASS")
        self.assertEqual(card["first_textured_ms"], 60000)
        self.assertEqual(card["first_textured_lower_bound_ms"], 0)

    def test_native_count_and_surface_count_both_count(self):
        card = gate.evaluate(result([sample(25, fine=1, regional=2), sample(60001)]), META)
        self.assertEqual(card["verdict"], "PASS")
        self.assertEqual(card["max_textured_tiles"], 3)
        self.assertEqual(card["first_textured_ms"], 25)

    def test_draw_submissions_can_exceed_view_census(self):
        # Observed on the real FD run: detail draw submissions can overlap.
        card = gate.evaluate(result([sample(25, fine=12), sample(60000)]), META)
        self.assertEqual(card["verdict"], "PASS")
        self.assertEqual(card["max_textured_tiles"], 12)

    def test_silently_failed_surface_teleport_cannot_pass_with_cave_textures(self):
        cave = sample(60000, fine=1)
        cave["message"] = cave["message"].replace("OUTDOORS", "CAVE/ROCK")
        card = gate.evaluate(result([sample(0), cave]), META)
        self.assertEqual(card["verdict"], "FAIL")
        self.assertTrue(any("plain surface" in reason for reason in card["reasons"]))

    def test_one_ms_late_does_not_rescue_deadline(self):
        card = gate.evaluate(result([sample(0), sample(60001, fine=1)]), META)
        self.assertEqual(card["verdict"], "FAIL")
        self.assertIn("FIRST_TEXTURED_TILE_AFTER_60S", card["reasons"])

    def test_post_close_cached_census_never_counts(self):
        r = result([sample(0), {"type": "orbitalatlas:close_map", "started_ms": 2000}, sample(60000, fine=1)])
        card = gate.evaluate(r, META)
        self.assertEqual(card["verdict"], "FAIL")
        self.assertIsNone(card["first_textured_ms"])

    def test_teleport_or_quit_ends_texture_observation(self):
        for boundary in ({"type": "teleport"}, {"type": "quit"},
                         {"type": "command", "args": {"cmd": "execute at @p run tp @p 1 2 3"}}):
            card = gate.evaluate(result([sample(0), boundary, sample(60000, fine=1)]), META)
            self.assertEqual(card["verdict"], "FAIL")
            self.assertIsNone(card["first_textured_ms"])

    def test_short_window_is_incomplete_even_with_textures(self):
        card = gate.evaluate(result([sample(0, fine=1), sample(59999, fine=1)]), META)
        self.assertIn("INCOMPLETE_60S_OBSERVATION", card["reasons"])

    def test_failed_quit_or_later_assert_cannot_pass_texture_scorecard(self):
        r = result([sample(0, fine=1), sample(60000)])
        r["verdict"] = "FAIL"
        self.assertIn("HARNESS_NOT_PASS", gate.evaluate(r, META)["reasons"])

    def test_slow_exit_requires_existing_full_receipt_contract(self):
        r = result([sample(0, fine=1), sample(60000)])
        r.update(verdict="ERROR", runner_verdict="WARN_SLOW_EXIT")
        receipt = dict(status="WARN_SLOW_EXIT", exit_confirmed=True, minecraft_exit_confirmed=True,
                       no_minecraft_jvm=True, slow_exit_warning=True, problems=[])
        self.assertEqual(gate.evaluate(r, META, receipt=receipt)["verdict"], "PASS")
        receipt["no_minecraft_jvm"] = False
        self.assertIn("HARNESS_NOT_PASS", gate.evaluate(r, META, receipt=receipt)["reasons"])

    def test_missing_renderer_and_missing_all_telemetry_fail(self):
        bad = sample(60000)
        bad["message"] = "situation=OUTDOORS lastRegional=view:20,detT:1,detS:0"
        for samples in ([], [sample(0), bad]):
            with self.subTest(samples=len(samples)):
                self.assertEqual(gate.evaluate(result(samples), META)["verdict"], "FAIL")

    def test_nonconsistent_fixture_fails(self):
        card = gate.evaluate(result([sample(0, fine=1), sample(60000)]), {"fixture": "old-seeded"})
        self.assertIn("CONSISTENT_FIXTURE_REQUIRED", card["reasons"])

    def test_missing_or_contradictory_product_profile_fails(self):
        for props in ("", "orbitalatlas.playProfile=false", "orbitalatlas.playProfile=true\norbitalatlas.playProfile=false"):
            card = gate.evaluate(result([sample(0, fine=1), sample(60000)]), {**META, "props": props})
            self.assertIn("PRODUCT_PROFILE_REQUIRED", card["reasons"])

    def test_runner_texture_props_add_product_profile_only_to_texture(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run, "HERE", tmp):
            (Path(tmp) / "manifest.json").write_text(json.dumps({"props": {"p": ["orbitalatlas.diag=true"]}, "arms": {"base": {"props": "p"}}}))
            plain = Path(run.props_file("base")).read_text()
            texture = Path(run.props_file("base", "q-texture")).read_text()
            self.assertNotIn("playProfile", plain)
            self.assertIn("orbitalatlas.playProfile=true", texture)
            (Path(tmp) / "manifest.json").write_text(json.dumps({"props": {"p": ["orbitalatlas.playProfile=false"]}, "arms": {"base": {"props": "p"}}}))
            with self.assertRaises(ValueError):
                run.props_file("base", "q-texture")

    def test_seeded_and_unmeasured_realism_ineligible(self):
        for realism in ({"detail_timing_representative": False, "verdict_notes": ["SEEDED_RECAPTURE n=21 station=1"]},
                        {"detail_timing_representative": None}):
            with self.subTest(realism=realism):
                r = result([sample(0, fine=1), sample(60000)])
                r["realism"] = realism
                card = gate.evaluate(r, META)
                self.assertEqual(card["verdict"], "FAIL")

    def test_generated_scenarios_reach_sixty_and_use_consistent_fixtures(self):
        for host in ("fd", "ct224", "gpc", "mac"):
            scenario = gen.q_texture(host)
            self.assertTrue(scenario["large_fixture"].endswith("-consistent"))
            steps = scenario["steps"]
            start = next(i for i, s in enumerate(steps) if s["type"] == "orbitalatlas:open_map")
            end = next(i for i, s in enumerate(steps) if s["type"] == "orbitalatlas:close_map")
            polls = [i for i in range(start, end) if steps[i]["type"] == "orbitalatlas:map_state"]
            self.assertEqual(len(polls), 61)
            waits = [s["seconds"] for s in steps[start:polls[-1]] if s["type"] == "wait_seconds"]
            self.assertEqual(len(waits), 60)
            self.assertEqual(sum(waits), 60)
            tail = steps[polls[-1] + 1:end]
            self.assertEqual(tail[0], {"type": "screenshot", "name": "texture_surface_60s"})
            if host == "fd":
                self.assertEqual([step["type"] for step in tail], ["screenshot", "assert_log", "wait_seconds"])
                self.assertEqual(tail[1]["since_step"], polls[-1] + 1)
                self.assertEqual(tail[2]["seconds"], 4.25)
            else:
                self.assertEqual(len(tail), 1)
            self.assertFalse(any("fill " in s.get("cmd", "") or "forceload" in s.get("cmd", "") for s in steps))

    def test_archive_and_dossier_override_harness_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "result.json").write_text(json.dumps(result([sample(0), sample(60000)])))
            (path / "meta.json").write_text(json.dumps(META))
            card = gate.score_archive(path, write=True)
            self.assertEqual(card["verdict"], "FAIL")
            self.assertEqual(json.loads((path / "texture-scorecard.json").read_text())["verdict"], "FAIL")
            row, _, _, _ = score.score_run(tmp, "fd")
            self.assertEqual(row["harness_verdict"], "PASS")
            self.assertEqual(row["verdict"], "FAIL")
            self.assertIn("ZERO_TEXTURED_TILES_IN_60S", row["reason"])

    def test_clean_cached_realism_does_not_override_missing_raw_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            r = result([sample(0, fine=1), sample(60000)])
            (p / "result.json").write_text(json.dumps(r))
            (p / "meta.json").write_text(json.dumps(META))
            (p / "realism.json").write_text(json.dumps({"detail_timing_representative": True, "stations": []}))
            card = gate.score_archive(p)
            self.assertIn("REALISM_UNMEASURED", card["reasons"])
            self.assertFalse(score.timing_eligibility(tmp, r)["timing_comparison_eligible"])

    def test_json_and_csv_keep_zero_texture_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            archive = p / "fd" / "test-atlas-ps-q-texture-fd"
            archive.mkdir(parents=True)
            (archive / "result.json").write_text(json.dumps(result([sample(0), sample(60000)])))
            (archive / "meta.json").write_text(json.dumps(META))
            data = p / "data"
            with patch.object(score, "ARCH", tmp), patch("sys.argv", ["score.py", "--out", str(data)]), contextlib.redirect_stdout(io.StringIO()):
                score.main()
            jrow = json.loads((data / "runs.jsonl").read_text().splitlines()[0])
            with (data / "runs.csv").open() as stream:
                crow = next(csv.DictReader(stream))
            for row in (jrow, crow):
                self.assertEqual(row["verdict"], "FAIL")
                self.assertIn("ZERO_TEXTURED_TILES_IN_60S", row["reason"])

    def test_queue_returns_nonzero_when_gate_fails(self):
        run.failed_jobs.clear()
        run.stop_all.clear()
        with patch.object(run, "pin"), patch.object(run, "run_one", return_value=1), \
                patch("sys.argv", ["run.py", "--builds", "fixture", "--hosts", "fd", "--scenarios", "q-texture", "--reps", "1"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(), 1)
        run.failed_jobs.clear()

    def test_worker_exception_is_not_a_successful_queue(self):
        run.failed_jobs.clear()
        run.stop_all.clear()
        with patch.object(run, "run_one", side_effect=ValueError("bad archive")), contextlib.redirect_stdout(io.StringIO()):
            run.host_queue("fd", [("q-texture", "fixture", "base", 1, False)], False)
        self.assertTrue(run.failed_jobs.is_set())
        run.failed_jobs.clear()

    def test_seeded_legacy_detail_keeps_harness_pass_but_excludes_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            r = result([sample(0, fine=1), sample(60000)])
            r["scenario"]["name"] = "atlas-ps-q-basepack-fd"
            r["realism"] = {"detail_timing_representative": False, "stations": [{"n": 21}],
                            "verdict_notes": ["SEEDED_RECAPTURE n=21 station=1"]}
            (p / "result.json").write_text(json.dumps(r))
            (p / "meta.json").write_text(json.dumps(META))
            row, _, _, _ = score.score_run(tmp, "fd")
            self.assertEqual(row["verdict"], "PASS")
            self.assertFalse(row["timing_comparison_eligible"])
            self.assertFalse(summarize.timing_run_eligible(row))
            r["realism"] = {"detail_timing_representative": None, "stations": [{"n": None}], "verdict_notes": []}
            (p / "result.json").write_text(json.dumps(r))
            self.assertFalse(score.timing_eligibility(tmp, r)["timing_comparison_eligible"])
            r["steps"] = []
            r["realism"] = {"detail_timing_representative": None, "stations": [], "verdict_notes": []}
            (p / "result.json").write_text(json.dumps(r))
            self.assertTrue(score.timing_eligibility(tmp, r)["timing_comparison_eligible"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
