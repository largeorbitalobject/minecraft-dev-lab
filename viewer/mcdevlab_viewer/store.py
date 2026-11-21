"""On-disk data layout (written by the collector, read by the server).

data/
  runs/<run id>/run.json           normalised run (no large blobs)
  runs/<run id>/mirror.json        what was copied from the node (remote fingerprint -> local files, derived log facts)
  runs/<run id>/log.txt.gz         the run log as plain text lines
  runs/<run id>/shots/<n>.png      full-size screenshots (while under the per-run cap)
  runs/<run id>/thumbs/<n>.jpg     thumbnails
  runs/<run id>/files/<name>       small evidence files (result.txt, progress.txt, summaries)
  hosts/<host>.json                per-host collector state (candidate, paused, errors, jobs)
  live/<host>.json                 live progress while a job runs
  jobs/<host>/<job>.json           cached job log text for failed jobs
  cache/<host>.json                inline-content cache keyed by remote fingerprint
  candidates.json                  jar sha256 -> label/commit
  state.json                       collector status for /api/status
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading

PRIVATE_PREFIX = "_"


class Store:
    def __init__(self, data_dir):
        self.root = os.path.abspath(data_dir)
        self._lock = threading.Lock()
        for sub in ("runs", "hosts", "live", "jobs", "cache"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

    # paths --------------------------------------------------------------------------------------
    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def run_dir(self, run_id):
        return self.p("runs", run_id)

    # atomic json --------------------------------------------------------------------------------
    def write_json(self, path, obj, indent=None):
        """Atomic write; returns False (and does nothing) when the content is unchanged."""
        data = json.dumps(obj, sort_keys=True, indent=indent, separators=(",", ":") if indent is None else None,
                          ensure_ascii=False).encode("utf-8")
        try:
            with open(path, "rb") as fh:
                if fh.read() == data:
                    return False
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True

    def write_bytes(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def read_json(self, path, default=None):
        try:
            with open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError):
            return default

    # runs ---------------------------------------------------------------------------------------
    def run_ids(self):
        try:
            return sorted(d for d in os.listdir(self.p("runs")) if os.path.isfile(self.p("runs", d, "run.json")))
        except OSError:
            return []

    def load_run(self, run_id):
        return self.read_json(self.p("runs", run_id, "run.json"))

    def load_mirror(self, run_id):
        return self.read_json(self.p("runs", run_id, "mirror.json"), {"files": {}})

    def read_log_lines(self, run_id):
        path = self.p("runs", run_id, "log.txt.gz")
        try:
            with gzip.open(path, "rb") as fh:
                return fh.read().decode("utf-8", "replace").split("\n")
        except OSError:
            return None

    def write_log_lines(self, run_id, lines):
        data = gzip.compress("\n".join(lines).encode("utf-8"), compresslevel=6)
        self.write_bytes(self.p("runs", run_id, "log.txt.gz"), data)
        return len(data)

    def dir_size(self, path):
        total = 0
        for base, _d, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        return total
