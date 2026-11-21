#!/usr/bin/env python3
"""Minecraft Dev Lab fleet orchestrator (orbital-pve).

Unattended loop: discover Atlas candidates (newest codex/* tip in CT224's repo, plus manual
submissions), build on CT224, gate on CT224, then push/install/gate on fd, GPC and the Mac.
Desktop hosts are idle-gated by their own recipes; refusals are retried until the candidate is
superseded or expires. Mac c39 packets are validated periodically. Results go to STATUS.md /
status.json and a rate-limited bus digest to Codex; only infrastructure trouble pings the phone.

State lives in SQLite. Every Dev Lab request_id is derived from (host, step, commit, attempt) and
recorded BEFORE dispatch, so a restart re-polls the same job instead of starting a second one.
"""
from __future__ import annotations

import argparse, datetime, json, re, shutil, sqlite3, subprocess, sys, time, traceback
from pathlib import Path

PYZ = "/root/minecraft-dev-lab-install/plugins/minecraft-dev-lab/minecraft-dev-lab.pyz"
FLEET = "/root/minecraft-dev-lab-config/fleet.json"
HOME = Path("/root/minecraft-dev-lab-config/orchestrator")
CONFIG = HOME / "config.json"
DB = HOME / "state.sqlite3"
ARCHIVE = Path("/root/minecraft-dev-lab-config/ctl/archive")
STAGED = Path("/root/minecraft-dev-lab-config/ctl/candidates")
BUS = "http://<lan-ip>:8090"
NOTIFY = "/orbital/scripts/orbital-notify.sh"

DEFAULTS = {
    "repo": "/root/atlas-p1-frame-association", "worktree": "/root/mcdevlab-atlas",
    "track_pattern": "refs/heads/codex/", "discover_seconds": 120, "tick_seconds": 20,
    "ct224": {"host": "proxmox-test", "instance": "atlas", "build": "build", "gate": "client-smoke"},
    "hosts": [
        {"host": "amd", "instance": "atlas", "push": "push-staged-fd", "install": "install-candidate", "gate": "client-feature-plan", "desktop": False},
        {"host": "gpc", "instance": "atlas-gpc", "push": "push-staged-gpc", "install": "install-candidate", "gate": "client-smoke", "desktop": True},
        {"host": "mac", "instance": "atlas", "push": "push-staged-mac", "install": "install-candidate", "gate": "client-smoke", "desktop": True},
    ],
    "retry_busy_seconds": 120, "retry_desktop_seconds": 600, "desktop_deadline_hours": 24,
    "c39_validate_seconds": 1800, "health_seconds": 900, "health_alert_failures": 4,
    "digest_min_seconds": 900, "bus_topic": "orbital-agent-pve",
}
TERMINAL = {"PASSED", "FAILED", "CANCELLED", "TIMED_OUT", "BLOCKED", "OUTPUT_LIMIT"}
RETRYABLE = re.compile(r"REFUSED|BUSY|owns the build/client slot|not idle|idle \d|owner_idle|Another Atlas workload|"
                       r"guard denied|PAUSED|NO_CONSOLE|quarantine|paused")

sys.path.insert(0, PYZ)
from mcdevlab.api import LabAPI  # noqa: E402  (bundled zipapp)


