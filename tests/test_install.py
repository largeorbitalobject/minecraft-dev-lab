import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import install
from scripts.package import build, ROOT


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcdev install 雪 🚀 ")
        self.base = Path(self.temp.name)
        self.source = self.base / "bundle"
        self.source.mkdir()
        (self.source / "minecraft-dev-lab.pyz").write_bytes(b"test archive bytes")
        (self.source / ".codex-plugin").mkdir()
        shutil.copyfile(ROOT / ".codex-plugin/plugin.json", self.source / ".codex-plugin/plugin.json")
        (self.source / "skills/test").mkdir(parents=True)
        (self.source / "skills/test/SKILL.md").write_text("Test fixture", encoding="utf-8")
        self.config = self.base / "project config 雪.json"
        self.config.write_text("{}", encoding="utf-8")
        self.dest = self.base / "installed plugin 雪"

    def tearDown(self):
        self.temp.cleanup()

    def test_config_and_marketplace_point_at_stable_installed_copy(self):
        result = install.install(self.dest, self.config, self.source)
        self.assertFalse(result["global_configuration_modified"])
        plugin = self.dest / "plugins/minecraft-dev-lab"
        command = json.loads((self.dest / "mcp.json").read_text())["mcpServers"]["minecraft-dev-lab"]
        self.assertEqual(Path(command["command"]), Path(sys.executable).resolve())
        self.assertEqual(command["args"], [str(plugin / "minecraft-dev-lab.pyz"), "serve", "--config", str(self.config.resolve())])
        self.assertEqual(json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["minecraft-dev-lab"], command)
        self.assertEqual(tomllib.loads((self.dest / "mcp.toml").read_text())["mcp_servers"]["minecraft-dev-lab"], command)
        market = json.loads((self.dest / ".agents/plugins/marketplace.json").read_text())
        entry = market["plugins"][0]
        self.assertEqual((self.dest / entry["source"]["path"]).resolve(), plugin.resolve())
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertTrue((plugin / "skills/test/SKILL.md").is_file())
        self.assertEqual(self.config.read_text(), "{}")
        self.assertEqual(list(self.base.glob(".mcdev-install-*")), [])

    def test_existing_destination_is_never_overwritten(self):
        self.dest.mkdir()
        (self.dest / "keep").write_text("important", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            install.install(self.dest, self.config, self.source)
        self.assertEqual((self.dest / "keep").read_text(), "important")

    def test_receipt_hashes_copied_bytes_if_source_changes_after_copy(self):
        original = install.shutil.copyfile
        artifact = self.source / "minecraft-dev-lab.pyz"
        copied_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        def mutate_after_copy(src, dst, *args, **kwargs):
            result = original(src, dst, *args, **kwargs)
            if Path(src) == artifact:
                artifact.write_bytes(b"a different source after copy")
            return result
        with patch.object(install.shutil, "copyfile", mutate_after_copy):
            receipt = install.install(self.dest, self.config, self.source)
        installed = self.dest / "plugins/minecraft-dev-lab/minecraft-dev-lab.pyz"
        self.assertEqual(receipt["pyz_sha256"], copied_hash)
        self.assertEqual(receipt["pyz_sha256"], hashlib.sha256(installed.read_bytes()).hexdigest())
        self.assertNotEqual(receipt["pyz_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())

    def test_destination_created_after_preflight_is_preserved(self):
        original = install.tempfile.mkdtemp
        def raced(*args, **kwargs):
            stage = original(*args, **kwargs)
            self.dest.mkdir()
            (self.dest / "other-owner").write_text("keep", encoding="utf-8")
            return stage
        with patch.object(install.tempfile, "mkdtemp", raced):
            with self.assertRaises(FileExistsError):
                install.install(self.dest, self.config, self.source)
        self.assertEqual((self.dest / "other-owner").read_text(), "keep")
        self.assertEqual(list(self.base.glob(".mcdev-install-*")), [])

    def test_failure_during_staging_leaves_no_destination_or_stage(self):
        with patch.object(install.shutil, "copytree", side_effect=OSError("injected copy failure")):
            with self.assertRaises(OSError):
                install.install(self.dest, self.config, self.source)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.base.glob(".mcdev-install-*")), [])

    def test_failure_during_commit_removes_only_claimed_destination(self):
        original = Path.rename
        calls = 0
        def failing(path, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected commit failure")
            return original(path, target)
        keep = self.base / "unrelated"
        keep.write_text("keep", encoding="utf-8")
        with patch.object(Path, "rename", failing):
            with self.assertRaises(OSError):
                install.install(self.dest, self.config, self.source)
        self.assertFalse(self.dest.exists())
        self.assertEqual(keep.read_text(), "keep")

    def test_invalid_config_and_missing_archive_do_not_install(self):
        with self.assertRaises(FileNotFoundError):
            install.install(self.dest, self.base / "missing.json", self.source)
        (self.source / "minecraft-dev-lab.pyz").unlink()
        with self.assertRaises(FileNotFoundError):
            install.install(self.dest, self.config, self.source)
        self.assertFalse(self.dest.exists())

    def test_symlink_destination_refused_when_supported(self):
        try:
            self.dest.symlink_to(self.base / "missing-target", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable on this platform/account")
        with self.assertRaises(FileExistsError):
            install.install(self.dest, self.config, self.source)


class PackageTests(unittest.TestCase):
    def test_zipapp_reproducible_and_release_contains_only_portable_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = build(Path(tmp) / "one")
            second = build(Path(tmp) / "two")
            self.assertEqual(first["artifacts"], second["artifacts"])
            archive = Path(first["output"]) / "minecraft-dev-lab-0.1.0.zip"
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                self.assertIn("minecraft-dev-lab-0.1.0/install.py", names)
                self.assertIn("minecraft-dev-lab-0.1.0/.codex-plugin/plugin.json", names)
                self.assertIn("minecraft-dev-lab-0.1.0/.mcp.json", names)
                self.assertTrue(all(not n.startswith("/") and ".." not in n.split("/") and "\\" not in n for n in names))
                self.assertFalse(any("__pycache__" in n or "/.git/" in n for n in names))
            for name, expected in first["artifacts"].items():
                self.assertEqual(hashlib.sha256((Path(first["output"]) / name).read_bytes()).hexdigest(), expected["sha256"])

    def test_actual_installed_zipapp_fresh_process_both_mcp_protocols(self):
        with tempfile.TemporaryDirectory(prefix="mcdev actual install 雪 🚀 ") as tmp:
            base = Path(tmp)
            build(base / "dist")
            with zipfile.ZipFile(base / "dist/minecraft-dev-lab-0.1.0.zip") as z:
                z.extractall(base / "extracted")  # This archive was produced just above.
            config = base / "fleet config.json"
            config.write_text(json.dumps({"schema": "mcdev.fleet.v1", "hosts": {}, "workspaces": {}}), encoding="utf-8")
            bundle = base / "extracted/minecraft-dev-lab-0.1.0"
            dest = base / "stable installation"
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            installed = subprocess.run([sys.executable, str(bundle / "install.py"), "--dest", str(dest), "--config", str(config)], cwd=base, env=env, capture_output=True, timeout=15)
            self.assertEqual(installed.returncode, 0, installed.stderr.decode(errors="replace"))
            self.assertFalse(json.loads(installed.stdout)["global_configuration_modified"])
            plugin = dest / "plugins/minecraft-dev-lab"
            manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
            self.assertTrue((plugin / manifest["skills"]).is_dir())
            command = json.loads((plugin / manifest["mcpServers"]).read_text())["mcpServers"]["minecraft-dev-lab"]
            modern_meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
            for version in ("2025-11-25", "2026-07-28"):
                with self.subTest(protocol=version):
                    messages = []
                    if version == "2025-11-25":
                        messages += [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "installed-test", "version": "1"}}}, {"jsonrpc": "2.0", "method": "notifications/initialized"}]
                    else:
                        messages += [{"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {"_meta": modern_meta}}]
                    for rid, method, params in [(1, "tools/list", {}), (2, "tools/call", {"name": "fleet_list", "arguments": {}})]:
                        if version == "2026-07-28":
                            params["_meta"] = modern_meta
                        messages.append({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
                    wire = b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages)
                    child = subprocess.run([command["command"]] + command["args"], input=wire, cwd=base, env=env, capture_output=True, timeout=15)
                    self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
                    replies = [json.loads(line) for line in child.stdout.splitlines()]
                    self.assertEqual(len(replies), 3, child.stdout)
                    by_id = {r["id"]: r for r in replies}
                    self.assertEqual(set(by_id), {0, 1, 2})
                    self.assertTrue(all(r["jsonrpc"] == "2.0" and "error" not in r for r in replies), replies)
                    self.assertIn("fleet_list", {t["name"] for t in by_id[1]["result"]["tools"]})
                    self.assertFalse(by_id[2]["result"].get("isError", False))
                    self.assertIsInstance(by_id[2]["result"]["structuredContent"], dict)
                    if version == "2026-07-28":
                        self.assertTrue(all(r["result"]["resultType"] == "complete" for r in replies))

    def test_installed_node_worker_exec_recovery_and_actual_artifact(self):
        with tempfile.TemporaryDirectory(prefix="mcdev installed job 雪 🚀 ") as tmp:
            base = Path(tmp)
            build(base / "dist")
            with zipfile.ZipFile(base / "dist/minecraft-dev-lab-0.1.0.zip") as z:
                z.extractall(base / "extracted")
            bundle = base / "extracted/minecraft-dev-lab-0.1.0"
            dest = base / "stable installation"
            app = dest / "plugins/minecraft-dev-lab/minecraft-dev-lab.pyz"
            instance = base / "instance"
            instance.mkdir()
            unrelated = base / "unrelated working directory"
            unrelated.mkdir()
            node_config = base / "node.json"
            recipe = (
                "from pathlib import Path; import zipfile; "
                "p=Path('launch.count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                "z=zipfile.ZipFile('result.jar','w'); "
                "z.writestr('fabric.mod.json','{\"id\":\"installed_smoke\",\"version\":\"1\"}'); z.close()"
            )
            node_config.write_text(json.dumps({
                "schema": "mcdev.node.v1", "id": "installed-node", "state_dir": "node-state",
                "instances": {"fixture": {"root": "instance", "enabled": True, "recipes": {
                    "artifact": {"kind": "test", "argv": [sys.executable, "-c", recipe], "timeout_seconds": 5,
                                 "output_limit_bytes": 4096, "artifacts": ["result.jar"]}}}}}), encoding="utf-8")
            fleet_config = base / "fleet.json"
            fleet_config.write_text(json.dumps({
                "schema": "mcdev.fleet.v1", "hosts": {"local": {"enabled": True, "node_id": "installed-node",
                    "argv": [sys.executable, str(app), "agent", "--config", str(node_config)]}},
                "workspaces": {"fixture": "instance"}}), encoding="utf-8")
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            setup = subprocess.run([sys.executable, str(bundle / "install.py"), "--dest", str(dest), "--config", str(fleet_config)], cwd=unrelated, env=env, capture_output=True, timeout=15)
            self.assertEqual(setup.returncode, 0, setup.stderr.decode(errors="replace"))

            def call(name, arguments):
                child = subprocess.run([sys.executable, str(app), "call", "--config", str(fleet_config), name, json.dumps(arguments)], cwd=unrelated, env=env, capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
                return json.loads(child.stdout)

            request = {"host": "local", "instance": "fixture", "recipe": "artifact", "request_id": "installed-recovery"}
            started = call("run_start", request)
            job = started["id"]
            terminal = {"PASSED", "FAILED", "CANCELLED", "TIMED_OUT", "BLOCKED", "OUTPUT_LIMIT"}
            state = started
            try:
                recovered = call("run_start", request)
                self.assertEqual(recovered["id"], job)
                deadline = time.monotonic() + 15
                while state["state"] not in terminal and time.monotonic() < deadline:
                    state = call("run_status", {"host": "local", "job": request["request_id"]})
                    if state["state"] not in terminal:
                        time.sleep(0.025)
                self.assertEqual(state["state"], "PASSED", state)
                self.assertEqual(state["id"], job)
                self.assertEqual((instance / "launch.count").read_text(), "1")
                self.assertTrue((base / "node-state" / job / "receipt.json").is_file())
                self.assertFalse((unrelated / "node-state").exists())
                artifact = instance / "result.jar"
                expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
                self.assertEqual(state["receipt"]["artifacts"][0]["sha256"], expected)
                self.assertEqual(state["receipt"]["game_acceptance"], "NOT_RUN")
                inspected = call("artifact_inspect", {"workspace": "fixture", "path": "result.jar"})
                self.assertEqual(inspected["status"], "PASS")
                self.assertEqual(inspected["sha256"], expected)
                self.assertIn("fabric", inspected["archive"]["loaders"])
                self.assertEqual(call("run_start", request)["id"], job)
                self.assertEqual((instance / "launch.count").read_text(), "1")
            finally:
                if state["state"] not in terminal:
                    call("run_cancel", {"host": "local", "job": job})
                    deadline = time.monotonic() + 10
                    while state["state"] not in terminal and time.monotonic() < deadline:
                        state = call("run_status", {"host": "local", "job": job})
                        if state["state"] not in terminal:
                            time.sleep(0.025)
                    self.assertIn(state["state"], terminal, "Installed test job did not reach a proven terminal state")


if __name__ == "__main__":
    unittest.main()
