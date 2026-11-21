from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from mcdevlab.node import Node, database, worker
from mcdevlab.common import atomic_json
from mcdevlab.api import LabAPI


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcdev node 雪 ")
        self.root = Path(self.temp.name)
        self.instance = self.root / "instance"; self.instance.mkdir()
        self.path = self.root / "node.json"
        self.config = {"schema": "mcdev.node.v1", "id": "test-node", "state_dir": "state", "instances": {
            "test": {"root": "instance", "enabled": True, "recipes": {
                "fast": {"kind": "test", "argv": [sys.executable, "-c", "from pathlib import Path; Path('result.txt').write_text('actual output'); print('done')"], "artifacts": ["result.txt"], "timeout_seconds": 3},
                "slow": {"kind": "test", "argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_seconds": 4},
                "timeout": {"kind": "test", "argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout_seconds": 1},
                "flood": {"kind": "test", "argv": [sys.executable, "-c", "print('x'*100000)"], "output_limit_bytes": 4096, "timeout_seconds": 3}
            }}}}
        atomic_json(self.path, self.config); self.node = Node(self.path); self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            try:
                self.node.dispatch({"op": "cancel", "job": job})
                self.wait(job)
            except Exception:
                pass
        self.temp.cleanup()

    def start(self, recipe="fast", req=None):
        result = self.node.dispatch({"op": "start", "instance": "test", "recipe": recipe, "request_id": req or str(uuid.uuid4())})
        self.jobs.append(result["id"]); return result

    def wait(self, job):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            value = self.node.dispatch({"op": "status", "job": job})
            if value["state"] not in {"QUEUED", "RUNNING"}:
                return value
            time.sleep(0.03)
        self.fail("owned test supervisor did not terminate")

    def test_actual_process_receipt_and_idempotency(self):
        req = str(uuid.uuid4()); started = self.start(req=req); done = self.wait(started["id"])
        self.assertEqual(done["state"], "PASSED")
        self.assertEqual(done["receipt"]["game_acceptance"], "NOT_RUN")
        self.assertEqual(done["receipt"]["artifacts"][0]["bytes"], 13)
        self.assertEqual(self.start(req=req)["id"], started["id"])
        self.assertEqual(self.node.dispatch({"op": "status", "job": req})["id"], started["id"])
        self.assertEqual(len(list((self.root / "state").glob("job-*"))), 1)

    def test_resource_conflict_and_cancel(self):
        started = self.start("slow")
        with self.assertRaisesRegex(RuntimeError, "leased"):
            self.start("fast")
        self.node.dispatch({"op": "cancel", "job": started["id"]})
        self.assertEqual(self.wait(started["id"])["state"], "CANCELLED")
        self.assertEqual(self.wait(self.start()["id"])["state"], "PASSED")

    def test_deadline_and_output_limit(self):
        self.assertEqual(self.wait(self.start("timeout")["id"])["state"], "TIMED_OUT")
        flood = self.wait(self.start("flood")["id"])
        self.assertEqual(flood["state"], "OUTPUT_LIMIT")
        self.assertLessEqual(flood["receipt"]["log_bytes"], 4096)

    def test_no_duplicate_request_identity_or_job_prefix(self):
        req = str(uuid.uuid4()); job = self.start(req=req)
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.start(req=job["id"])
        with self.assertRaisesRegex(ValueError, "another operation"):
            self.start("slow", req=req)
        self.wait(job["id"])

    def test_spawn_failure_is_durable_not_retried(self):
        req = str(uuid.uuid4())
        with patch("mcdevlab.node.subprocess.Popen", side_effect=OSError("injected")):
            with self.assertRaises(RuntimeError):
                self.start(req=req)
        result = self.start(req=req)
        self.assertEqual(result["state"], "FAILED")

    def test_guard_refusal_before_recipe_and_missing_client_guard(self):
        guard = self.root / "guard.py"; guard.write_text("import json; print(json.dumps({'allowed':False}))")
        self.config["guards"] = [[sys.executable, str(guard)]]; atomic_json(self.path, self.config); self.node = Node(self.path)
        result = self.wait(self.start()["id"])
        self.assertEqual(result["state"], "BLOCKED")
        self.assertFalse((self.instance / "result.txt").exists())
        self.config.pop("guards"); self.config["instances"]["test"]["recipes"]["fast"]["kind"] = "client"
        atomic_json(self.path, self.config); self.node = Node(self.path)
        with self.assertRaisesRegex(ValueError, "guard"):
            self.start()

    def test_running_guard_stops_owned_process(self):
        guard = self.root / "guard.py"
        permit = self.root / "permit"; permit.write_text("yes")
        guard.write_text("import json; from pathlib import Path; print(json.dumps({'allowed':Path(" + repr(str(permit)) + ").exists()}))")
        self.config["guards"] = [[sys.executable, str(guard)]]
        atomic_json(self.path, self.config); self.node = Node(self.path)
        started = self.start("slow")
        end = time.monotonic() + 3
        while not (self.node.state / started["id"] / "gate.json").exists() and time.monotonic() < end:
            time.sleep(.01)
        permit.unlink()
        self.assertEqual(self.wait(started["id"])["state"], "BLOCKED")

    def test_cancel_owns_descendants(self):
        marker = self.instance / "escaped.txt"
        child = "import time; from pathlib import Path; time.sleep(1.5); Path(" + repr(str(marker)) + ").write_text('escaped'); time.sleep(1)"
        parent = "import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); Path('ready').write_text('yes'); time.sleep(30)"
        self.config["instances"]["test"]["recipes"]["slow"]["argv"] = [sys.executable, "-c", parent]
        atomic_json(self.path, self.config); self.node = Node(self.path)
        started = self.start("slow"); end = time.monotonic() + 3
        while not (self.instance / "ready").exists() and time.monotonic() < end:
            time.sleep(.01)
        self.assertTrue((self.instance / "ready").exists())
        self.node.dispatch({"op": "cancel", "job": started["id"]})
        self.assertEqual(self.wait(started["id"])["state"], "CANCELLED")
        time.sleep(1.6)
        self.assertFalse(marker.exists(), "owned descendant must not survive cancellation")

    def test_log_capture_failure_cannot_pass(self):
        # Make the output destination a directory before the real supervisor opens
        # it. Successful recipe exit cannot turn missing evidence into PASSED.
        with patch("mcdevlab.node.subprocess.Popen"):
            started = self.start()
        (self.node.state / started["id"] / "output.log").mkdir()
        worker(self.node.state, started["id"])
        result = self.node.dispatch({"op": "status", "job": started["id"]})
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["receipt"]["error"]["type"], "LogCaptureFailed")

    def test_unknown_cleanup_keeps_resource(self):
        # Fault-inject the owned supervisor boundary, without launching a process.
        with patch("mcdevlab.node.subprocess.Popen"):
            started = self.start()
        class FakeProcess:
            returncode = 0
            def poll(self): return 0
        class FakeOwner:
            process = FakeProcess()
            def __init__(self, *a, **kw): pass
            def close(self): raise OSError("cleanup uncertain")
        with patch("mcdevlab.node.OwnedProcess", FakeOwner):
            worker(self.node.state, started["id"])
        self.assertEqual(self.node.dispatch({"op": "status", "job": started["id"]})["state"], "UNKNOWN")
        with self.assertRaisesRegex(RuntimeError, "leased"):
            self.start()
        self.jobs.remove(started["id"])

    def test_action_intent_recovery_and_conflict(self):
        self.config["instances"]["test"]["bridge"] = {"resource": "host"}; atomic_json(self.path, self.config); self.node = Node(self.path)
        action = {"request_id": str(uuid.uuid4()), "operation": "open_map", "arguments": {}}
        request = {"op": "game_action", "instance": "test", "request": action}
        reply = {"status": "unknown", "outcome": "unknown", "request_id": action["request_id"]}
        with patch("mcdevlab.bridges.invoke_bridge", return_value=reply) as invoke:
            self.assertEqual(self.node.dispatch(request), reply)
            self.assertEqual(self.node.dispatch(request), reply)
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(self.node.dispatch({"op": "game_action_result", "request_id": action["request_id"]}), reply)
        with self.assertRaisesRegex(RuntimeError, "reserved"):
            self.start()
        changed = {**request, "request": {**action, "arguments": {"changed": True}}}
        with self.assertRaisesRegex(ValueError, "different"):
            self.node.dispatch(changed)

    def test_readonly_disabled_and_path_safety(self):
        self.assertEqual(self.node.state, self.root / "state")
        self.assertEqual(self.node.dispatch({"op": "probe"})["id"], "test-node")
        self.config["instances"]["test"]["enabled"] = False; atomic_json(self.path, self.config); self.node = Node(self.path)
        self.assertFalse(self.node.dispatch({"op": "inspect", "instance": "test"})["enabled"])
        with self.assertRaises(RuntimeError): self.start()
        fleet = self.root / "fleet.json"
        atomic_json(fleet, {"schema": "mcdev.fleet.v1", "hosts": {}, "workspaces": {"test": "instance"}})
        api = LabAPI(fleet)
        with self.assertRaises(ValueError): api.call("artifact_inspect", {"workspace": "test", "path": "../node.json"})
        with self.assertRaises(ValueError): api.call("fleet_list", {"shell": "bad"})

    def test_real_node_transport_identity(self):
        fleet = self.root / "fleet.json"
        atomic_json(fleet, {"schema": "mcdev.fleet.v1", "hosts": {"local": {"enabled": True, "node_id": "test-node", "argv": [sys.executable, "-m", "mcdevlab", "agent", "--config", str(self.path)]}}, "workspaces": {}})
        api = LabAPI(fleet)
        self.assertEqual(api.call("host_probe", {"host": "local"})["id"], "test-node")
        api.config["hosts"]["local"]["node_id"] = "wrong"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            api.call("host_probe", {"host": "local"})


if __name__ == "__main__": unittest.main()
