"""Real temporary JAR I/O and adversarial receipt checks; no Minecraft or JVM."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from mcdevlab import evidence


def run(arm, pair, value=100, *, kind="cpu_component"):
    return {
        "schema": "mcdev.benchmark.v1", "run_id": f"{arm}-{pair}",
        "launch_id": f"jvm-{arm}-{pair}", "pair_id": f"pair-{pair}", "status": "PASS",
        "artifact_sha256": ("a" if arm == "base" else "b") * 64, "evidence_kind": kind,
        "context": {
            "scene": "saved-rejoin-pose-v1", "fixture": {"manifest_sha256": "c" * 64},
            "settings": {"width": 1280, "height": 720, "view_distance": 12},
            "hardware": {"host": "offline-fixture", "cpu": "test-model", "gpu": "none"},
            "toolchain": {"java": "21", "loader": "test", "minecraft": "1.21.1"},
            "timing_origin": "encoder-entry-to-return/thread-cpu", "protocol": "encoder-v1",
            "cache_state": {"jvm": "fresh", "filesystem": "warm", "warmups": 0},
        },
        "metrics": {"mesh_cpu_v1": {"value": value, "unit": "ms", "direction": "lower", "measurement": "direct"}},
    }


def arms(base=(100, 110, 90), candidate=(70, 80, 60)):
    return ([run("base", n, v) for n, v in enumerate(base)],
            [run("new", n, v) for n, v in enumerate(candidate)])


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "example.jar"

    def jar(self, entries, compression=zipfile.ZIP_STORED):
        with zipfile.ZipFile(self.path, "w", compression=compression) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return self.path

    def test_fabric_hash_metadata_and_no_extraction(self):
        self.jar({"fabric.mod.json": json.dumps({"id": "demo", "name": "Demo", "version": "1.0", "depends": {"minecraft": "1.21.1", "java": ">=21"}}),
                  "../../not-extracted.txt": "never written", "nested.jar": "not opened"})
        before = set(Path(self.directory.name).iterdir())
        result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["bytes"], self.path.stat().st_size)
        self.assertEqual(result["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result["archive"]["loaders"], ["fabric"])
        self.assertEqual(result["archive"]["metadata"]["fabric.mod.json"]["id"], "demo")
        self.assertEqual(before, set(Path(self.directory.name).iterdir()))

    def test_forge_neoforge_and_manifest_continuation(self):
        metadata = 'modLoader="javafml"\nloaderVersion="[4,)"\n[[mods]]\nmodId="demo"\nversion="1.2"\ndisplayName="Demo"\n'
        self.jar({"META-INF/mods.toml": metadata, "META-INF/neoforge.mods.toml": metadata,
                  "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\nBuild-Commit: abc\r\n def\r\n\r\nName: entry\r\nBuild-Commit: WRONG\r\n"})
        result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["archive"]["loaders"], ["forge", "neoforge"])
        self.assertEqual(result["archive"]["metadata"]["META-INF/MANIFEST.MF"]["Build-Commit"], "abcdef")

    def test_invalid_zip_still_hashes_exact_bytes(self):
        self.path.write_bytes(b"not a jar")
        result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["sha256"], hashlib.sha256(b"not a jar").hexdigest())

    def test_malformed_metadata_does_not_echo_content(self):
        self.jar({"fabric.mod.json": "private-player-token"})
        result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn("private-player-token", json.dumps(result))

    def test_duplicate_metadata_rejected(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w") as archive:
                archive.writestr("fabric.mod.json", '{"id":"one"}')
                archive.writestr("fabric.mod.json", '{"id":"two"}')
        self.assertEqual(evidence.inspect_artifact(self.path)["status"], "FAIL")

    def test_member_size_bound(self):
        self.jar({"fabric.mod.json": " " * (evidence.MAX_METADATA_BYTES + 1)})
        self.assertEqual(evidence.inspect_artifact(self.path)["status"], "FAIL")

    def test_expansion_bomb_rejected(self):
        self.jar({"fabric.mod.json": '"' + "x" * 100_000 + '"'}, zipfile.ZIP_DEFLATED)
        result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("ratio", result["archive"]["errors"][0])

    def test_directory_count_checked_before_zipfile_allocation(self):
        self.jar({"a": "b"})
        data = bytearray(self.path.read_bytes())
        end = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, end + 8, 30_000, 30_000)
        self.path.write_bytes(data)
        with patch.object(evidence.zipfile, "ZipFile", side_effect=AssertionError("Must reject before allocation")):
            self.assertEqual(evidence.inspect_artifact(self.path)["status"], "FAIL")

    def test_underreported_directory_entries_rejected_before_allocation(self):
        self.jar({"a": "b", "c": "d"})
        data = bytearray(self.path.read_bytes())
        end = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, end + 8, 1, 1)
        self.path.write_bytes(data)
        with patch.object(evidence.zipfile, "ZipFile", side_effect=AssertionError("Must reject before allocation")):
            self.assertEqual(evidence.inspect_artifact(self.path)["status"], "FAIL")

    def test_fake_eocd_in_comment_cannot_redirect_stdlib_parser(self):
        self.jar({"a": "b"})
        with zipfile.ZipFile(self.path, "a") as archive:
            # Deliberately mismatched comment length makes our terminal scan
            # skip this record, while an unchecked ZipFile would select it.
            archive.comment = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1,
                                          100 * 1024 * 1024, 0, 0) + b"trailing"
        with patch.object(evidence.zipfile, "ZipFile", side_effect=AssertionError("Must reject before allocation")):
            result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("Ambiguous", result["archive"]["errors"][0])

    def test_hidden_zip64_locator_cannot_redirect_stdlib_parser(self):
        # A central-entry comment can place a ZIP64 locator exactly 20 bytes
        # before EOCD without any ZIP64 sentinel in the ordinary end record.
        entry = zipfile.ZipInfo("a")
        entry.comment = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr(entry, "b")
        with patch.object(evidence.zipfile, "ZipFile", side_effect=AssertionError("Must reject before allocation")):
            result = evidence.inspect_artifact(self.path)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("ZIP64 locator", result["archive"]["errors"][0])

    def test_oversized_file_rejected_before_hashing(self):
        self.path.write_bytes(b"12345678")
        with patch.object(evidence, "MAX_ARTIFACT_BYTES", 4):
            with self.assertRaises(ValueError):
                evidence.inspect_artifact(self.path)

    def test_symlink_rejected(self):
        real = self.jar({"a": "b"})
        link = real.with_name("link.jar")
        link.symlink_to(real)
        with self.assertRaises(OSError):
            evidence.inspect_artifact(link)

    def test_changing_file_rejected_after_inspection(self):
        self.jar({"a": "b"})
        original = evidence._inspect_zip
        def mutate(stream, size):
            result = original(stream, size)
            with self.path.open("ab") as output:
                output.write(b"changed")
            return result
        with patch.object(evidence, "_inspect_zip", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed"):
                evidence.inspect_artifact(self.path)

    def test_crc_failure_rejected(self):
        self.jar({"fabric.mod.json": '{"id":"demo"}'})
        data = self.path.read_bytes().replace(b'{"id":"demo"}', b'{"id":"evil"}')
        self.path.write_bytes(data)
        self.assertEqual(evidence.inspect_artifact(self.path)["status"], "FAIL")


class ComparisonTests(unittest.TestCase):
    def test_matched_repeated_pair_thresholds(self):
        baseline, candidate = arms()
        original = copy.deepcopy((baseline, candidate))
        candidate.reverse()  # Pair identity, not list position, defines matching.
        result = evidence.compare_runs(baseline, candidate, {"min_pairs": 3, "metrics": {
            "mesh_cpu_v1": {"min_relative_improvement": .2, "min_absolute_improvement": 25, "max_regression": 0}}})
        self.assertEqual(result["status"], "PASS")
        metric = result["metrics"]["mesh_cpu_v1"]
        self.assertEqual(metric["median_absolute_improvement"], 30)
        self.assertAlmostEqual(metric["median_relative_improvement"], .3)
        self.assertEqual(metric["improvement_mad"], 0)
        self.assertEqual(baseline, original[0])
        self.assertEqual(list(reversed(candidate)), original[1])

    def test_no_threshold_is_not_efficacy_pass(self):
        result = evidence.compare_runs(*arms())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["threshold_status"], "NOT_RUN")
        self.assertIn("No efficacy", result["claim"])

    def test_early_cutoff_single_pair_cannot_become_median_pass(self):
        result = evidence.compare_runs(*arms((10,), (13,)))
        self.assertEqual(result["status"], "NOT_RUN")
        self.assertFalse(result["comparison_performed"])
        self.assertEqual(result["metrics"], {})

    def test_all_context_dimensions_must_match(self):
        for key in evidence.CONTEXT_KEYS:
            with self.subTest(key=key):
                baseline, candidate = arms()
                candidate[0]["context"][key] = "different"
                result = evidence.compare_runs(baseline, candidate)
                self.assertEqual(result["status"], "NOT_RUN")
                self.assertEqual(result["metrics"], {})

    def test_metric_shape_and_scope_mismatches_refused(self):
        cases = (("unit", "ns"), ("direction", "higher"), ("measurement", "proxy"))
        for field, value in cases:
            with self.subTest(field=field):
                baseline, candidate = arms()
                candidate[0]["metrics"]["mesh_cpu_v1"][field] = value
                self.assertFalse(evidence.compare_runs(baseline, candidate)["comparison_performed"])
        baseline, candidate = arms()
        candidate[0]["evidence_kind"] = "visible_pixels"
        self.assertEqual(evidence.compare_runs(baseline, candidate)["metrics"], {})

    def test_both_proxy_arms_still_refused(self):
        baseline, candidate = arms()
        for item in baseline + candidate:
            item["metrics"]["mesh_cpu_v1"]["measurement"] = "proxy"
        self.assertFalse(evidence.compare_runs(baseline, candidate)["comparison_performed"])

    def test_reused_launch_or_run_id_refused(self):
        for key in ("launch_id", "run_id"):
            baseline, candidate = arms()
            candidate[0][key] = baseline[0][key]
            self.assertFalse(evidence.compare_runs(baseline, candidate)["comparison_performed"])

    def test_duplicate_missing_or_mixed_pairs_refused(self):
        for mutation in (lambda b, c: c[0].update(pair_id=c[1]["pair_id"]),
                         lambda b, c: c[0].update(pair_id="unpaired"),
                         lambda b, c: c[0].update(artifact_sha256="c" * 64),
                         lambda b, c: b[0].update(status="FAIL"),
                         lambda b, c: c[0].update(status="NOT_RUN")):
            baseline, candidate = arms()
            mutation(baseline, candidate)
            self.assertFalse(evidence.compare_runs(baseline, candidate)["comparison_performed"])

    def test_nan_infinite_boolean_huge_samples_refused(self):
        for invalid in (float("nan"), float("inf"), -float("inf"), True, 10 ** 1000):
            baseline, candidate = arms()
            candidate[0]["metrics"]["mesh_cpu_v1"]["value"] = invalid
            with self.assertRaises(ValueError):
                evidence.compare_runs(baseline, candidate)

    def test_higher_direction_and_units(self):
        baseline, candidate = arms((100, 100, 100), (130, 120, 140))
        for item in baseline + candidate:
            item["metrics"]["mesh_cpu_v1"].update(unit="chunks/s", direction="higher")
        result = evidence.compare_runs(baseline, candidate)
        self.assertEqual(result["metrics"]["mesh_cpu_v1"]["median_absolute_improvement"], 30)

    def test_zero_baseline_no_ratio_invention(self):
        baseline, candidate = arms((0, 0, 0), (0, 0, 0))
        result = evidence.compare_runs(baseline, candidate, {"metrics": {"mesh_cpu_v1": {"max_regression": .1}}})
        self.assertIsNone(result["metrics"]["mesh_cpu_v1"]["median_relative_improvement"])
        self.assertEqual(result["status"], "FAIL")

    def test_relative_and_absolute_must_both_pass(self):
        baseline, candidate = arms((90, 90, 90), (87, 87, 87))
        result = evidence.compare_runs(baseline, candidate, {"metrics": {"mesh_cpu_v1": {
            "min_relative_improvement": .2, "min_absolute_improvement": 50}}})
        self.assertEqual(result["status"], "FAIL")

    def test_improved_proxy_cannot_override_cpu_failure(self):
        baseline, candidate = arms((10, 10, 10), (13, 13, 13))
        for item in baseline + candidate:
            item["metrics"]["service_calls"] = {"value": 100 if item in baseline else 15,
                                                "unit": "calls", "direction": "lower", "measurement": "direct"}
        result = evidence.compare_runs(baseline, candidate, {"metrics": {
            "mesh_cpu_v1": {"max_regression": .2}, "service_calls": {"min_relative_improvement": .5}}})
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["metrics"]["service_calls"]["threshold_status"], "PASS")
        self.assertEqual(result["evidence_kind"], "cpu_component")

    def test_threshold_typos_and_single_repetition_rejected(self):
        for thresholds in ({"minimum_pairs": 3}, {"min_pairs": 1}, {"min_pairs": True},
                           {"metrics": {"missing": {"max_regression": .1}}},
                           {"metrics": {"mesh_cpu_v1": {"max_regresion": .1}}},
                           {"metrics": {"mesh_cpu_v1": {"max_regression": float("nan")}}}):
            with self.subTest(thresholds=thresholds), self.assertRaises(ValueError):
                evidence.compare_runs(*arms(), thresholds)

    def test_even_finite_large_medians_do_not_overflow(self):
        baseline, candidate = arms((1e308, 1e308), (9e307, 9e307))
        result = evidence.compare_runs(baseline, candidate, {"min_pairs": 2})
        self.assertEqual(result["metrics"]["mesh_cpu_v1"]["baseline_median"], 1e308)
        json.dumps(result, allow_nan=False)

    def test_arithmetic_overflow_refused(self):
        with self.assertRaises(ValueError):
            evidence.compare_runs(*arms((1e308,) * 3, (-1e308,) * 3))

    def test_no_runs_is_not_run(self):
        self.assertEqual(evidence.compare_runs([], [])["status"], "NOT_RUN")

    def test_unknown_matching_context_or_metric_fields_rejected(self):
        baseline, candidate = arms()
        candidate[0]["context"]["forgotten_pose"] = "other"
        with self.assertRaises(ValueError):
            evidence.compare_runs(baseline, candidate)
        baseline, candidate = arms()
        candidate[0]["metrics"]["mesh_cpu_v1"]["samples"] = [1, 2, 3]
        with self.assertRaises(ValueError):
            evidence.compare_runs(baseline, candidate)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.artifact = {"schema": "mcdev.artifact.v1", "status": "PASS", "sha256": "a" * 64,
                         "bytes": 123, "archive": {"status": "PASS"}}

    def check(self, name="build:jar", status="PASS", digest="a" * 64):
        return {"name": name, "evidence_kind": name.split(":")[0], "status": status, "artifact_sha256": digest}

    def test_local_ready_never_published(self):
        result = evidence.prepare_release(self.artifact, [self.check()], ["build:jar"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["readiness"], "READY")
        self.assertFalse(result["published"])
        self.assertEqual(result["publication"], "NOT_REQUESTED")

    def test_missing_native_is_not_run_despite_source_build_pass(self):
        result = evidence.prepare_release(self.artifact, [self.check(), self.check("source:review")],
                                          ["build:jar", "source:review", "visible_pixels:frozen453"])
        self.assertEqual(result["status"], "NOT_RUN")
        self.assertEqual(result["required_checks"][-1]["status"], "NOT_RUN")
        self.assertEqual(result["readiness"], "NOT_READY")

    def test_exact_artifact_hash_not_head_name_required(self):
        result = evidence.prepare_release(self.artifact, [self.check(digest="b" * 64)], ["build:jar"])
        self.assertEqual(result["status"], "FAIL")

    def test_fail_and_not_run_distinct(self):
        for status in ("FAIL", "NOT_RUN"):
            result = evidence.prepare_release(self.artifact, [self.check(status=status)], ["build:jar"])
            self.assertEqual(result["status"], status)

    def test_failed_archive_cannot_release(self):
        self.artifact["archive"]["status"] = "FAIL"
        self.assertEqual(evidence.prepare_release(self.artifact, [self.check()], ["build:jar"])["status"], "FAIL")

    def test_cannot_relabel_source_as_native(self):
        check = self.check("visible_pixels:frozen453")
        check["evidence_kind"] = "source"
        with self.assertRaises(ValueError):
            evidence.prepare_release(self.artifact, [check], [check["name"]])

    def test_ambiguous_or_empty_requirements_rejected(self):
        for required in ([], ["build"], ["build:jar", "build:jar"], ["other:x"]):
            with self.assertRaises(ValueError):
                evidence.prepare_release(self.artifact, [], required)

    def test_duplicate_receipts_require_explicit_supersession(self):
        with self.assertRaises(ValueError):
            evidence.prepare_release(self.artifact, [self.check(), self.check(status="FAIL")], ["build:jar"])

    def test_no_mutation_or_external_action(self):
        checks = [self.check()]
        original = copy.deepcopy((self.artifact, checks))
        with patch.object(evidence.os, "open", side_effect=AssertionError("Pure operation")):
            evidence.prepare_release(self.artifact, checks, ["build:jar"])
        self.assertEqual((self.artifact, checks), original)


class TriageTests(unittest.TestCase):
    def test_patterns_without_private_excerpts(self):
        text = 'Player SecretUser token=secret123 /home/alice/private\njava.lang.OutOfMemoryError: Java heap space\nNoSuchMethodError at secret.private.Mod'
        result = evidence.triage_log(text)
        self.assertEqual({item["category"] for item in result["findings"]}, {"memory_exhaustion", "binary_api_mismatch"})
        serialized = json.dumps(result)
        for secret in ("SecretUser", "secret123", "/home/alice", "secret.private.Mod"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result["status"], "HEURISTIC_ONLY")

    def test_bounded_and_no_false_health_claim(self):
        with patch.object(evidence, "MAX_LOG_CHARS", 4):
            result = evidence.triage_log("safe OutOfMemoryError")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["findings"], [])
        self.assertNotEqual(result["status"], "PASS")


class RedactionTests(unittest.TestCase):
    def test_cli_json_properties_and_query_tokens(self):
        samples = (
            "launch --accessToken secret-value --version 1.21.1",
            "launch --access_token=secret-value --version 1.21.1",
            "launch ['--accessToken', 'secret-value', '--version', '1.21.1']",
            "launch [--accessToken, secret-value, --version, 1.21.1]",
            'config={"access_token": "secret-value", "other": "kept"}',
            "config={'token': 'secret-value', 'other': 'kept'}",
            "token = secret-value; other=kept",
            "https://example.invalid/?token=secret-value&other=kept",
            "TOKEN: secret-value, other=kept",
            "--token 'secret-value' --other kept",
        )
        for original in samples:
            with self.subTest(original=original):
                result = evidence.redact_log(original)
                self.assertNotIn("secret-value", result)
                self.assertIn("[REDACTED]", result)
                self.assertTrue("kept" in result or "1.21.1" in result)

    def test_authorization_bearer_forms(self):
        for original in (
            "Authorization: Bearer secret-value",
            "Authorization Bearer secret-value",
            "authorization=Bearer secret-value",
            'headers={"Authorization": "Bearer secret-value", "Accept": "json"}',
            "Authorization: Basic secret-value",
        ):
            with self.subTest(original=original):
                result = evidence.redact_log(original)
                self.assertNotIn("secret-value", result)
                self.assertIn("[REDACTED]", result)

    def test_quoted_escaped_and_incomplete_values(self):
        for original in ('access_token="private \\"quoted\\" value"', "token='private with spaces'", 'token="private unterminated', "token='private unterminated"):
            with self.subTest(original=original):
                result = evidence.redact_log(original)
                self.assertNotIn("private", result)
                self.assertNotIn("unterminated", result)

    def test_compiler_errors_source_lines_and_stacktraces_preserved(self):
        diagnostic = (
            "src/main/java/Example.java:42: error: cannot find symbol\n"
            "    MapMetrics.endWork(DEFERRED);\n"
            "                      ^\n"
            "error: <identifier> expected\n"
            "Unexpected token near type parameter\n"
            "java.lang.NoSuchMethodError: demo.Example.work()\n"
            "\tat demo.Example.run(Example.java:42)\n"
            "Caused by: java.lang.IllegalStateException: failed\n"
        )
        self.assertEqual(evidence.redact_log(diagnostic), diagnostic)

    def test_chat_lines_remove_message_and_identity(self):
        original = (
            "[12:00:00] [Render thread/INFO]: [CHAT] <PrivatePlayer> private message\r\n"
            "[12:00:01] [Server thread/INFO]: <OtherPlayer> other private message\n"
            "<ThirdPlayer> another secret\n"
            "java.lang.NoSuchMethodError: demo.Example.work()\n"
        )
        result = evidence.redact_log(original)
        for fragment in ("PrivatePlayer", "OtherPlayer", "ThirdPlayer", "private message", "another secret"):
            self.assertNotIn(fragment, result)
        self.assertEqual(result.count("[REDACTED PLAYER CHAT]"), 3)
        self.assertIn("[REDACTED PLAYER CHAT]\r\n", result)
        self.assertIn("NoSuchMethodError", result)

    def test_ansi_does_not_hide_known_field_or_chat_marker(self):
        original = "access\x1b[31mToken\x1b[0m=secret-value\n[CH\x1b[0mAT] <SecretPlayer> hi\n"
        result = evidence.redact_log(original)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("SecretPlayer", result)
        self.assertNotIn("\x1b", result)

    def test_bound_truncation_empty_input_and_invalid_type(self):
        with patch.object(evidence, "MAX_REDACT_CHARS", 16):
            result = evidence.redact_log('token="private-value-after-bound')
        self.assertNotIn("private", result)
        self.assertIn("TRUNCATED", result)
        self.assertEqual(evidence.redact_log(""), "")
        with self.assertRaises(ValueError):
            evidence.redact_log(None)

    def test_two_fields_in_one_line_and_heuristics_still_separate(self):
        original = "token=first-secret access_token=second-secret\nOutOfMemoryError\n"
        result = evidence.redact_log(original)
        self.assertNotIn("first-secret", result)
        self.assertNotIn("second-secret", result)
        self.assertEqual(result.count("[REDACTED]"), 2)
        self.assertEqual(evidence.triage_log(original)["status"], "HEURISTIC_ONLY")
        self.assertNotIn("excerpt", evidence.triage_log(original))


if __name__ == "__main__":
    unittest.main()