def now(): return time.time()
def utc(t=None): return datetime.datetime.fromtimestamp(t or now(), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def log(msg): print(f"{utc()} {msg}", flush=True)


class Orchestrator:
    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        cfg = dict(DEFAULTS)
        if CONFIG.exists():
            cfg.update(json.loads(CONFIG.read_text()))
        else:
            CONFIG.write_text(json.dumps(DEFAULTS, indent=1) + "\n")
        self.cfg = cfg
        self.api = LabAPI(FLEET)
        self.db = sqlite3.connect(DB, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY, commit_sha TEXT UNIQUE, ref TEXT, source TEXT,
          created REAL, state TEXT, jar_sha TEXT, build_attempt INTEGER DEFAULT 0, build_request TEXT, built REAL,
          next_try REAL DEFAULT 0, detail TEXT);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, candidate_id INTEGER, host TEXT, state TEXT,
          attempt INTEGER DEFAULT 0, request_id TEXT, next_try REAL DEFAULT 0, started REAL, finished REAL,
          verdict TEXT, detail TEXT, UNIQUE(candidate_id, host));
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS digest(id INTEGER PRIMARY KEY, created REAL, line TEXT, sent INTEGER DEFAULT 0);
        """)

    # ---- small helpers -------------------------------------------------------------------
    def kv(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def setkv(self, k, v): self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

    def call(self, tool, args): return self.api.call(tool, args)

    def start(self, host, instance, recipe, request_id):
        return self.call("run_start", {"host": host, "instance": instance, "recipe": recipe, "request_id": request_id})

    def status(self, host, job): return self.call("run_status", {"host": host, "job": job})

    def logs(self, host, job, n=6000):
        try: return self.call("run_logs", {"host": host, "job": job, "bytes": n}).get("text", "")
        except Exception as e: return f"(logs unavailable: {e})"

    def wait(self, host, job, timeout):
        end = now() + timeout
        while now() < end:
            s = self.status(host, job)
            if s["state"] in TERMINAL or s["state"] == "UNKNOWN":
                return s
            time.sleep(3)
        raise TimeoutError(f"{host} {job} did not finish in {timeout}s")

    def ct(self, *cmd, timeout=60):
        return subprocess.run(["pct", "exec", "224", "--", *cmd], capture_output=True, text=True, timeout=timeout)

    def digest(self, line): self.db.execute("INSERT INTO digest(created,line) VALUES (?,?)", (now(), line))

    @staticmethod
    def verdict_from_log(text):
        found = re.findall(r'"status"\s*:\s*"([A-Z_]+)"', text) or re.findall(r'"verdict"\s*:\s*"([A-Z_]+)"', text)
        return found[-1] if found else None

    # ---- discovery ---------------------------------------------------------------------
    def discover(self):
        if now() - self.kv("last_discover", 0) < self.cfg["discover_seconds"]:
            return
        self.setkv("last_discover", now())
        r = self.ct("git", "-C", self.cfg["repo"], "for-each-ref", "--sort=-committerdate", "--count=1",
                    "--format=%(refname:short) %(objectname)", self.cfg["track_pattern"])
        if r.returncode or not r.stdout.strip():
            raise RuntimeError(f"branch discovery failed: {r.stderr.strip()[:200]}")
        ref, sha = r.stdout.split()
        if self.db.execute("SELECT 1 FROM candidates WHERE commit_sha=?", (sha,)).fetchone():
            return
        self.db.execute("INSERT INTO candidates(commit_sha,ref,source,created,state) VALUES (?,?,?,?, 'queued')",
                        (sha, ref, "auto", now()))
        log(f"discovered {ref} {sha[:8]}")
        # Newer auto candidate supersedes older auto work that has not started anywhere.
        self.db.execute("""UPDATE candidates SET state='superseded' WHERE source='auto' AND state='queued' AND commit_sha<>?""", (sha,))
        self.db.execute("""UPDATE runs SET state='superseded', finished=? WHERE state IN ('pending','retry') AND candidate_id IN
                           (SELECT id FROM candidates WHERE source='auto' AND commit_sha<>?)""", (now(), sha))

    def ingest_manual(self):
        """Manual submissions: files in orchestrator/queue/*.json {"ref": "<sha|branch>"}."""
        qdir = HOME / "queue"; qdir.mkdir(exist_ok=True)
        for f in sorted(qdir.glob("*.json")):
            try:
                ref = json.loads(f.read_text())["ref"]
                r = self.ct("git", "-C", self.cfg["repo"], "rev-parse", "--verify", ref + "^{commit}")
                if r.returncode:
                    raise ValueError(f"unknown ref {ref}")
                sha = r.stdout.strip()
                self.db.execute("""INSERT INTO candidates(commit_sha,ref,source,created,state) VALUES (?,?,?,?, 'queued')
                                   ON CONFLICT(commit_sha) DO UPDATE SET source='manual',
                                   state=CASE WHEN state='superseded' THEN 'queued' ELSE state END""", (sha, ref, "manual", now()))
                log(f"manual submission {ref} {sha[:8]}")
                f.rename(f.with_suffix(".accepted"))
            except Exception as e:
                log(f"rejected submission {f.name}: {e}"); f.rename(f.with_suffix(".rejected"))

    # ---- CT224: build + gate as one unit (the gate uses the worktree's build) ------------
    def ct224_busy_with_gate(self):
        return self.db.execute("""SELECT 1 FROM runs WHERE host=? AND state IN ('pending','retry','running')""",
                               (self.cfg["ct224"]["host"],)).fetchone()

    def ct224_lock_free(self):
        return self.ct("flock", "-n", "/run/lock/orbital-client/work.lock", "true").returncode == 0

    def step_build(self):
        c = self.cfg["ct224"]
        cand = self.db.execute("SELECT * FROM candidates WHERE state='building'").fetchone()
        if cand is None:
            if self.ct224_busy_with_gate():
                return
            cand = self.db.execute("""SELECT * FROM candidates WHERE state='queued' AND next_try<=?
                                      ORDER BY (source='manual') DESC, created DESC LIMIT 1""", (now(),)).fetchone()
            if cand is None:
                return
            if not self.ct224_lock_free():
                self.db.execute("UPDATE candidates SET next_try=? WHERE id=?", (now() + self.cfg["retry_busy_seconds"], cand["id"]))
                return
            co = self.ct("git", "-C", self.cfg["worktree"], "checkout", "--quiet", "--detach", cand["commit_sha"])
            if co.returncode:
                self.db.execute("UPDATE candidates SET state='build_failed', detail=? WHERE id=?", (f"checkout: {co.stderr[:300]}", cand["id"]))
                return
            attempt = cand["build_attempt"] + 1
            req = f"orch-build-{cand['commit_sha'][:10]}-{attempt}"
            self.db.execute("UPDATE candidates SET state='building', build_attempt=?, build_request=? WHERE id=?", (attempt, req, cand["id"]))
            self.start(c["host"], c["instance"], c["build"], req)
            log(f"build {cand['commit_sha'][:8]} started ({req})")
            return
        s = self.status(c["host"], cand["build_request"])
        if s["state"] not in TERMINAL:
            return
        text = self.logs(c["host"], cand["build_request"])
        if s["state"] == "PASSED":
            self.archive(cand)
            hosts = [c["host"]] + [h["host"] for h in self.cfg["hosts"]]
            for h in hosts:
                self.db.execute("INSERT OR IGNORE INTO runs(candidate_id,host,state) VALUES (?,?, 'pending')", (cand["id"], h))
            self.db.execute("UPDATE candidates SET state='built', built=? WHERE id=?", (now(), cand["id"]))
            log(f"build {cand['commit_sha'][:8]} PASSED")
        elif RETRYABLE.search(text):
            self.db.execute("UPDATE candidates SET state='queued', next_try=? WHERE id=?", (now() + self.cfg["retry_busy_seconds"], cand["id"]))
        else:
            tail = "\n".join(text.strip().splitlines()[-12:])
            self.db.execute("UPDATE candidates SET state='build_failed', detail=? WHERE id=?", (tail[-1500:], cand["id"]))
            self.digest(f"BUILD FAILED {cand['ref']} {cand['commit_sha'][:8]}: {tail.splitlines()[-1][:200] if tail else s['state']}")

    def archive(self, cand):
        dest = ARCHIVE / cand["commit_sha"]; dest.mkdir(parents=True, exist_ok=True)
        src = self.cfg["worktree"] + "/mcdevlab-results"
        for name in ("candidate.json", "candidate.jar"):
            subprocess.run(["pct", "pull", "224", f"{src}/{name}", str(dest / name)], check=True, timeout=120)
        meta = json.loads((dest / "candidate.json").read_text())
        import hashlib
        got = hashlib.sha256((dest / "candidate.jar").read_bytes()).hexdigest()
        if got != meta["sha256"] or meta.get("source_commit") != cand["commit_sha"]:
            raise RuntimeError(f"archived candidate mismatch for {cand['commit_sha'][:8]}")
        self.db.execute("UPDATE candidates SET jar_sha=? WHERE id=?", (got, cand["id"]))

    # ---- per-host gate runs --------------------------------------------------------------
    def host_spec(self, host):
        if host == self.cfg["ct224"]["host"]:
            c = self.cfg["ct224"]; return {"host": host, "instance": c["instance"], "gate": c["gate"], "desktop": False}
        return next(h for h in self.cfg["hosts"] if h["host"] == host)

    def step_runs(self):
        for run in self.db.execute("""SELECT r.*, c.commit_sha, c.ref, c.jar_sha FROM runs r JOIN candidates c ON c.id=r.candidate_id
                                       WHERE r.state IN ('pending','retry','running') ORDER BY c.created DESC""").fetchall():
            try:
                if run["state"] == "running":
                    self.poll_run(run)
                elif run["next_try"] <= now() and not self.db.execute(
                        "SELECT 1 FROM runs WHERE host=? AND state='running'", (run["host"],)).fetchone():
                    self.launch_run(run)
            except Exception as e:
                log(f"run {run['host']} {run['commit_sha'][:8]}: {type(e).__name__}: {e}")
                self.db.execute("UPDATE runs SET state='retry', next_try=?, detail=? WHERE id=?",
                                (now() + self.cfg["retry_busy_seconds"], f"{type(e).__name__}: {e}"[:500], run["id"]))

    def launch_run(self, run):
        spec = self.host_spec(run["host"])
        built = self.db.execute("SELECT built FROM candidates WHERE id=?", (run["candidate_id"],)).fetchone()[0] or now()
        if spec.get("desktop") and now() - built > self.cfg["desktop_deadline_hours"] * 3600:
            self.db.execute("UPDATE runs SET state='expired', finished=? WHERE id=?", (now(), run["id"])); return
        if run["host"] == self.cfg["ct224"]["host"] and not self.ct224_lock_free():
            self.db.execute("UPDATE runs SET state='retry', next_try=?, detail='CT224 slot busy' WHERE id=?",
                            (now() + self.cfg["retry_busy_seconds"], run["id"])); return
        attempt = run["attempt"] + 1
        tag = f"{run['commit_sha'][:10]}-{attempt}"
        if "push" in spec:
            # Exact archived bytes for this commit, never whatever CT224 built most recently.
            src = ARCHIVE / run["commit_sha"]
            STAGED.mkdir(parents=True, exist_ok=True)
            for name in ("candidate.jar", "candidate.json"):
                shutil.copyfile(src / name, STAGED / (name + ".tmp")); (STAGED / (name + ".tmp")).replace(STAGED / name)
            push = self.wait("ctl", self.start("ctl", "candidates", spec["push"], f"orch-push-{run['host']}-{tag}")["id"], 400)
            if push["state"] != "PASSED":
                raise RuntimeError(f"push {push['state']}")
            inst = self.wait(run["host"], self.start(run["host"], spec["instance"], spec["install"], f"orch-install-{run['host']}-{tag}")["id"], 300)
            if inst["state"] != "PASSED":
                text = self.logs(run["host"], inst["id"])
                raise RuntimeError(f"install {inst['state']}: {text.strip().splitlines()[-1][:200] if text.strip() else ''}")
        req = f"orch-gate-{run['host']}-{tag}"
        self.db.execute("UPDATE runs SET state='running', attempt=?, request_id=?, started=? WHERE id=?", (attempt, req, now(), run["id"]))
        self.start(run["host"], spec["instance"], spec["gate"], req)
        log(f"gate {run['host']} {run['commit_sha'][:8]} started ({req})")

    def poll_run(self, run):
        s = self.status(run["host"], run["request_id"])
        if s["state"] == "UNKNOWN":
            if not self.kv(f"unknown-{run['request_id']}"):
                self.setkv(f"unknown-{run['request_id']}", now())
                self.alert("warn", f"Dev Lab job UNKNOWN on {run['host']}",
                           f"{run['request_id']} lost its supervisor heartbeat; resource stays reserved until reconciled.")
            return
        if s["state"] not in TERMINAL:
            return
        text = self.logs(run["host"], run["request_id"], 12000)
        verdict = self.verdict_from_log(text) or s["state"]
        spec = self.host_spec(run["host"])
        if s["state"] == "PASSED":
            state, verdict = "pass", "PASS"
        elif s["state"] == "BLOCKED" or (verdict and RETRYABLE.search(verdict)) or RETRYABLE.search(text[-3000:]):
            wait = self.cfg["retry_desktop_seconds"] if spec.get("desktop") else self.cfg["retry_busy_seconds"]
            self.db.execute("UPDATE runs SET state='retry', next_try=?, detail=? WHERE id=?",
                            (now() + wait, f"{s['state']} {verdict}", run["id"]))
            return
        else:
            state = "fail"
        detail = self.extract_detail(text)
        self.db.execute("UPDATE runs SET state=?, verdict=?, finished=?, detail=? WHERE id=?",
                        (state, verdict, now(), detail, run["id"]))
        self.digest(f"{run['host']}: {verdict} {run['ref']} {run['commit_sha'][:8]}" + (f" — {detail[:220]}" if state == "fail" else ""))
        log(f"gate {run['host']} {run['commit_sha'][:8]} -> {verdict}")

    @staticmethod
    def extract_detail(text):
        for pat in (r"(INCOMPLETE feature-plan[^\n\"]*)", r'"reason"\s*:\s*"([^"]+)"', r"(GL error[^\n\"]*)",
                    r'"exception"\s*:\s*"([^"]{0,300})', r"(PASS feature-plan ON gate[^\n\"]*)"):
            m = re.findall(pat, text)
            if m:
                return m[-1][:600]
        return "\n".join(text.strip().splitlines()[-4:])[-600:]

    def finish_candidates(self):
        for cand in self.db.execute("SELECT * FROM candidates WHERE state='built'").fetchall():
            if not self.db.execute("SELECT 1 FROM runs WHERE candidate_id=? AND state IN ('pending','retry','running')", (cand["id"],)).fetchone():
                self.db.execute("UPDATE candidates SET state='done' WHERE id=?", (cand["id"],))

    # ---- periodic jobs -------------------------------------------------------------------
    def c39(self):
        if now() - self.kv("last_c39", 0) < self.cfg["c39_validate_seconds"]:
            return
        self.setkv("last_c39", now())
        before = set(p.name for p in Path("/root/minecraft-dev-lab-config/ctl/c39-validations").glob("*.json"))
        s = self.wait("ctl", self.start("ctl", "candidates", "validate-mac-c39", f"orch-c39-{int(now())}")["id"], 400)
        for p in Path("/root/minecraft-dev-lab-config/ctl/c39-validations").glob("*.json"):
            if p.name not in before and p.name != "latest.json":
                r = json.loads(p.read_text())
                self.digest(f"mac c39 packet {r['packet']} -> {r['verdict']} {json.dumps(r.get('stats') or r.get('refusal') or '')[:200]}")
        if s["state"] not in ("PASSED",) and s["state"] != "FAILED":
            log(f"c39 validation {s['state']}")

    def health(self):
        if now() - self.kv("last_health", 0) < self.cfg["health_seconds"]:
            return
        self.setkv("last_health", now())
        fails = self.kv("health_fails", {})
        for host in ["ctl", self.cfg["ct224"]["host"]] + [h["host"] for h in self.cfg["hosts"]]:
            try:
                self.call("host_probe", {"host": host}); ok = True
            except Exception as e:
                ok = False; err = f"{type(e).__name__}: {e}"[:200]
            n = 0 if ok else fails.get(host, 0) + 1
            if n == self.cfg["health_alert_failures"]:
                self.alert("warn", f"Dev Lab host {host} unreachable", f"{n} consecutive probe failures over ~{n * self.cfg['health_seconds'] // 60} min: {err}")
            if ok and fails.get(host, 0) >= self.cfg["health_alert_failures"]:
                self.alert("info", f"Dev Lab host {host} recovered", "Probe succeeds again.")
            fails[host] = n
        self.setkv("health_fails", fails)

    def alert(self, level, title, msg):
        try: subprocess.run([NOTIFY, level, title, msg, "video_game"], timeout=20)
        except Exception as e: log(f"notify failed: {e}")

    def flush_digest(self):
        rows = self.db.execute("SELECT * FROM digest WHERE sent=0 ORDER BY id").fetchall()
        if not rows or now() - self.kv("last_digest", 0) < self.cfg["digest_min_seconds"]:
            return
        lines = [r["line"] for r in rows][:40]
        body = {"v": 1, "id": f"mcdevlab-orch-digest-{int(now())}", "ts": utc(), "from": "agent@orbital-pve", "type": "event",
                "subject": f"Dev Lab orchestrator digest ({len(rows)} results)",
                "body": "Automated fleet results (status: /root/minecraft-dev-lab-config/orchestrator/STATUS.md). " + " | ".join(lines),
                "corr": None}
        data = json.dumps(body)
        while len(data.encode()) >= 3900 and lines:
            lines.pop(); body["body"] = "Automated fleet results (truncated; see STATUS.md). " + " | ".join(lines); data = json.dumps(body)
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--data-binary", data,
                            "-H", "Title: To Codex: Orbital Atlas Dev Lab results digest", "-H", "Tags: event",
                            f"{BUS}/{self.cfg['bus_topic']}"], capture_output=True, text=True, timeout=30)
        if r.stdout.strip() == "200":
            self.db.execute(f"UPDATE digest SET sent=1 WHERE id IN ({','.join(str(r['id']) for r in rows)})")
            self.setkv("last_digest", now())
        elif r.stdout.strip() == "429":
            self.setkv("last_digest", now() - self.cfg["digest_min_seconds"] + 60)

    def write_status(self):
        cands = self.db.execute("SELECT * FROM candidates ORDER BY created DESC LIMIT 15").fetchall()
        out = {"utc": utc(), "candidates": []}
        md = [f"# Dev Lab fleet status\n\nUpdated {utc()} by the orchestrator (systemd `mcdevlab-orchestrator`).\n",
              "| commit | ref | source | state | " + " | ".join(["ct224", "fd (amd)", "gpc", "mac"]) + " |", "|---|---|---|---|---|---|---|---|"]
        for c in cands:
            runs = {r["host"]: r for r in self.db.execute("SELECT * FROM runs WHERE candidate_id=?", (c["id"],))}
            def cell(h):
                r = runs.get(h)
                if not r: return "—"
                return {"pass": "PASS", "fail": f"FAIL ({(r['verdict'] or '')})", "running": "running", "retry": "waiting",
                        "pending": "queued", "superseded": "superseded", "expired": "expired"}.get(r["state"], r["state"])
            md.append(f"| `{c['commit_sha'][:8]}` | {c['ref']} | {c['source']} | {c['state']} | " +
                      " | ".join(cell(h) for h in ("proxmox-test", "amd", "gpc", "mac")) + " |")
            out["candidates"].append({**dict(c), "runs": {h: dict(r) for h, r in runs.items()}})
        fails = self.db.execute("""SELECT r.host, r.verdict, r.detail, c.commit_sha FROM runs r JOIN candidates c ON c.id=r.candidate_id
                                   WHERE r.state='fail' ORDER BY r.finished DESC LIMIT 8""").fetchall()
        if fails:
            md.append("\n## Recent failures\n")
            md += [f"- `{f['commit_sha'][:8]}` **{f['host']}** {f['verdict']}: {(f['detail'] or '')[:300]}" for f in fails]
        (HOME / "STATUS.md").write_text("\n".join(md) + "\n")
        (HOME / "status.json").write_text(json.dumps(out, indent=1, default=str) + "\n")

    # ---- main loop -----------------------------------------------------------------------
    def tick(self):
        for step in (self.ingest_manual, self.discover, self.step_build, self.step_runs, self.finish_candidates,
                     self.c39, self.health, self.flush_digest, self.write_status):
            try:
                step()
            except Exception as e:
                log(f"{step.__name__}: {type(e).__name__}: {e}")
                if not isinstance(e, (TimeoutError, RuntimeError)):
                    traceback.print_exc()

    def run(self):
        log("orchestrator started")
        while True:
            self.tick()
            time.sleep(self.cfg["tick_seconds"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run"); sub.add_parser("once"); sub.add_parser("status")
    s = sub.add_parser("submit", help="queue a commit or branch from CT224's Atlas repo"); s.add_argument("ref")
    a = ap.parse_args()
    if a.cmd == "submit":
        q = HOME / "queue"; q.mkdir(parents=True, exist_ok=True)
        (q / f"{int(now() * 1000)}.json").write_text(json.dumps({"ref": a.ref}))
        print(f"queued {a.ref}"); return
    if a.cmd == "status":
        print((HOME / "STATUS.md").read_text() if (HOME / "STATUS.md").exists() else "no status yet"); return
    o = Orchestrator()
    o.tick() if a.cmd == "once" else o.run()


if __name__ == "__main__":
    main()
