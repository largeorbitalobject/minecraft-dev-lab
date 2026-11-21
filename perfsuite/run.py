#!/usr/bin/env python3
"""Orbital Atlas perf suite runner: queue scenarios per host on the existing one-command harness runners.

  run.py --builds rc.160,rc.159,rc.142 --hosts fd,gpc,mac --set quick [--reps 2] [--arm base] [--jfr] [--dry]
  run.py --builds rc.160 --hosts fd --scenarios q-basepack,n-mixed --arm cap120

Builds: 'rc.NNN' -> pinned/candidate-0.2.0-beta.1-rc.NNN.{jar,json}; anything else -> pinned/candidate-<name>.{jar,json}.
Never use candidate-release-* (release jars carry no harness steps).
Hosts run in parallel (one thread per host, each host strictly sequential). Order per host: for each repeat, every scenario,
builds rotated per repeat (ABC, BCA, ...) so drift does not line up with a build.
Etiquette built in: Mac `pgrep -xq java` before every run (skip the run if the owner's java is up, never print command lines);
GPC woken with gpc-power.sh; PVE `df /` checked before every run (stop above 95 %); fd PRIOR_GAME_NOT_SAVED -> the game dir is moved
aside to game.unsaved-<UTC>-perfsuite (never deleted) and the run retried once; fd GPU_BUSY -> stop that host and report.
--jfr: fd only (attaches JFR with the fd JDK 21 jcmd to the run's java pid, settings=profile, dump on exit -> <archive>/profile.jfr);
       mac passes 'jfr' to mac-harness-large. GPC has no JFR path.
Every invocation is appended to perfsuite/queue-log.jsonl (host, scenario, build, arm, rep, start/end, exit, archive).
"""
import argparse, datetime, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time
from texture_gate import score_archive

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = "/root/minecraft-dev-lab-config/bin"
PIN = "/root/mcdevlab-shared/harness/pinned"
SCN = "/root/mcdevlab-shared/harness/scenarios"
ARCH = "/root/mcdevlab-shared/harness/results-archive"
LOG = os.path.join(HERE, "queue-log.jsonl")
FDSSH = ["pct", "exec", "214", "--", "tailscale", "ssh", "root@orbital-fd"]
MACSSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", "-i", "/root/.ssh/mcdevlab_ed25519", "-o",
          "ProxyCommand=pct exec 214 -- nc -w 25 <tailnet-ip> 22", "user@orbital-mac"]
FD_JCMD = "/home/orbital/android-build/jdk21/bin/jcmd"
RUNNER = {"fd": "fd-harness-large", "gpc": "gpc-harness-large", "mac": "mac-harness-large", "ct224": "ct224-harness-large"}
# Mac fixtures live in pristine dirs; the scenario's large_fixture names the fixture on the other hosts.
MAC_PRISTINE = {"large-gpc-basepack-v1": "pristine-large", "owner-beta-lush-20261114": "pristine-beta-lush-real",
                "owner-lush-20261117-consistent": "pristine-beta-lush-real-consistent"}
with open(os.path.join(HERE, "manifest.json")) as stream:
    MANIFEST = json.load(stream)
MAC_PRISTINE.update({name: fixture["mac_pristine"] for name, fixture in MANIFEST["fixture_registry"].items()
                     if "mac_pristine" in fixture})
lock = threading.Lock()
stop_all = threading.Event()
failed_jobs = threading.Event()


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(*a):
    with lock:
        print(utc(), *a, flush=True)


def logrow(row):
    with lock:
        open(LOG, "a").write(json.dumps(row) + "\n")


def pin(build):
    b = PIN + "/candidate-" + ("0.2.0-beta.1-" + build if re.fullmatch(r"rc\.\d+", build) else build)
    assert "candidate-release-" not in b, "release jars carry no harness steps"
    if not (os.path.isfile(b + ".jar") and os.path.isfile(b + ".json")):
        raise SystemExit("missing pin " + b + ".{jar,json}")
    return b + ".jar", b + ".json", json.load(open(b + ".json"))["label"]


