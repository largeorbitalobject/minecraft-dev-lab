from __future__ import annotations

import json
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .common import argv, atomic_json, fingerprint, identifier, load_json, within, json_process
from .processes import OwnedProcess

TERMINAL = {"PASSED", "FAILED", "CANCELLED", "TIMED_OUT", "BLOCKED", "OUTPUT_LIMIT"}


def own_command(*args):
    entry = Path(sys.argv[0])
    if entry.suffix == ".pyz":
        return [sys.executable, str(entry.resolve()), *args]
    return [sys.executable, "-m", "mcdevlab", *args]


@contextmanager
def database(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(state / "jobs.sqlite3", timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, instance TEXT NOT NULL,
      recipe TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL,
      spec TEXT NOT NULL, created REAL NOT NULL, heartbeat REAL NOT NULL,
      cancel INTEGER NOT NULL DEFAULT 0, receipt TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS exclusive_resource_v1 ON jobs(resource)
      WHERE state IN ('QUEUED','RUNNING','UNKNOWN');
    CREATE TABLE IF NOT EXISTS actions (
      request_id TEXT PRIMARY KEY, identity TEXT NOT NULL, resource TEXT NOT NULL,
      state TEXT NOT NULL, result TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS exclusive_action ON actions(resource)
      WHERE state='RUNNING';
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def public_job(row):
    result = {k: row[k] for k in ("id", "request_id", "instance", "recipe", "resource", "state", "created", "heartbeat")}
    result["cancel_requested"] = bool(row["cancel"])
    if row["state"] in {"QUEUED", "RUNNING"} and time.time() - row["heartbeat"] > 15:
        result["state"] = "UNKNOWN"
        result["note"] = "Supervisor heartbeat is stale; ownership remains reserved. Do not relaunch automatically."
    if row["receipt"]:
        result["receipt"] = json.loads(row["receipt"])
    return result


class Node:
    def __init__(self, config_path):
        self.path = Path(config_path).resolve()
        self.config = load_json(self.path)
        if self.config.get("schema") != "mcdev.node.v1":
            raise ValueError("Expected mcdev.node.v1 configuration")
        identifier(self.config.get("id"))
        state = Path(self.config["state_dir"]).expanduser()
        self.state = (state if state.is_absolute() else self.path.parent / state).resolve()
        self.instances = self.config.get("instances", {})
        if not isinstance(self.instances, dict):
            raise ValueError("instances must be an object")

    def instance(self, key):
        identifier(key)
        if key not in self.instances:
            raise ValueError("Unknown configured instance")
        value = self.instances[key]
        root = Path(value["root"]).expanduser()
        root = (root if root.is_absolute() else self.path.parent / root).resolve()
        if not root.is_dir():
            raise RuntimeError("Configured instance root is missing")
        return value, root

    def dispatch(self, request):
        op = request.get("op")
        if op == "probe":
            return {"schema": "mcdev.node.v1", "id": self.config["id"], "version": __version__,
                    "platform": platform.system(), "architecture": platform.machine(),
                    "python": platform.python_version(), "logical_cpus": os.cpu_count(),
                    "instances": [{"id": k, "label": v.get("label", k), "enabled": v.get("enabled", False),
                                   "recipes": sorted(v.get("recipes", {})), "bridge_configured": bool(v.get("bridge"))} for k, v in self.instances.items()],
                    "gpu": {"expected": self.config.get("expected_gpu"), "observed": None},
                    "note": "Expected GPU is configuration, not a renderer/device witness."}
        if op == "inspect":
            value, root = self.instance(request["instance"])
            result = {"id": request["instance"], "root_exists": True, "enabled": value.get("enabled", False),
                      "recipes": {k: {"kind": v.get("kind"), "timeout_seconds": v.get("timeout_seconds"), "resource": v.get("resource", "host")} for k, v in value.get("recipes", {}).items()},
                      "metadata": {}, "mods": []}
            for name in ("mmc-pack.json", "instance.cfg"):
                p = root / name
                if p.is_file() and not p.is_symlink():
                    result["metadata"][name] = fingerprint(p)
                    if name.endswith(".json") and p.stat().st_size < 65536:
                        pack = load_json(p)
                        result["components"] = [{"uid": c.get("uid"), "version": c.get("version")} for c in pack.get("components", [])[:128]]
            mods = within(root, value.get("mods_dir", "mods"))
            if mods.is_dir():
                entries = sorted(mods.glob("*.jar"))
                result["mods_truncated"] = len(entries) > 512
                for p in entries[:512]:
                    if p.is_symlink() or not p.is_file():
                        continue
                    result["mods"].append({"name": p.name, "bytes": p.stat().st_size})
            return result
        if op == "start":
            return self.start(request)
        if op == "list":
            limit = request.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("Run list limit must be1–100")
            with database(self.state) as db:
                rows = db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
                holders = db.execute("SELECT id,resource,state FROM jobs WHERE state IN ('QUEUED','RUNNING','UNKNOWN')").fetchall()
                actions = db.execute("SELECT request_id,resource,state FROM actions WHERE state='RUNNING'").fetchall()
            return {"runs": [public_job(row) for row in rows], "resource_holders": [dict(row) for row in holders], "action_holders": [dict(row) for row in actions]}
        if op in {"status", "cancel", "logs"}:
            job = identifier(request["job"])
            with database(self.state) as db:
                row = db.execute("SELECT * FROM jobs WHERE id=? OR request_id=?", (job, job)).fetchone()
                if row is None:
                    raise ValueError("Unknown job/request ID")
                if op == "cancel":
                    db.execute("UPDATE jobs SET cancel=1 WHERE id=? AND state IN ('QUEUED','RUNNING')", (row["id"],))
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if op == "logs":
                    limit = request.get("bytes", 16384)
                    if type(limit) is not int or not 1 <= limit <= 65536:
                        raise ValueError("Log bytes must be between1 and65536")
                    path = self.state / row["id"] / "output.log"
                    if not path.exists():
                        return {"job": row["id"], "text": "", "state": row["state"]}
                    with path.open("rb") as f:
                        f.seek(max(0, path.stat().st_size - limit)); data = f.read(limit)
                    from .evidence import triage_log, redact_log
                    text = data.decode("utf-8", "replace")
                    return {"job": row["id"], "triage": triage_log(text), "text": redact_log(text), "redaction": "Best effort; compiler diagnostics preserved. Review before sharing.", "raw_log": "Retained only on the node; use local output.log when authorized."}
                return public_job(row)
        if op == "game_action_result":
            req = identifier(request["request_id"])
            with database(self.state) as db:
                row = db.execute("SELECT * FROM actions WHERE request_id=?", (req,)).fetchone()
            if not row:
                raise ValueError("Unknown action request ID")
            return json.loads(row["result"]) if row["result"] else {"status": "unknown", "request_id": req, "outcome": "unknown", "note": "Action may still be in progress; no replay."}
        if op in {"bridge_capabilities", "game_action"}:
            value, _ = self.instance(request["instance"])
            from .bridges import inspect_bridge, invoke_bridge
            bridge = value.get("bridge")
            if not bridge:
                return {"available": False, "reason": "No verified bridge adapter configured for this instance"}
            if op == "bridge_capabilities":
                return inspect_bridge(bridge)
            if value.get("enabled") is not True:
                raise RuntimeError("Instance actions are disabled in node configuration")
            # Read-only status remains a provider operation, not a desktop fallback.
            action = request["request"]; req = identifier(action["request_id"])
            identity = json.dumps({"instance": request["instance"], "request": action, "owner_job": request.get("owner_job")}, sort_keys=True, allow_nan=False)
            resource = identifier(bridge.get("resource", "host"))
            with database(self.state) as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT * FROM actions WHERE request_id=?", (req,)).fetchone()
                if previous:
                    if previous["identity"] != identity:
                        raise ValueError("Action request ID belongs to different arguments")
                    return json.loads(previous["result"]) if previous["result"] else {"status": "unknown", "request_id": req, "outcome": "unknown", "note": "Request already dispatched or in progress; not replayed."}
                holder = db.execute("SELECT * FROM jobs WHERE resource=? AND state IN ('QUEUED','RUNNING','UNKNOWN')", (resource,)).fetchone()
                if holder and (holder["id"] != request.get("owner_job") or holder["instance"] != request["instance"] or holder["state"] != "RUNNING" or time.time() - holder["heartbeat"] > 15 or json.loads(holder["spec"])["kind"] != "client"):
                    raise RuntimeError("Gameplay resource held by another or uncertain owner")
                if request.get("owner_job") and not holder:
                    raise RuntimeError("Expected client owner is no longer active")
                try:
                    db.execute("INSERT INTO actions VALUES (?,?,?,'RUNNING',NULL)", (req, identity, resource))
                except sqlite3.IntegrityError as e:
                    raise RuntimeError("Another action is still in progress or uncertain on this resource") from e
            try:
                result = invoke_bridge(bridge, action)
            except Exception:
                result = {"status": "unknown", "outcome": "unknown", "request_id": req, "note": "Provider did not return a validated outcome; original request will not replay."}
            with database(self.state) as db:
                # An uncertain provider may still own native work. Keep its resource
                # reserved until an operator reconciles it outside this MCP session.
                state = "RUNNING" if result.get("outcome") == "unknown" else "COMPLETE"
                db.execute("UPDATE actions SET state=?,result=? WHERE request_id=?", (state, json.dumps(result), req))
            return result
        raise ValueError("Unsupported node operation")

    def start(self, request):
        instance = identifier(request.get("instance")); recipe = identifier(request.get("recipe")); req = identifier(request.get("request_id"))
        if req.startswith("job-"):
            raise ValueError("Request IDs must not use the reserved job- prefix")
        value, root = self.instance(instance)
        if value.get("enabled") is not True:
            raise RuntimeError("Instance actions are disabled in node configuration")
        recipes = value.get("recipes", {})
        if recipe not in recipes:
            raise ValueError("Unknown configured recipe; commands cannot be supplied by MCP callers")
        spec = dict(recipes[recipe]); spec["argv"] = argv(spec.get("argv"))
        timeout = spec.get("timeout_seconds", 180)
        if type(timeout) is not int or not 1 <= timeout <= 7200:
            raise ValueError("Recipe timeout must be1–7200 seconds")
        if spec.get("kind") not in {"build", "test", "client", "inspect"}:
            raise ValueError("Recipe kind must be build, test, client or inspect")
        spec["cwd"] = str(within(root, spec.get("cwd", ".")))
        if not Path(spec["cwd"]).is_dir():
            raise ValueError("Recipe working directory is missing")
        guards = self.config.get("guards", []) + spec.get("guards", [])
        spec["guards"] = [argv(g) for g in guards]
        if spec["kind"] == "client" and not guards:
            raise ValueError("Client recipe requires an explicit live occupancy/resource guard")
        spec["timeout_seconds"] = timeout
        spec["output_limit_bytes"] = min(16 * 1024 * 1024, max(4096, int(spec.get("output_limit_bytes", 2 * 1024 * 1024))))
        spec["instance_root"] = str(root)
        spec["config_sha256"] = fingerprint(self.path)["sha256"]
        resource = identifier(spec.get("resource", "host"))
        now = time.time(); job = "job-" + uuid.uuid4().hex
        with database(self.state) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE request_id=?", (req,)).fetchone()
            if old:
                if old["instance"] != instance or old["recipe"] != recipe:
                    raise ValueError("Request ID already belongs to another operation")
                return public_job(old)
            if db.execute("SELECT 1 FROM actions WHERE resource=? AND state='RUNNING'", (resource,)).fetchone():
                raise RuntimeError("Resource reserved by an active or uncertain game action")
            try:
                db.execute("INSERT INTO jobs(id,request_id,instance,recipe,resource,state,spec,created,heartbeat) VALUES (?,?,?,?,?,'QUEUED',?,?,?)",
                           (job, req, instance, recipe, resource, json.dumps(spec, allow_nan=False), now, now))
            except sqlite3.IntegrityError as e:
                holder = db.execute("SELECT id FROM jobs WHERE resource=? AND state IN ('QUEUED','RUNNING','UNKNOWN')", (resource,)).fetchone()
                raise RuntimeError("Resource already leased by job " + (holder[0] if holder else "unknown")) from e
        directory = self.state / job
        directory.mkdir(mode=0o700)
        try:
            with (directory / "supervisor.log").open("wb") as log:
                supervisor = subprocess.Popen(own_command("worker", "--state", str(self.state), "--job", job),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, shell=False,
                                 cwd=str(Path(__file__).resolve().parent.parent) if Path(sys.argv[0]).suffix != ".pyz" else str(root),
                                 start_new_session=os.name != "nt",
                                 creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0)
                # Reap in persistent MCP/direct-API processes; a one-shot agent may
                # exit first, leaving the independently owned supervisor running.
                threading.Thread(target=supervisor.wait, daemon=True).start()
        except OSError as e:
            with database(self.state) as db:
                db.execute("UPDATE jobs SET state='FAILED',receipt=? WHERE id=?", (json.dumps({"error": "Supervisor could not start", "type": type(e).__name__}), job))
            raise RuntimeError("Supervisor could not start; request is recorded and will not replay") from e
        return {"id": job, "request_id": req, "state": "QUEUED", "resource": resource,
                "note": "Process completion is distinct from game/world or performance acceptance."}


def _guard(spec):
    for command in spec.get("guards", []):
        # Guard output contract is strict. No guard means no policy was asserted.
        result = json_process(command, {"op": "guard", "kind": spec["kind"]}, timeout=5, max_output=65536)
        if result.get("allowed") is not True:
            raise RuntimeError("Configured occupancy/resource guard denied admission")


def worker(state: Path, job: str):
    identifier(job); state = state.resolve(); directory = state / job
    with database(state) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        if row is None or row["state"] != "QUEUED":
            return 2
        db.execute("UPDATE jobs SET state='RUNNING',heartbeat=? WHERE id=?", (time.time(), job))
    spec = json.loads(row["spec"]); process = None; final = "FAILED"; error = None; exit_code = None
    started = time.time(); overflow = threading.Event(); pump_error = threading.Event(); byte_count = [0]; pump = None
    try:
        _guard(spec)
        with database(state) as db:
            if db.execute("SELECT cancel FROM jobs WHERE id=?", (job,)).fetchone()[0]:
                final = "CANCELLED"; return 0
        # Pass only the environment inherited by this explicitly configured node.
        # Auth/launcher paths live in recipes/bridge providers, never MCP arguments.
        token = secrets.token_hex(32)
        atomic_json(directory / "exec.json", {"argv": spec["argv"], "cwd": spec["cwd"], "gate": token})
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "wb", buffering=0) as stream:
            process = OwnedProcess(own_command("exec", "--state", str(state), "--job", job), stream, cwd=str(Path(__file__).resolve().parent.parent) if Path(sys.argv[0]).suffix != ".pyz" else spec["cwd"])
        def consume():
            try:
                with os.fdopen(read_fd, "rb", buffering=0) as incoming, (directory / "output.log").open("wb") as output:
                    while block := incoming.read(8192):
                        room = max(0, spec["output_limit_bytes"] - byte_count[0])
                        output.write(block[:room]); output.flush(); byte_count[0] += min(room, len(block))
                        if len(block) > room:
                            overflow.set()
            except Exception:
                pump_error.set()
        pump = threading.Thread(target=consume, daemon=True); pump.start()
        # Gate creation follows POSIX group establishment / Windows Job assignment.
        atomic_json(directory / "gate.json", {"token": token})
        deadline = time.monotonic() + spec["timeout_seconds"]; next_guard = time.monotonic() + 2
        while process.process.poll() is None:
            with database(state) as db:
                cancel = db.execute("SELECT cancel FROM jobs WHERE id=?", (job,)).fetchone()[0]
                db.execute("UPDATE jobs SET heartbeat=? WHERE id=?", (time.time(), job))
            if cancel:
                final = "CANCELLED"; break
            if overflow.is_set():
                final = "OUTPUT_LIMIT"; break
            if pump_error.is_set():
                final = "FAILED"; error = {"type": "LogCaptureFailed", "message": "Local diagnostic capture failed"}; break
            if time.monotonic() >= deadline:
                final = "TIMED_OUT"; break
            if time.monotonic() >= next_guard:
                _guard(spec); next_guard = time.monotonic() + 2
            time.sleep(0.05)
        else:
            exit_code = process.process.returncode
            final = "PASSED" if exit_code == 0 else "FAILED"
    except Exception as exc:
        final = "BLOCKED" if isinstance(exc, RuntimeError) else "FAILED"
        error = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        if process:
            try:
                process.close(); exit_code = process.process.returncode
            except Exception as exc:
                final = "UNKNOWN"; error = {"type": type(exc).__name__, "message": "Owned process cleanup unproven; resource remains reserved"}
        if pump:
            pump.join(timeout=3)
            if pump.is_alive():
                final = "UNKNOWN"; error = {"type": "CleanupUncertain", "message": "Output pipe still owned after cleanup; resource remains reserved"}
        if overflow.is_set() and final == "PASSED":
            final = "OUTPUT_LIMIT"
        if pump_error.is_set() and final != "UNKNOWN":
            final = "FAILED"; error = {"type": "LogCaptureFailed", "message": "Local diagnostic capture failed"}
        artifacts = []
        if final == "PASSED":
            try:
                artifact_budget = 512 * 1024 * 1024
                for relative in spec.get("artifacts", [])[:32]:
                    path = within(Path(spec["instance_root"]), relative)
                    found = fingerprint(path, artifact_budget)
                    artifact_budget -= found["bytes"]
                    artifacts.append(found)
            except Exception as exc:
                final = "FAILED"; error = {"type": type(exc).__name__, "message": "Required artifact missing or unreadable"}
        receipt = {"schema": "mcdev.run.v1", "job": job, "request_id": row["request_id"], "instance": row["instance"],
                   "recipe": row["recipe"], "kind": spec["kind"], "status": final, "started": started, "finished": time.time(),
                   "exit_code": exit_code, "log_bytes": byte_count[0], "config_sha256": spec["config_sha256"],
                   "artifacts": artifacts, "game_acceptance": "NOT_RUN", "error": error}
        atomic_json(directory / "receipt.json", receipt)
        with database(state) as db:
            db.execute("UPDATE jobs SET state=?,heartbeat=?,receipt=? WHERE id=?", (final, time.time(), json.dumps(receipt), job))
    return 0


def exec_recipe(state, job):
    directory = Path(state) / identifier(job); spec = load_json(directory / "exec.json")
    end = time.monotonic() + 10
    while not (directory / "gate.json").exists():
        if time.monotonic() >= end:
            return 124
        time.sleep(0.01)
    if load_json(directory / "gate.json").get("token") != spec["gate"]:
        return 126
    command = argv(spec["argv"])
    os.chdir(spec["cwd"])
    if os.name != "nt":
        os.execvpe(command[0], command, os.environ)
    return subprocess.call(command, shell=False)
