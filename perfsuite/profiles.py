#!/usr/bin/env python3
"""JFR hot-method summaries for perf-suite runs that carry profile.jfr.

  profiles.py [--out /root/atlas-beta-program/perf-dossier/profiles] [run_dir ...]

Default: every results-archive/<host>/*atlas-ps-*/profile.jfr. Per run writes <out>/<host>-<scenario>-<build>-<run_id>.txt:
  1. thread census (jdk.ExecutionSample count per thread, and how many of those samples have an Atlas frame on the stack)
  2. jfr-hot.py for the render thread ("Render thread"; "main" on macOS) and every thread named *OrbitalAtlas*/*Atlas*
and appends a row to <out>/index.jsonl. Sampling: JFR 'profile' settings (Java samples every 10 ms per thread, native excluded),
so counts are CPU-time proxies for Java code only; GPU time and driver time inside native GL calls show as the GL leaf frame.
"""
import argparse, collections, glob, json, os, re, subprocess, sys

ARCH = "/root/mcdevlab-shared/harness/results-archive"
HOT = "/root/minecraft-dev-lab-config/bin/jfr-hot.py"
PREFIX = "io.github.largeorbitalobject"


sys.path.insert(0, "/root/minecraft-dev-lab-config/bin")
import importlib.util, datetime
_spec = importlib.util.spec_from_file_location("jfr_hot", HOT); jfr_hot = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(jfr_hot)


def window(d):
    """Steady-state window: first measure step start -> result finished (excludes boot and world load)."""
    try:
        r = json.load(open(os.path.join(d, "result.json")))
        t0 = datetime.datetime.fromisoformat(r["started_utc"].replace("Z", "+00:00"))
        ms = [s["started_ms"] for s in r["steps"] if s.get("type") == "measure" and s.get("started_ms") is not None]
        if not ms:
            return None, None
        since = t0 + datetime.timedelta(milliseconds=ms[0])
        until = datetime.datetime.fromisoformat(r["finished_utc"].replace("Z", "+00:00")) if r.get("finished_utc") else None
        return since, until
    except Exception:
        return None, None


def phases(d):
    """Per-phase windows (UTC) from the measure steps: q-basepack world-map sweep, flight, walk, cave spans."""
    try:
        r = json.load(open(os.path.join(d, "result.json")))
        t0 = datetime.datetime.fromisoformat(r["started_utc"].replace("Z", "+00:00"))
    except Exception:
        return {}
    groups = {"worldmap sweep (wm1/wm2/spin)": r"^(wm1_|wm2_|wm_spin)", "flight 30 b/s (fly_*)": r"^fly_", "walk (walk_*)": r"^walk_",
              "cave minimap (mm_cave_*)": r"^mm_cave_", "cave world map (wm_cave_*)": r"^wm_cave_"}
    out = {}
    for g, rx in groups.items():
        ws = [(s["started_ms"], s["started_ms"] + (s.get("duration_ms") or 0)) for s in r["steps"]
              if s.get("type") == "measure" and re.search(rx, (s.get("args") or {}).get("label") or "")]
        if ws:
            out[g] = (t0 + datetime.timedelta(milliseconds=min(a for a, _ in ws)), t0 + datetime.timedelta(milliseconds=max(b for _, b in ws)))
    return out


