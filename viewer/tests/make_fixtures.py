"""Synthetic fixtures built from HARNESS-RESULT-SCHEMA.md, until the real harness writes its own.

  python3 tests/make_fixtures.py DIR      writes DIR/harness/test-runs/<run>/{result.json,progress.jsonl,harness.log,
                                          client.log, screenshots/*.png} for a set of runs that exercise baseline,
                                          regression, environment mismatch, noise, crash and a run in flight.
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from mcdevlab_viewer import thumbs  # noqa: E402

T0 = 1791200000.0  # fixed epoch so tests are deterministic
SCEN = {"name": "flyover-spawn", "sha256": "5c0e" * 16, "path": "/scenarios/flyover-spawn.json"}
CAND_A = {"label": "aaaa1111", "commit": "aaaa1111" + "0" * 32, "jar_sha256": "a1" * 32, "mod_id": "examplemod"}
CAND_B = {"label": "bbbb2222", "commit": "bbbb2222" + "0" * 32, "jar_sha256": "b2" * 32, "mod_id": "examplemod"}
CAND_C = {"label": "cccc3333", "commit": "cccc3333" + "0" * 32, "jar_sha256": "c3" * 32, "mod_id": "examplemod"}
MODS = [{"id": "minecraft", "version": "1.21.1", "file": "client.jar"}, {"id": "neoforge", "version": "21.1.248", "file": "nf.jar"},
        {"id": "sodium", "version": "0.6.0", "file": "sodium.jar"}, {"id": "examplemod", "version": "1.0.0", "file": "examplemod.jar"}]
NVIDIA = "NVIDIA GeForce RTX 4060 Ti/PCIe/SSE2"
AMD = "AMD Radeon Graphics (radeonsi, gfx1151, LLVM 20.1.8, DRM 3.64, 7.0.0-34-generic)"


def png(w=64, h=40, seed=0):
    rows = []
    for y in range(h):
        rows.append(bytearray(b"".join(bytes(((x * 4 + seed * 40) % 256, (y * 6) % 256, 120)) for x in range(w))))
    return thumbs._encode_png(w, h, 3, rows)


def frames(n, p50, spread, seed):
    rnd = random.Random(seed)
    return [round(max(1.0, rnd.gauss(p50, spread)), 3) for _ in range(n)]


def write_run(root, name, host="ctl", cand=CAND_A, renderer=NVIDIA, p95=24.0, fps=58.0, verdict="PASS", reason=None, start=T0,
              seed=1, mods=None, heap="6G", window=(1280, 800), log_extra=(), crash=None, scenario=SCEN, hitches=3, gl_version="4.6.0 NVIDIA 610.57.04"):
    d = os.path.join(root, "harness", "test-runs", name)
    os.makedirs(os.path.join(d, "screenshots"), exist_ok=True)
    fr = frames(240, p95 * 0.6, 3, seed)
    steps = [{"index": 0, "type": "wait_for_title", "args": {}, "status": "PASS", "started_ms": 0, "duration_ms": 41000, "message": "title screen"},
             {"index": 1, "type": "open_world", "args": {"world": "flat"}, "status": "PASS", "started_ms": 41000, "duration_ms": 18000, "message": "world loaded"},
             {"index": 2, "type": "measure", "args": {"label": "spawn", "seconds": 20}, "status": "PASS" if verdict != "FAIL" else "FAIL",
              "started_ms": 59000, "duration_ms": 20000, "message": reason or "measured 20 s"},
             {"index": 3, "type": "screenshot", "args": {"name": "spawn"}, "status": "PASS" if verdict == "PASS" else "SKIP", "started_ms": 79000, "duration_ms": 400, "message": ""}]
    metrics = [{"name": "frame_ms_p50", "segment": "spawn", "unit": "ms", "value": round(p95 * 0.55, 3), "better": "lower"},
               {"name": "frame_ms_p95", "segment": "spawn", "unit": "ms", "value": p95, "better": "lower"},
               {"name": "frame_ms_p99", "segment": "spawn", "unit": "ms", "value": round(p95 * 1.4, 3), "better": "lower"},
               {"name": "fps_avg", "segment": "spawn", "unit": "fps", "value": fps, "better": "higher"},
               {"name": "hitches_50ms", "segment": "spawn", "unit": "", "value": hitches, "better": "lower"},
               {"name": "heap_used_mb_max", "segment": "spawn", "unit": "MB", "value": 2900 + seed, "better": "lower"},
               {"name": "examplemod.chunks_meshed", "segment": "spawn", "unit": "", "value": 400 + seed, "better": None}]
    res = {
        "schema": "mcdevlab.harness.result.v1", "verdict": verdict, "reason": reason, "scenario": scenario, "candidate": cand, "host": host,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)), "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + 80)),
        "duration_ms": 80000,
        "environment": {"minecraft": "1.21.1", "loader": "neoforge", "loader_version": "21.1.248", "java": "21.0.12", "os": "Linux amd64 7.0.14",
                        "cpu": "test cpu", "cpu_threads": 16, "max_memory_mb": 6144, "gl_renderer": renderer, "gl_version": gl_version,
                        "gl_vendor": "vendor", "window": list(window), "max_heap": heap, "mods": mods or MODS},
        "steps": steps, "metrics": metrics, "series": {"spawn": {"frame_ms": fr, "interval_ms": 83}},
        "screenshots": [{"name": "spawn", "file": "screenshots/spawn.png", "step": 3, "width": 64, "height": 40},
                        {"name": "hud", "file": "screenshots/hud.png", "step": 3, "width": 64, "height": 40}],
        "log_markers": [{"step": 2, "pattern": "Done", "matched": True, "line": "Done (3.2s)!"}],
        "exit": {"graceful": verdict != "ERROR", "saved": True, "method": "save_and_quit"},
    }
    with open(os.path.join(d, "result.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    with open(os.path.join(d, "progress.jsonl"), "w") as fh:
        for i in range(80):
            fh.write(json.dumps({"t_ms": i * 1000, "utc": "x", "step": min(3, i // 25), "step_type": "measure", "phase": "MEASURE" if i > 59 else "WORLD",
                                 "fps": fps + (i % 5), "frame_ms_p50": p95 * 0.55, "frame_ms_p95": p95 + (i % 7) * 0.2, "frame_ms_max": p95 * 2,
                                 "tick_ms_p50": None, "heap_used_mb": 2000 + i * 8, "heap_max_mb": 6144, "chunks_rendered": 100 + i, "measure_label": "spawn"}) + "\n")
    for nm, sd in (("spawn", seed), ("hud", seed + 1)):
        with open(os.path.join(d, "screenshots", nm + ".png"), "wb") as fh:
            fh.write(png(seed=sd))
    log = ["[00:00:01.000] [main/INFO] [cpw.mods.modlauncher.Launcher]: ModLauncher running: args [--width, %d, --height, %d, --fml.mcVersion, 1.21.1]" % window,
           "[00:00:02.000] [Render thread/INFO] [mojang/Window]: OpenGL Renderer: %s" % renderer,
           "[00:00:03.000] [Render thread/WARN] [examplemod/]: Chunk 12 at 0x7fa3b2c1d4e8 took 51 ms to mesh",
           "[00:00:04.000] [Render thread/WARN] [examplemod/]: Chunk 14 at 0x7fa3b2c1d9f0 took 63 ms to mesh",
           "[00:00:05.000] [Render thread/ERROR] [minecraft/RecipeManager]: Parsing error loading recipe examplemod:old_%d" % seed,
           "[00:00:06.000] [Render thread/INFO] [examplemod/]: Meshed 400 chunks"] + list(log_extra)
    if crash:
        log += ["[00:01:20.000] [Render thread/ERROR] [minecraft/Minecraft]: Unhandled game exception"] + crash
    with open(os.path.join(d, "client.log"), "w") as fh:
        fh.write("\n".join(log) + "\n")
    with open(os.path.join(d, "harness.log"), "w") as fh:
        fh.write("harness started\n")
    os.utime(os.path.join(d, "result.json"), (start + 80, start + 80))
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            os.utime(p, (start + 80, start + 80))
    for f in os.listdir(os.path.join(d, "screenshots")):
        os.utime(os.path.join(d, "screenshots", f), (start + 80, start + 80))
    return d


def write_running(root, name, now=None, host="ctl", cand=CAND_B):
    d = os.path.join(root, "harness", "test-runs", name)
    os.makedirs(d, exist_ok=True)
    now = now or time.time()
    with open(os.path.join(d, "progress.jsonl"), "w") as fh:
        for i in range(30):
            fh.write(json.dumps({"t_ms": i * 1000, "utc": "x", "step": 1, "step_type": "measure", "phase": "MEASURE", "fps": 55 + i % 4,
                                 "frame_ms_p50": 12, "frame_ms_p95": 20 + i % 3, "frame_ms_max": 40, "tick_ms_p50": None,
                                 "heap_used_mb": 2100 + i * 5, "heap_max_mb": 6144, "chunks_rendered": 200, "measure_label": "spawn"}) + "\n")
    with open(os.path.join(d, "client.log"), "w") as fh:
        fh.write("[00:00:01.000] [main/INFO] [x/]: started\n[00:00:02.000] [Render thread/WARN] [examplemod/]: live warning 1\n")
    return d


def build(root):
    """The standard scenario: A baseline x3 (noise), B regresses frame p95 on the same machine, C ran on another GPU,
    D fails with a crash."""
    for i, p in enumerate((24.0, 24.6, 23.5)):
        write_run(root, "run-a-%d" % i, cand=CAND_A, p95=p, fps=58 - i * 0.4, start=T0 + i * 600, seed=i + 1)
    write_run(root, "run-b-0", cand=CAND_B, p95=31.0, fps=44.0, start=T0 + 3000, seed=7, hitches=11,
              log_extra=["[00:00:07.000] [Render thread/ERROR] [examplemod/]: Mesh upload failed for chunk 77 at 0x7fa3b2c1ffff"])
    write_run(root, "run-c-0", cand=CAND_C, renderer=AMD, p95=18.0, fps=66.0, start=T0 + 4000, seed=9, host="amd")
    write_run(root, "run-d-0", cand=CAND_C, p95=0, fps=0, verdict="FAIL", reason="measure step crashed", start=T0 + 5000, seed=11,
              crash=["java.lang.NullPointerException: Cannot invoke \"net.minecraft.world.level.Level.getBlockState\" because \"level\" is null",
                     "\tat com.example.examplemod.render.ChunkMesher.mesh(ChunkMesher.java:88) ~[examplemod-1.0.0.jar%23101!/:?] {re:classloading}",
                     "\tat net.minecraft.client.renderer.LevelRenderer.renderLevel(LevelRenderer.java:1200) ~[client.jar:?] {re:mixin}"])
    return os.path.join(root, "harness", "test-runs")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/devlab-fixtures"
    print(build(out))
