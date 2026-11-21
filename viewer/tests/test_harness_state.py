import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from mcdevlab_viewer import collector, harness_state, config
from mcdevlab_viewer.store import Store


class ControllerState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.name = "20261102T040000Z-atlas-release-smoke-fd-arm-a"
        self.obj = {"host": "fd", "run": self.name, "status": "RUNNING", "started_utc": "2026-11-02T04:00:01Z"}
        self.path = self.root / self.name / "controller-status.json"
        self.path.parent.mkdir()
        self.write()

    def write(self, **kw):
        self.obj.update(kw)
        self.path.write_text(json.dumps(self.obj))
        os.utime(self.path, (self.now, self.now))

    def observe(self):
        return harness_state.observe(str(self.root), "fd", self.now)

    def test_running_recipe_and_started_from_actual_wire(self):
        o = self.observe()
        self.assertEqual(o["state"], "running")
        self.assertEqual(o["job"]["recipe"], "atlas-release-smoke-fd")
        self.assertEqual(o["job"]["started"], 1793592001)

    def test_stale_or_future_mirror_is_not_running(self):
        for offset in (-91, 6):
            os.utime(self.path, (self.now + offset, self.now + offset))
            self.assertEqual(self.observe()["state"], "stale")

    def test_exit_confirmation_required_even_for_terminal(self):
        for status in ("PASS", "FAIL"):
            self.write(status=status, exit_confirmed=False)
            self.assertEqual(self.observe()["state"], "stale")
            self.write(exit_confirmed=True)
            os.utime(self.path, (self.now-500, self.now-500))
            self.assertEqual(self.observe()["state"], "idle")

    def test_cleanup_never_claims_idle(self):
        self.write(status="CLEANUP_PENDING")
        self.assertEqual(self.observe()["state"], "stale")

    def test_paused_refused_are_visible(self):
        for status, state in (("PAUSED", "paused"), ("REFUSED", "blocked")):
            self.write(status=status, reason="GPU_BUSY")
            self.assertEqual(self.observe()["state"], state)
            self.assertEqual(self.observe()["reason"], "GPU_BUSY")

    def test_bad_or_partial_newest_receipt_cannot_use_old_running(self):
        p = self.root / "20261102T050000Z-atlas-release-smoke-fd-new" / "controller-status.json"
        p.parent.mkdir()
        p.write_text('{"status":')
        self.assertEqual(self.observe()["state"], "stale")

    def test_wrong_host_and_run_refuse_running(self):
        self.write(host="ct224")
        self.assertEqual(self.observe()["state"], "stale")
        self.write(host="fd", run="other")
        self.assertEqual(self.observe()["state"], "stale")

    def test_symlink_and_oversized_receipts_do_not_get_read(self):
        self.path.unlink()
        self.path.symlink_to('/dev/zero')
        self.assertEqual(self.observe()["state"], "stale")
        self.path.unlink()
        self.path.write_bytes(b' ' * (harness_state.MAX_BYTES+1))
        self.assertEqual(self.observe()["state"], "stale")

    def poller(self):
        cfg = config.load_config(None)
        h = {"transport": {"type": "local"}, "sources": [],
             "controller_archive": str(self.root), "controller_host": "fd"}
        return collector.HostPoller("amd", h, cfg, Store(str(self.root / "store")),
                                    None, threading.Lock(), collector.NullApi())

    def test_actual_full_poll_reports_running_despite_old_api_down(self):
        hp = self.poller()
        st = hp.full_poll()
        self.assertEqual(st["state"], "running")
        self.assertEqual(st["job"]["recipe"], "atlas-release-smoke-fd")
        self.assertEqual(st["job"]["source"], "large-harness-controller")

    def test_actual_live_poll_end_and_receipt_loss_without_host_call(self):
        hp = self.poller()
        hp.full_poll()
        with patch.object(hp, '_call', side_effect=AssertionError('no host call')):
            self.assertTrue(hp.live_poll()["active"])
            self.write(status="PASS", exit_confirmed=True)
            self.assertFalse(hp.live_poll()["active"])
            self.assertEqual(hp.state["state"], "idle")
        self.write(status="RUNNING", exit_confirmed=False)
        hp.full_poll()
        self.path.unlink()
        self.assertFalse(hp.live_poll()["active"])
        self.assertEqual(hp.state["state"], "stale")

    def test_old_api_running_keeps_priority_and_pause_is_preserved(self):
        hp = self.poller()
        with patch.object(hp, '_jobs', return_value=([{"id":"old", "state":"RUNNING", "recipe":"old"}], {})):
            self.assertEqual(hp.full_poll()["job"]["id"], "old")
        pause = self.root / "PAUSE"
        pause.touch()
        hp.h["pause_files"] = [str(pause)]
        st = hp.full_poll()
        self.assertTrue(st["paused_while_running"])
        self.write(status="PASS", exit_confirmed=True)
        self.assertFalse(hp.live_poll()["active"])
        self.assertEqual(hp.state["state"], "paused")

    def test_ct224_mapping_uses_its_own_archive(self):
        cfg = config.load_config(None)
        self.assertEqual(cfg["hosts"]["amd"]["controller_host"], "fd")
        self.assertEqual(cfg["hosts"]["proxmox-test"]["controller_host"], "ct224")
        name = self.name.replace('-fd-', '-ct224-')
        dest = self.root / name / "controller-status.json"
        dest.parent.mkdir()
        dest.write_text(json.dumps(dict(self.obj, host="ct224", run=name)))
        o = harness_state.observe(str(self.root), "ct224")
        self.assertEqual(o["state"], "running")
        self.assertEqual(o["job"]["recipe"], "atlas-release-smoke-ct224")


if __name__ == '__main__':
    unittest.main()