UNITS = {"bytes": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def alloc(jfr, since=None, until=None, top=25):
    """jdk.ObjectAllocationSample weights: per thread, share with an Atlas frame, top Atlas sites (deepest mod frame) and classes."""
    p = subprocess.Popen(["jfr", "print", "--events", "jdk.ObjectAllocationSample", "--stack-depth", "64", jfr], stdout=subprocess.PIPE,
                         text=True, bufsize=1 << 20)
    th, th_mod, site, cls = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    seen = set()   # the first sample per thread carries everything allocated since thread start (before JFR): skip it
    ev = None
    for line in p.stdout:
        s = line.strip()
        if s.startswith("jdk.ObjectAllocationSample"):
            ev = {"w": 0.0, "t": None, "thread": None, "cls": None, "top": None}
        elif ev is None:
            continue
        elif s.startswith("startTime ="):
            ev["t"] = s.split("=", 1)[1].strip()
        elif s.startswith("objectClass ="):
            ev["cls"] = s.split("=", 1)[1].split(" (")[0].strip()
        elif s.startswith("weight ="):
            v = s.split("=", 1)[1].strip().split()
            try:
                ev["w"] = float(v[0].replace(",", "")) * UNITS.get(v[1], 1)
            except Exception:
                pass
        elif s.startswith("eventThread ="):
            m = re.search(r'"([^"]*)"', s); ev["thread"] = m.group(1) if m else s
        elif PREFIX in s and ev["top"] is None:
            ev["top"] = re.sub(r"\s+line:.*$", "", s)
        elif s == "}":
            first = ev["thread"] not in seen
            seen.add(ev["thread"])
            if ev["thread"] is not None and not first and (not (since or until) or jfr_hot.within(ev["t"], since, until)):
                th[ev["thread"]] += ev["w"]
                if ev["top"]:
                    th_mod[ev["thread"]] += ev["w"]; site[ev["top"]] += ev["w"]; cls[ev["cls"]] += ev["w"]
            ev = None
    p.wait()
    tot = sum(th.values()) or 1
    out = ["## Allocation (jdk.ObjectAllocationSample weight = estimated bytes allocated; first sample per thread dropped)", "",
           "total %.1f GB sampled-weight; with an Atlas frame on the stack %.1f GB (%.1f%%)" % (tot / 1e9, sum(th_mod.values()) / 1e9, 100 * sum(th_mod.values()) / tot), "",
           "### per thread (GB, Atlas-on-stack GB)", ""]
    for t, w in th.most_common(15):
        out.append("%8.2f GB  atlas %8.2f GB  %s" % (w / 1e9, th_mod[t] / 1e9, t))
    out += ["", "### Atlas allocation sites (deepest-first Atlas frame), GB", ""]
    for f, w in site.most_common(top):
        out.append("%8.2f GB  %5.1f%%  %s" % (w / 1e9, 100 * w / tot, f[:170]))
    out += ["", "### classes allocated under Atlas frames, GB", ""]
    for c, w in cls.most_common(15):
        out.append("%8.2f GB  %s" % (w / 1e9, c))
    return out, {"alloc_total_gb": round(tot / 1e9, 2), "alloc_atlas_gb": round(sum(th_mod.values()) / 1e9, 2),
                 "alloc_atlas_pct": round(100 * sum(th_mod.values()) / tot, 1)}


def gcsum(jfr, since=None, until=None):
    """jdk.GarbageCollection: count / total pause / max pause per (collector, cause); jdk.GCHeapSummary max heap used."""
    p = subprocess.run(["jfr", "print", "--events", "jdk.GarbageCollection", jfr], capture_output=True, text=True, timeout=900)
    ev, cur = [], {}
    for l in p.stdout.splitlines():
        l = l.strip()
        m = re.match(r"(name|cause|sumOfPauses|longestPause|startTime) = (.*)", l)
        if m:
            cur[m.group(1)] = m.group(2)
        if l == "}" and cur:
            ev.append(cur); cur = {}
    def ms(x):
        v, u = x.split()[:2]; v = float(v.replace(",", ""))
        return v * 1000 if u == "s" else v / 1000 if u == "us" else v
    by = collections.defaultdict(lambda: [0, 0.0, 0.0])
    n = 0
    for e in ev:
        if (since or until) and not jfr_hot.within(e.get("startTime"), since, until):
            continue
        try:
            pz = ms(e["sumOfPauses"])
        except Exception:
            continue
        k = (e.get("name", "").strip('"'), e.get("cause", "").strip('"')); by[k][0] += 1; by[k][1] += pz; by[k][2] = max(by[k][2], pz); n += 1
    out = ["## GC (jdk.GarbageCollection in the window)", "", "%d collections, %.0f ms total pause" % (n, sum(v[1] for v in by.values())), ""]
    for k, v in sorted(by.items(), key=lambda x: -x[1][1]):
        out.append("%-8s %-28s n=%4d  total %7.0f ms  max %6.1f ms" % (k[0], k[1], v[0], v[1], v[2]))
    return out, {"gc_n": n, "gc_pause_ms": round(sum(v[1] for v in by.values())), "gc_full_n": sum(v[0] for k, v in by.items() if "Full" in k[0]),
                 "gc_humongous_n": sum(v[0] for k, v in by.items() if "Humongous" in k[1])}


def native(jfr, thread, since=None, until=None, top=20):
    """jdk.NativeMethodSample on one thread (time blocked/running in native code, e.g. GL driver calls): top leaf frames and the
    deepest Atlas frame above them. JFR 'profile' samples native threads every 20 ms."""
    p = subprocess.Popen(["jfr", "print", "--events", "jdk.NativeMethodSample", "--stack-depth", "64", jfr], stdout=subprocess.PIPE, text=True, bufsize=1 << 20)
    leaf, mod = collections.Counter(), collections.Counter()
    ev = None; frames = []; n = 0
    for line in p.stdout:
        s_ = line.strip()
        if s_.startswith("jdk.NativeMethodSample"):
            ev = {"t": None, "th": None}; frames = []
        elif ev is None:
            continue
        elif s_.startswith("startTime ="):
            ev["t"] = s_.split("=", 1)[1].strip()
        elif s_.startswith("sampledThread ="):
            m = re.search(r'"([^"]*)"', s_); ev["th"] = m.group(1) if m else s_
        elif s_ == "}":
            if ev["th"] == thread and frames and (not (since or until) or jfr_hot.within(ev["t"], since, until)):
                n += 1; leaf[frames[0]] += 1
                tm = next((f for f in frames if f.startswith(PREFIX)), None)
                if tm: mod[tm] += 1
            ev = None
        elif "(" in s_ and (s_.endswith(")") or "line:" in s_):
            frames.append(re.sub(r"\s+line:.*$", "", s_))
    p.wait()
    out = ["## Native samples on %r (jdk.NativeMethodSample): %d" % (thread, n), "", "### leaf (native) frames", ""]
    for f, k in leaf.most_common(top):
        out.append("%5.1f%%  %6d  %s" % (100.0 * k / max(n, 1), k, f[:170]))
    out += ["", "### deepest Atlas frame above the native call", ""]
    for f, k in mod.most_common(top):
        out.append("%5.1f%%  %6d  %s" % (100.0 * k / max(n, 1), k, f[:170]))
    return out, {"render_native_samples": n, "render_native_atlas_samples": sum(mod.values())}


def census(jfr, since=None, until=None):
    p = subprocess.Popen(["jfr", "-J-Duser.timezone=UTC", "print", "--events", "jdk.ExecutionSample", "--stack-depth", "96", jfr], stdout=subprocess.PIPE, text=True,
                         bufsize=1 << 20, env=dict(os.environ, TZ="UTC"))
    tot, mod = collections.Counter(), collections.Counter()
    thread, has, ts = None, False, None
    for line in p.stdout:
        s = line.strip()
        if s.startswith("jdk.ExecutionSample"):
            thread, has, ts = None, False, None
        elif s.startswith("startTime ="):
            ts = s.split("=", 1)[1].strip()
        elif s.startswith("sampledThread ="):
            m = re.search(r'"([^"]*)"', s); thread = m.group(1) if m else s
        elif PREFIX in s:
            has = True
        elif s == "}" and thread is not None:
            if (since or until) and not jfr_hot.within(ts, since, until):
                thread = None
                continue
            tot[thread] += 1
            if has:
                mod[thread] += 1
            thread = None
    p.wait()
    return tot, mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/atlas-beta-program/perf-dossier/profiles")
    ap.add_argument("runs", nargs="*")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    runs = a.runs or sorted(os.path.dirname(p) for p in glob.glob(os.path.join(ARCH, "*", "*atlas-ps-*", "profile.jfr")))
    idx = open(os.path.join(a.out, "index.jsonl"), "a")
    for d in runs:
        jfr = os.path.join(d, "profile.jfr")
        if not os.path.isfile(jfr) or os.path.getsize(jfr) < 1024:
            continue
        host = os.path.basename(os.path.dirname(d)); rid = os.path.basename(d)
        try:
            meta = json.load(open(os.path.join(d, "meta.json")))
        except Exception:
            meta = {}
        sc = re.sub(r"^\d+T\d+Z-atlas-ps-|-(fd|gpc|mac|ct224)-.*$", "", rid)
        build = (meta.get("candidate") or "?").replace("0.2.0-beta.1-", "")
        name = "%s-%s-%s-%s.txt" % (host, sc, build, rid)
        since, until = window(d)
        # The jfr tool prints startTime in the JVM default zone (this host: America/Los_Angeles; TZ and -Duser.timezone are
        # ignored by the launcher), so compare against the window shifted into local wall time.
        import time as _t
        off = datetime.timedelta(seconds=_t.localtime().tm_gmtoff)
        lsince, luntil = (since + off if since else None), (until + off if until else None)
        tot, mod = census(jfr, lsince, luntil)
        row_phases = {}
        render = "Render thread" if "Render thread" in tot else "main"
        lines = ["# JFR profile summary: %s" % rid, "archive: %s" % d, "host=%s scenario=%s build=%s arm=%s" % (host, sc, build, meta.get("arm")),
                 "JFR settings=profile (Java execution samples ~10 ms per running thread).",
                 "Window: %s .. %s UTC (first measure step -> end of run; boot and world load excluded)." % (since, until), "",
                 "## Thread census (samples, samples with an Atlas frame on the stack)", ""]
        allsum = sum(tot.values())
        for t, n in tot.most_common(40):
            lines.append("%7d  %5.1f%%  atlas-on-stack %6d (%5.1f%% of thread)  %s" % (n, 100.0 * n / allsum, mod[t], 100.0 * mod[t] / n if n else 0, t))
        lines.append("")
        atlas_threads = [t for t in tot if "atlas" in t.lower()]
        for t in [render] + sorted(atlas_threads, key=lambda t: -tot[t])[:4]:   # top 4 Atlas threads by samples (time budget)
            win = (["--since-utc", lsince.isoformat(), "--until-utc", luntil.isoformat()] if since and until else [])
            r = subprocess.run([sys.executable, HOT, jfr, "--thread", t, "--top", "25"] + win, capture_output=True, text=True, timeout=1800)
            lines += ["## jfr-hot.py --thread %r" % t, "", r.stdout.strip(), ""]
        al, alrow = alloc(jfr, lsince, luntil)
        gl, glrow = gcsum(jfr, lsince, luntil)
        alrow.update(glrow)
        nl, nlrow = native(jfr, render, lsince, luntil)
        alrow.update(nlrow)
        lines += gl + [""] + nl + [""] + al + [""]
        for g, (ps_, pu_) in phases(d).items():
            if ps_ < since or (until and pu_ > until + datetime.timedelta(seconds=5)):
                continue   # JFR attached later than this phase started
            ptot, pmod = census(jfr, ps_ + off, pu_ + off)
            prender = ptot.get(render, 0)
            lines += ["## Phase: %s  (%s .. %s UTC)" % (g, ps_.strftime("%H:%M:%S"), pu_.strftime("%H:%M:%S")), "",
                      "render thread samples %d, with Atlas on stack %d (%.1f%%); all-thread samples with Atlas on stack %d" % (
                          prender, pmod.get(render, 0), 100.0 * pmod.get(render, 0) / prender if prender else 0, sum(pmod.values())), ""]
            for t in [render] + sorted([t for t in ptot if "atlas" in t.lower()], key=lambda t: -ptot[t])[:1]:
                r = subprocess.run([sys.executable, HOT, jfr, "--thread", t, "--top", "20", "--since-utc", (ps_ + off).isoformat(),
                                    "--until-utc", (pu_ + off).isoformat()], capture_output=True, text=True, timeout=1800)
                lines += ["### %s: jfr-hot.py --thread %r" % (g, t), "", r.stdout.strip(), ""]
            row_ph = row_phases.setdefault(g, {"render_samples": prender, "render_atlas_samples": pmod.get(render, 0),
                                               "render_atlas_pct": round(100.0 * pmod.get(render, 0) / prender, 1) if prender else None,
                                               "atlas_threads": {t: ptot[t] for t in ptot if "atlas" in t.lower()}})
        open(os.path.join(a.out, name), "w").write("\n".join(lines) + "\n")
        row = {"run_id": rid, "host": host, "scenario": sc, "build": build, "file": name, "render_thread": render,
               "render_samples": tot[render], "render_atlas_samples": mod[render],
               "render_atlas_pct": round(100.0 * mod[render] / tot[render], 1) if tot[render] else None,
               "atlas_threads": {t: tot[t] for t in atlas_threads}, "total_samples": allsum,
               "atlas_on_stack_all_threads": sum(mod.values()), "window": [str(since), str(until)], "phases": row_phases, **alrow}
        idx.write(json.dumps(row) + "\n")
        print(name, json.dumps(row)[:300])


def alloc_only(d, out_dir):
    """Recompute only the allocation section for an already summarised run (appends a corrected section, updates index row)."""
    import time as _t
    since, until = window(d)
    off = datetime.timedelta(seconds=_t.localtime().tm_gmtoff)
    al, row = alloc(os.path.join(d, "profile.jfr"), since + off if since else None, until + off if until else None)
    rid = os.path.basename(d)
    for f in glob.glob(os.path.join(out_dir, "*" + rid + ".txt")):
        txt = open(f).read()
        i = txt.find("## Allocation")
        j = txt.find("\n## ", i + 5)
        txt = txt[:i] + "\n".join(al) + "\n" + (txt[j:] if j > 0 else "")
        open(f, "w").write(txt)
    idx = os.path.join(out_dir, "index.jsonl")
    rows = [json.loads(l) for l in open(idx)]
    for r in rows:
        if r["run_id"] == rid:
            r.update(row)
    open(idx, "w").write("".join(json.dumps(r) + "\n" for r in rows))
    print(rid, row)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--alloc-only":
        for d in sys.argv[2:]:
            alloc_only(d, "/root/atlas-beta-program/perf-dossier/profiles")
        sys.exit(0)
    main()