def props_file(arm, scenario=None, host=None):
    with open(os.path.join(HERE, "manifest.json")) as stream:
        man = json.load(stream)
    lines = man["props"][man["arms"][arm]["props"]]
    suffix = ""
    if scenario == "q-texture":
        profile = [line.partition("=")[2].strip() for line in lines if line.partition("=")[0].strip() == "orbitalatlas.playProfile"]
        if any(value != "true" for value in profile):
            raise ValueError("q-texture requires the product playProfile=true")
        if not profile:
            lines = [*lines, "orbitalatlas.playProfile=true"]
        suffix = "-q-texture"
    policy = man.get("representative_stations", {}).get(host, {})
    if policy.get("routes", {}).get("q-basepack" if scenario == "q-basepack-off" else scenario):
        base = {"orbitalatlas.playProfile": "true", "orbitalatlas.diag": "true", "orbitalatlas.mapPerfLog": "true",
                "orbitalatlas.harnessCensusLite": "true", "orbitalatlas.harnessPresentAll": "true",
                "orbitalatlas.sceneMeshBudgetMb": str(policy["budget_mb"]), "orbitalatlas.sceneMeshBudgetAdaptive": "false"}
        # A named historical arm may explicitly override the mesh budget, e.g. cap120 on Mac.
        for line in lines:
            key, value = line.split("=", 1)
            if key in base and key != "orbitalatlas.sceneMeshBudgetMb" and base[key] != value:
                raise ValueError(f"{host}/{scenario} requires {key}={base[key]}")
            base[key] = value
        lines = [f"{key}={value}" for key, value in base.items()]
        suffix = "-" + host + "-paired"
    p = os.path.join(HERE, "props", arm + suffix + ".props")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(p), delete=False) as out:
        out.write("\n".join(lines) + "\n")
    os.replace(out.name, p)
    return p


def verified_recipe(host, archive, route, candidate, props):
    """Check the wrapper's measured input identities, including the restored world/store manifest."""
    with open(os.path.join(archive, "result.json")) as stream:
        result = json.load(stream)
    with open(os.path.join(archive, "meta.json")) as stream:
        meta = json.load(stream)
    with open(candidate) as stream:
        pin_info = json.load(stream)
    with open(props) as stream:
        flags = stream.read().splitlines()
    # Mac's in-game harness uses the generic mac-agent label. Its wrapper hashes the staged jar.
    recorded_jar = result["candidate"].get("jar_sha256")
    checks = {
        "route": result["scenario"]["sha256"] == route["scenario_sha256"],
        "candidate": meta["candidate_sha256"] == pin_info["sha256"] and
                     (recorded_jar == pin_info["sha256"] or (host == "mac" and recorded_jar is None)),
        "label": meta["candidate"] == pin_info["label"],
        "commit": meta["source_commit"] == pin_info["source_commit"],
        "properties": meta["props"].splitlines() == flags,
    }
    fixture = route["fixture"]
    expected = MANIFEST["fixture_registry"][fixture]
    if host == "gpc":
        with open(os.path.join(archive, "prepare.json"), encoding="utf-8-sig") as stream:
            prepared = json.load(stream)
        files = prepared["fixtures"]
        world = next((f for f in files if "world" in f), {})
        atlas = next((f for f in files if "large_fixture" in f), {})
        checks.update({
            "fixture": atlas.get("large_fixture") == fixture and meta["fixture"]["fixture"] == fixture,
            "atlas archive": atlas.get("tar_sha256") == expected["tar_sha256"],
            "world archive": world.get("pinned") is True and world.get("consistent_snapshot") is True
                             and world.get("world_tar_sha256") == expected["world_tar_sha256"],
            "native inputs": prepared["scenario_sha256"] == route["scenario_sha256"]
                             and prepared["candidate_sha256"] == pin_info["sha256"]
                             and prepared["candidate_commit"] == pin_info["source_commit"],
        })
    else:
        receipt = "fixture.json" if host == "mac" else "recipe-receipt.json"
        with open(os.path.join(archive, receipt)) as stream:
            restored = json.load(stream)
        checks.update({"fixture": restored["fixture"] == fixture,
                       "manifest": restored["fixture_manifest_sha256"] == expected["manifest_sha256"]})
    return [key for key, passed in checks.items() if not passed]


