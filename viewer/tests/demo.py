"""Demo mode: the viewer over synthetic harness fixtures with a harness run that is 'live'.

  python3 tests/demo.py [--port 8096]

Writes fixtures under a temp dir, fakes the Dev Lab API with one RUNNING job, keeps appending to progress.jsonl,
and serves on the given port. For looking at the UI (live charts, compare refusal, log diff) without a real fleet.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_fixtures as mf  # noqa: E402
from mcdevlab_viewer import collector, server  # noqa: E402
from test_collector import FakeApi, job, make_cfg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8096)
    args = ap.parse_args()
    root, data = tempfile.mkdtemp(), tempfile.mkdtemp()
    mf.build(root)
    with open(os.path.join(root, "candidate.json"), "w") as fh:
        json.dump({"sha256": mf.CAND_B["jar_sha256"], "source_commit": mf.CAND_B["commit"], "label": "bbbb2222"}, fh)
    live_dir = mf.write_running(root, "run-live")
    api = FakeApi()
    api.jobs["ctl"] = [job("job-live", "flyover", "RUNNING", time.time() - 20, rid="demo-flyover-bbbb2222-1")]
    cfg = make_cfg(root, poll_interval_s=60, live_interval_s=2)
    col = collector.Collector(cfg, data, api=api)

    def grow():
        i = 30
        while True:
            with open(os.path.join(live_dir, "progress.jsonl"), "a") as fh:
                fh.write(json.dumps({"t_ms": i * 1000, "step": 2, "step_type": "measure", "phase": "MEASURE", "fps": 50 + (i % 9), "frame_ms_p50": 12,
                                     "frame_ms_p95": 20 + (i % 11) * 0.7, "frame_ms_max": 40, "heap_used_mb": 2100 + i * 6, "heap_max_mb": 6144,
                                     "chunks_rendered": 200 + i, "measure_label": "spawn"}) + "\n")
            with open(os.path.join(live_dir, "client.log"), "a") as fh:
                fh.write("[00:00:%02d] [Render thread/%s] [examplemod/]: tick %d\n" % (i % 60, "WARN" if i % 5 == 0 else "INFO", i))
            i += 1
            time.sleep(1)
    threading.Thread(target=grow, daemon=True).start()
    col.run_forever()
    srv = server.make_server(data, args.port, "0.0.0.0", config=cfg)
    print("demo on :%d (data %s)" % (args.port, data), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