def disk_ok():
    st = os.statvfs("/")
    used = 1 - st.f_bavail / st.f_blocks
    return used < 0.95, round(100 * used, 1)


def mac_java_up():
    r = subprocess.run(MACSSH + ["pgrep -xq java"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    if r.returncode not in (0, 1):
        return None   # Mac unreachable
    return r.returncode == 0


def gpc_on():
    t = "/root/atlas-beta-program/tools/gpc-power.sh"
    s = subprocess.run([t, "status"], capture_output=True, text=True, timeout=60).stdout.strip()
    if s != "up":
        say("gpc: power", s, "-> on")
        subprocess.run([t, "on"], capture_output=True, text=True, timeout=900)
    return subprocess.run([t, "status"], capture_output=True, text=True, timeout=60).stdout.strip() == "up"


def fd_move_unsaved():
    # Native marker reconciliation owns this operation under the remote work lock.
    # A controller that already released its lease must never move another run's game.
    return False


def fd_jfr_attach(run_name, done):
    """Poll the fd stage status for the java pid, then start a JFR recording dumped on exit."""
    st = "/opt/mcdevlab/large/stage/%s/status.json" % run_name
    out = "/opt/mcdevlab/large/jfr/%s.jfr" % run_name
    t0 = time.time()
    while not done.is_set() and time.time() - t0 < 900:
        r = subprocess.run(FDSSH + ["cat " + st], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        try:
            s = json.loads(r.stdout)
        except Exception:
            s = {}
        if s.get("status") == "RUNNING" and s.get("java_pid"):
            pid = int(s["java_pid"])
            for _ in range(30):
                r = subprocess.run(FDSSH + ["%s %d JFR.start name=perfsuite settings=profile dumponexit=true filename=%s >/dev/null 2>&1 && echo ok" % (FD_JCMD, pid, out)],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
                if "ok" in r.stdout:
                    say("fd: JFR started for", run_name)
                    return out
                time.sleep(5)
            say("fd: JFR attach failed for", run_name)
            return None
        time.sleep(5)
    return None


GPCSSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20", "user@<lan-ip>"]
GPC_JCMD = r"C:\Program Files\Eclipse Adoptium\jdk-17.0.11.9-hotspot\bin\jcmd.exe"


def gpc_jfr_attach(run_name, done):
    """Find the harness clone's javaw (filtered on the throwaway instance name inside PowerShell; the command line is never
    printed), then JFR.start with the GPC JDK 17 jcmd (attach works against the 21 JVM)."""
    out = "C:\\mcdevlab\\jfr\\%s.jfr" % run_name
    ps = ("$p=(Get-CimInstance Win32_Process -Filter \"name='javaw.exe' or name='java.exe'\" | Where-Object { $_.CommandLine -like '*McDevLab-AtlasHarness*' } "
          "| Select-Object -First 1).ProcessId; if ($p) { New-Item -ItemType Directory -Force C:\\mcdevlab\\jfr | Out-Null; "
          "$r = & '%s' $p JFR.start name=perfsuite settings=profile dumponexit=true filename=%s 2>&1 | Out-String; "
          "if ($r -match 'JFR.dump') { 'ok' } else { 'fail' } } else { 'nopid' }") % (GPC_JCMD, out)
    t0 = time.time()
    time.sleep(90)
    while not done.is_set() and time.time() - t0 < 900:
        r = subprocess.run(GPCSSH + ['powershell -NoProfile -Command "%s"' % ps.replace('"', '\\"')], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        o = r.stdout.strip()
        if o.endswith("ok"):
            say("gpc: JFR started for", run_name)
            return "C:/mcdevlab/jfr/%s.jfr" % run_name
        if o.endswith("fail"):
            say("gpc: JFR attach failed for", run_name)
            return None
        time.sleep(15)
    return None


def run_one(host, scen, build, arm, rep, jfr, dry):
    if arm == "off" and scen == "q-basepack":
        scen = "q-basepack-off"   # the map steps need the map; the off twin keeps route and measure labels
    sc = "%s/atlas-ps-%s-%s.json" % (SCN, scen, host)
    jar, js, label = pin(build)
    props = props_file(arm, scen, host)
    armname = ("%s-%s-%s-r%d" % (arm, build.replace("rc.", "rc"), "j" if jfr else "n", rep))[:40]
    env = dict(os.environ, CANDIDATE_JAR=jar, CANDIDATE_JSON=js, EXPECT_LABEL=label)
    cmd = [BIN + "/" + RUNNER[host], sc, armname, props]
    with open(sc) as stream:
        fixture = json.load(stream).get("large_fixture")
    host_routes = MANIFEST["representative_stations"].get(host, {}).get("routes", {})
    route = host_routes.get(scen)
    if route:
        with open(sc, "rb") as stream:
            if fixture != route["fixture"] or hashlib.file_digest(stream, "sha256").hexdigest() != route["scenario_sha256"]:
                raise ValueError(f"{scen}/{host} differs from its frozen input; regenerate with gen.py")
    if host in ("fd", "ct224"):
        explicit_fixture = route or (scen == "q-basepack-off" and "q-basepack" in host_routes)
        cmd.append(fixture if explicit_fixture and host == "fd" else "-")
    elif host == "mac":
        env["FIXTURE"] = fixture
        env["PRISTINE_DIR"] = MAC_PRISTINE[fixture]
        cmd.append("jfr" if jfr else "-")
    else:
        cmd.append("-")   # gpc: the wrapper has no JFR option; --jfr attaches with jcmd (gpc_jfr_attach)
    row = {"host": host, "scenario": scen, "build": build, "label": label, "arm": arm, "rep": rep, "jfr": jfr, "armname": armname,
           "start": utc(), "cmd": " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd))}
    if dry:
        say("DRY", host, row["cmd"])
        return 0
    ok, pct = disk_ok()
    if not ok:
        say("STOP: PVE / at %s%%" % pct)
        stop_all.set()
        row.update(end=utc(), exit="DISK", archive=None)
        logrow(row)
        return 9
    if host == "mac":
        j = mac_java_up()
        if j is not False:
            say("mac: skip (java up or Mac unreachable: %s)" % j)
            row.update(end=utc(), exit="MAC_JAVA_UP" if j else "MAC_UNREACHABLE", archive=None)
            logrow(row)
            return 8
    if host == "gpc" and not gpc_on():
        row.update(end=utc(), exit="GPC_DOWN", archive=None)
        logrow(row)
        return 7
    for attempt in (1, 2):
        say(host, "start", scen, build, arm, "rep", rep, "attempt", attempt)
        p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        done = threading.Event()
        runname, out_lines, jt = None, [], None
        for line in p.stdout:
            out_lines.append(line.rstrip()[:400])
            m = re.search(r'"run": "([^"]+)"', line)
            if m and not runname:
                runname = m.group(1)
                if jfr and host == "fd":
                    jt = threading.Thread(target=lambda: row.__setitem__("jfr_remote", fd_jfr_attach(runname, done)), daemon=True)
                    jt.start()
            m2 = re.search(r"gpc-harness: start (\S+) ->", line)
            if m2 and not runname:
                runname = m2.group(1)
                if jfr:
                    jt = threading.Thread(target=lambda: row.__setitem__("jfr_remote", gpc_jfr_attach(runname, done)), daemon=True)
                    jt.start()
        rc = p.wait()
        done.set()
        tail = "\n".join(out_lines[-4:])
        archive = None
        m = re.findall(r"(/root/mcdevlab-shared/harness/results-archive/\S+?)(?:\"|\s|$)", tail)
        if m:
            archive = m[-1].rstrip("\"}")
        if host == "fd" and "GPU_BUSY" in tail:
            say("fd: GPU_BUSY (llama-server?) -> stopping the fd queue")
            row.update(end=utc(), exit="GPU_BUSY", archive=archive, tail=tail)
            logrow(row)
            return 6
        break
    if jfr and host == "fd" and runname and row.get("jfr_remote") and archive and os.path.isdir(archive):
        r = subprocess.run(FDSSH + ["cat " + row["jfr_remote"]], stdin=subprocess.DEVNULL, stdout=open(os.path.join(archive, "profile.jfr"), "wb"), timeout=600)
        row["jfr_copied"] = r.returncode == 0
    if jfr and host == "gpc" and row.get("jfr_remote") and archive and os.path.isdir(archive):
        time.sleep(10)
        r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "user@<lan-ip>:" + row["jfr_remote"],
                            os.path.join(archive, "profile.jfr")], capture_output=True, timeout=600)
        row["jfr_copied"] = r.returncode == 0
    if route and rc == 0:
        try:
            failures = verified_recipe(host, archive, route, js, props)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            failures = [f"missing or unreadable provenance ({type(exc).__name__})"]
        row["recipe_errors"] = failures
        if failures:
            say(host, "recipe identity failed", ", ".join(failures))
            rc = 1
    if scen == "q-texture":
        try:
            card = score_archive(archive, write=True) if archive else None
        except (OSError, ValueError, TypeError):
            card = None
        if card is None:
            card = {"verdict": "FAIL", "reasons": ["MISSING_TEXTURE_ARCHIVE"]}
        row["texture_scorecard"] = card
        reasons = "; ".join(card["reasons"][:3])
        if len(card["reasons"]) > 3:
            reasons += "; +%d reasons (see texture-scorecard.json)" % (len(card["reasons"]) - 3)
        say(host, "texture", card["verdict"], "first_ms", card.get("first_textured_ms"), reasons)
        if card["verdict"] != "PASS" and rc == 0:
            rc = 1
    row.update(end=utc(), exit=rc, archive=archive, run=runname, tail=tail[-600:])
    logrow(row)
    say(host, "done", scen, build, arm, "rc", rc, archive)
    return rc


def host_queue(host, jobs, dry):
    for j in jobs:
        if stop_all.is_set():
            break
        try:
            rc = run_one(host, *j, dry=dry)
        except Exception as exc:
            # A scoring/collection failure must not turn into a successful
            # process exit just because it happened on a worker thread.
            failed_jobs.set()
            say(host, "FAIL", type(exc).__name__)
            break
        if rc != 0:
            failed_jobs.set()
        if rc in (6, 7, 9):
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--builds", required=True)
    ap.add_argument("--hosts", default="fd,gpc,mac")
    ap.add_argument("--set", default=None, help="quick | nightly (nightly = quick + n-*)")
    ap.add_argument("--scenarios", default=None)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--rep-start", type=int, default=1)
    flags = ap.add_mutually_exclusive_group()
    flags.add_argument("--arm", default="base")
    flags.add_argument("--arms", help="one arm per --builds entry; compare against the first control in alternating order")
    ap.add_argument("--jfr", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    with open(os.path.join(HERE, "manifest.json")) as stream:
        man = json.load(stream)
    if a.scenarios:
        scens = a.scenarios.split(",")
    else:
        sets = {"quick": ("quick",), "nightly": ("quick", "nightly")}[a.set or "quick"]
        scens = [k for k, v in man["scenarios"].items() if v["set"] in sets]
    builds = a.builds.split(",")
    for b in builds:
        pin(b)
    arms = a.arms.split(",") if a.arms else [a.arm] * len(builds)
    if len(arms) != len(builds) or set(arms).difference(man["arms"]):
        ap.error("specify a known arm for each build")
    targets = list(zip(builds, arms))
    threads = []
    for host in a.hosts.split(","):
        jobs = []
        for rep in range(a.rep_start, a.rep_start + a.reps):
            if a.arms and len(targets) > 1:
                # AB/BA for one treatment; ABC/CBA/ACB/BCA for two, yielding ABBAABBA per comparison.
                others = targets[1:]
                k = ((rep - 1) // 2) % len(others)
                order = targets[:1] + others[k:] + others[:k]
                if rep % 2 == 0:
                    order.reverse()
            else:
                k = (rep - 1) % len(targets)
                order = targets[k:] + targets[:k]
            if a.arms:
                jobs.extend((s, b, arm, rep, a.jfr) for b, arm in order for s in scens)
            else:
                jobs.extend((s, b, arm, rep, a.jfr) for s in scens for b, arm in order)
        say(host, "queue:", len(jobs), "runs")
        t = threading.Thread(target=host_queue, args=(host, jobs, a.dry))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return 1 if failed_jobs.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
