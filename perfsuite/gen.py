#!/usr/bin/env python3
"""Orbital Atlas perf suite: generate the per-host harness scenarios from manifest.json.

  gen.py            write every scenario in the manifest to /root/mcdevlab-shared/harness/scenarios/atlas-ps-<id>-<host>.json
  gen.py --check    only print what would be written (names, step counts, estimated seconds)

Scenario names end in the host suffix (-fd/-gpc/-mac/-ct224) as the runners require. The steps only use the pinned
harness 0.1.1-gravity generic steps (no scroll/key) plus orbitalatlas:* steps present in rc.142..rc.160.
Every builder below documents what the segment stresses; the scorer (score.py) keys on the measure labels and on the
step-message timelines (minimap_state miniLevels, map_state lastRegional / wmLevels) by step label prefix.
"""
import argparse, hashlib, json, os

HERE = os.path.dirname(os.path.realpath(__file__))
OUT = "/root/mcdevlab-shared/harness/scenarios"
WORLD = "MiniAtlas-Visual-1790853134481"
LANE = OUT + "/atlas-rc160-cavewindow-%s.json"   # the owner-tunnel arrival recipe per host (from the rc.160 cave lane)
with open(os.path.join(HERE, "manifest.json")) as stream:
    MANIFEST = json.load(stream)


def station(host, scenario):
    return MANIFEST["representative_stations"].get(host, {}).get("routes", {}).get(scenario, {})


def cmd(c):
    return {"type": "command", "cmd": c, "elevated": True}


def wait(s):
    return {"type": "wait_seconds", "seconds": s}


def measure(label, s):
    return {"type": "measure", "label": label, "seconds": s}


def mm_state():
    return {"type": "orbitalatlas:minimap_state"}


def map_state():
    return {"type": "orbitalatlas:map_state", "radius": 4}


def shot(n):
    return {"type": "screenshot", "name": n}


def load_world(rd=12, dialog_timeout=15):
    steps = [
        {"type": "wait_title", "timeout_seconds": 300},
        {"type": "set_option", "key": "renderDistance", "value": rd},
        # Uncapped frames (since 10:35Z 2026-11-15): with the default 60 fps cap frame p50 sits at 16.7 ms on fd/GPC and hides cost.
        {"type": "set_option", "key": "maxFps", "value": 260}, {"type": "set_option", "key": "enableVsync", "value": False},
        {"type": "open_world", "world": WORLD},
    ]
    if dialog_timeout is not None:
        steps.append({"type": "click_button", "pattern": "(?i)^(continue without fixes|proceed|yes|continue|i understand|ok)$",
                      "optional": True, "timeout_seconds": dialog_timeout})
    return steps + [
        {"type": "wait_world", "timeout_seconds": 420},
        {"type": "orbitalatlas:window", "width": 1512, "height": 921, "timeout_seconds": 20},
    ]


def header(rd=12):
    return load_world(rd) + [
        cmd("gamemode creative"), cmd("difficulty peaceful"), cmd("gamerule doDaylightCycle false"), cmd("time set 6000"),
        cmd("weather clear 100000"), cmd("gamerule doMobSpawning false"), cmd("gamerule sendCommandFeedback false"),
    ]


def footer():
    return [{"type": "assert_log", "pattern": "Game crashed|\\[FATAL\\]|ERROR\\].*MapFailsafe|Orbital Atlas.*disabled", "present": False,
             "since_step": 0, "timeout_seconds": 3}, {"type": "quit"}]


def poll_mm(prefix, seconds, every=1.0):
    """minimap_state every `every` s for `seconds` s; the scorer reads miniLevels from the step messages (timeline)."""
    out = []
    n = int(round(seconds / every))
    for i in range(n + 1):
        out.append(mm_state())
        if i < n:
            out.append(wait(every))
    return out


def poll_map(times):
    """map_state at the given offsets (s) after the previous step (cumulative waits)."""
    out, t = [], 0.0
    for x in times:
        if x > t:
            out.append(wait(round(x - t, 2)))
            t = x
        out.append(map_state())
    return out


def join_block():
    # Join to map: no settle wait. Minimap LOD census at 1 Hz-ish plus three 10 s measure windows (tile_view/first_paint).
    s = [mm_state(), shot("join_00s")]
    for i in range(3):
        s += [measure("join_%02d_%02ds" % (i * 10, i * 10 + 10), 10), mm_state()]
    s += [shot("join_30s"), {"type": "orbitalatlas:wait_ready", "radius": 6, "coverage": 0.9, "stable_seconds": 2,
                             "timeout_seconds": 180, "require_idle": False}, mm_state()]
    return s


def zoom_sweep(tag, spans, pan=True):
    # World map: per span, census timeline (map_state at 0,.5,1,2,3,5 s), a still window and a pan window.
    s = []
    for sp in spans:
        s += [{"type": "orbitalatlas:open_map", "span": sp, "timeout_seconds": 60}]
        s += poll_map([0, 0.5, 1, 2, 3, 5])
        s += [shot("%s_%d" % (tag, sp)), measure("%s_still_%d" % (tag, sp), 4)]
        if pan:
            s += [{"type": "orbitalatlas:pan", "seconds": 4, "dx": 160, "dy": 0}, measure("%s_pan_%d" % (tag, sp), 4), map_state()]
    return s


def zoom_burst():
    # Zoom smoothness proxy: instant span changes 0.25 s apart (no animated zoom step exists in harness 0.1.1);
    # progress.jsonl (1 Hz frame max/p95) and the mapPerf 5 s window cover it. Then a rotation window (map_spin).
    s = [{"type": "orbitalatlas:open_map", "span": 1024, "timeout_seconds": 60}, wait(2)]
    for rep in range(2):
        for sp in (768, 512, 384, 256, 192, 128, 96, 64, 96, 128, 192, 256, 384, 512, 768, 1024):
            s.append({"type": "orbitalatlas:open_map", "span": sp, "timeout_seconds": 60})
            if sp <= 256:
                s.append(map_state())
            s.append(wait(0.25))
    s += [map_state(), {"type": "orbitalatlas:open_map", "span": 256, "timeout_seconds": 60}, wait(1),
          {"type": "orbitalatlas:map_spin", "seconds": 8, "px_per_second": 120}, measure("wm_spin_256", 8), map_state()]
    return s


def q_basepack(host):
    route = station(host, "q-basepack")
    flight_x, flight_z, flight_yaw = route.get("flight", (-53600.5, 4000.5, 79))
    walk_x, walk_z, walk_yaw = route.get("walk", (3300.5, 1800.5, -90))
    walk_arrival_yaw = walk_yaw if "walk" in route else 0
    s = header(12)
    s += join_block()
    # World map zoom sweep at the join position: cold (first open) then warm (second open).
    s += zoom_sweep("wm1", (64, 128, 256, 512, 1024))
    s += [{"type": "orbitalatlas:close_map"}, wait(2)]
    s += zoom_sweep("wm2", (64, 128, 256, 512, 1024), pan=False)
    s += zoom_burst()
    s += [{"type": "orbitalatlas:close_map"}, wait(2)]
    # Elytra-speed flight (creative fly 1.5 blocks/tick = 30 b/s, the elytra cruise speed; deterministic route = capflight).
    s += [{"type": "orbitalatlas:minimap_zoom", "mode": "auto"},
          {"type": "teleport", "x": flight_x, "y": 230, "z": flight_z, "yaw": flight_yaw, "pitch": 20, "no_gravity": True, "timeout_seconds": 240},
          {"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, wait(5), mm_state(),
          {"type": "orbitalatlas:fly", "seconds": 62, "speed": 1.5, "yaw": flight_yaw, "altitude": 230}]
    for i in range(6):
        s += [measure("fly_%02ds" % (i * 10 + 10), 10), mm_state()]
    # Landing: drop to the surface, then time the minimap's re-sharpen at the walking zoom (1 Hz census, 20 s).
    # 2026-11-15 22:58Z (perf-climb): without the SFCR cloud mod the flight covers the full 1800 blocks and the integrated server is still
    # catching up when the flight ends: the 30 s command step timed out (GPC 4/4). Let the chunks round the player arrive first.
    s += [{"type": "wait_chunks", "radius": 4, "timeout_seconds": 300}]
    s += [wait(1), cmd("gamemode survival"), cmd("execute as @p at @s positioned over motion_blocking_no_leaves run tp @s ~ ~ ~"),
          wait(1), cmd("gamemode creative"), shot("land_00s")]
    s += poll_mm("land", 20)
    s += [shot("land_20s"), measure("land_20_30s", 10), mm_state()]
    # Walk/explore 5 min (closezoom-walk route: fly 0.28 b/t ~ sprint speed at low altitude), 30 s windows.
    s += [{"type": "teleport", "x": walk_x, "y": 230, "z": walk_z, "yaw": walk_arrival_yaw, "pitch": 30, "no_gravity": True, "timeout_seconds": 240},
          {"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, cmd("gamemode survival"),
          cmd("execute as @p at @s positioned over motion_blocking_no_leaves run tp @s ~ ~ ~ 0 30"), wait(3), cmd("gamemode creative"),
          {"type": "orbitalatlas:minimap_zoom", "mode": "auto"},
          {"type": "orbitalatlas:fly", "seconds": 300, "speed": 0.28, "yaw": walk_yaw, "altitude": route.get("walk_altitude", 100)}]
    for i in range(10):
        s += [measure("walk_%03ds" % (i * 30 + 30), 30), mm_state()]
    # Close-zoom relief pop-in at the walk end: world map at span 64 (3D relief), census + screenshots over 6 s.
    s += [{"type": "orbitalatlas:open_map", "span": 64, "tilt": 55, "timeout_seconds": 60}, map_state(), shot("close64_00s")]
    s += [wait(0.5), map_state(), shot("close64_0p5s"), wait(0.5), map_state(), shot("close64_1s"), wait(1), map_state(), shot("close64_2s"),
          wait(2), map_state(), shot("close64_4s"), wait(2), map_state(), shot("close64_6s"), measure("close64_still", 4)]
    s += [{"type": "orbitalatlas:close_map"}, wait(1)]
    # Revisit: back to the flight start (left ~7 min ago); minimap census 1 Hz for 15 s, then world map 256 census.
    s += [{"type": "teleport", "x": flight_x, "y": 120, "z": flight_z, "yaw": flight_yaw, "pitch": 20, "no_gravity": True, "timeout_seconds": 240}]
    s += poll_mm("revisit", 15)
    s += [measure("revisit_15_25s", 10), {"type": "orbitalatlas:open_map", "span": 256, "timeout_seconds": 60}]
    s += poll_map([0, 0.5, 1, 2, 3, 5])
    # Keep the game alive for an enclosing diagnostic sample after the last measured station.
    s += [measure("revisit_wm_256", 4), {"type": "orbitalatlas:close_map"}, wait(8)]
    comment = route.get("comment", "perfsuite quick set: join, world-map zoom sweep (cold/warm/burst/spin), "
                        "30 b/s flight + landing, 5 min walk, close-zoom relief, revisit. base-pack fixture.")
    scenario = {"name": "atlas-ps-q-basepack-" + host, "_comment": comment, "with_candidate": True,
            "timeout_seconds": 1500, "screenshot_on_failure": True, "large_fixture": route.get("fixture", "large-gpc-basepack-v1"), "steps": s + footer()}
    if "description" in route:
        scenario["description"] = route["description"]
    return scenario


def q_cave(host):
    """Owner tunnel (-2776 -6 2881): arrival recipe copied from the rc.160 cave lane scenario for this host (its fixture and
    tunnel carve), minus the cave-style override and the AUTO-interior assert (GPC never reports inside), then the cave spans."""
    lane = json.load(open(LANE % host))
    st = lane["steps"]
    end = next(i for i, x in enumerate(st) if x.get("type") == "screenshot")   # first screenshot = arrival done
    arrive = [x for x in st[:end] if x.get("type") not in ("orbitalatlas:set_feature_text", "orbitalatlas:interior_wait")
              and not (x.get("type") == "orbitalatlas:window")]
    # keep our own window size before commands
    arrive.insert(5, {"type": "orbitalatlas:window", "width": 1512, "height": 921, "timeout_seconds": 20})
    arrive[2:2] = [{"type": "set_option", "key": "maxFps", "value": 260}, {"type": "set_option", "key": "enableVsync", "value": False}]
    s = arrive + [shot("cave_arrive"), {"type": "orbitalatlas:interior_signals"}, mm_state()]
    for sp in (64, 128, 256):
        s += [{"type": "orbitalatlas:minimap_zoom", "mode": "manual", "span": sp}, wait(1)]
        s += poll_mm("mmcave%d" % sp, 4)
        s += [measure("mm_cave_%d" % sp, 6), mm_state(), shot("mm_cave_%d" % sp)]
    s += [{"type": "orbitalatlas:minimap_zoom", "mode": "auto"}]
    for sp in (64, 96, 128, 192, 256, 384, 640, 1024):
        s += [{"type": "orbitalatlas:open_map", "span": sp, "x": -2776, "z": 2881, "yaw": 0, "tilt": 55, "timeout_seconds": 60}]
        s += poll_map([0, 0.5, 1, 2, 4])
        s += [shot("wmcave_%d" % sp), measure("wm_cave_%d" % sp, 4)]
    s += [{"type": "orbitalatlas:pan", "seconds": 5, "dx": 120, "dy": 0}, measure("wm_cave_pan_1024", 5), map_state(),
          {"type": "orbitalatlas:open_map", "span": 128, "x": -2776, "z": 2881, "yaw": 0, "tilt": 55, "timeout_seconds": 60},
          {"type": "orbitalatlas:map_spin", "seconds": 6, "px_per_second": 120}, measure("wm_cave_spin_128", 6), map_state(),
          {"type": "orbitalatlas:close_map"}, wait(1)]
    return {"name": "atlas-ps-q-cave-" + host, "_comment": "perfsuite quick set: caves at the owner tunnel, minimap 64/128/256, "
            "world map 64..1024 + pan + spin.", "with_candidate": True, "timeout_seconds": 1500, "screenshot_on_failure": True,
            "large_fixture": lane.get("large_fixture"), "steps": s + footer()}


def n_mixed(host):
    """Nightly mixed set on base-pack: riding (horse), building edits, radar crowd, 200 marks, wide zoom, GUI scale, dimension
    round trip (overworld -> Nether walk -> End -> overworld) with heap after each leg."""
    s = header(12) + [{"type": "orbitalatlas:wait_ready", "radius": 6, "coverage": 0.9, "stable_seconds": 2, "timeout_seconds": 180,
                       "require_idle": False}, measure("base_idle", 10)]
    # Riding: tame saddled horse, mount, drive 30 s.
    s += [{"type": "teleport", "x": 3300.5, "y": 230, "z": 1800.5, "yaw": 0, "pitch": 30, "no_gravity": True, "timeout_seconds": 240},
          {"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, cmd("gamemode survival"),
          cmd("execute as @p at @s positioned over motion_blocking_no_leaves run tp @s ~ ~ ~ -90 20"), wait(3), cmd("gamemode creative"),
          cmd('execute at @p run summon horse ~ ~ ~ {Tame:1b,SaddleItem:{id:"minecraft:saddle",count:1}}'), wait(1),
          cmd("execute as @p at @s run ride @s mount @e[type=horse,limit=1,sort=nearest]"), wait(1), mm_state(),
          {"type": "orbitalatlas:drive", "seconds": 30, "speed": 0.45, "yaw": -90}, measure("ride_horse_30s", 30), mm_state(),
          cmd("execute as @p run ride @s dismount"), wait(1)]
    # Building: map edits next to the player (edit -> tile latency), then a fill burst + measure.
    s += [{"type": "orbitalatlas:edit_latency", "trials": 6, "trial_timeout_seconds": 20, "mode": "world"}, measure("build_after_edits", 5)]
    for i in range(10):
        blk = "minecraft:stone" if i % 2 == 0 else "minecraft:air"
        s += [cmd("execute at @p run fill ~4 ~ ~4 ~12 ~6 ~12 %s" % blk), wait(0.5)]
    s += [measure("build_burst_after", 10), mm_state()]
    # Radar crowd.
    s += [{"type": "orbitalatlas:summon_all_mobs", "radius": 30, "spacing": 3, "modded": True, "finalize": True}, wait(2),
          measure("radar_crowd_hud", 15), {"type": "orbitalatlas:open_map", "span": 128, "timeout_seconds": 60},
          measure("radar_crowd_wm128", 8), {"type": "orbitalatlas:close_map"}, cmd("execute at @p run kill @e[type=!player,distance=..80]"), wait(2)]
    # 200 marks (waypoint stand-in; no N-waypoint step exists).
    for i in range(200):
        s.append({"type": "orbitalatlas:pings", "action": "own_mark", "dx": (i % 20 - 10) * 40, "dz": (i // 20 - 5) * 40, "label": "m%d" % i})
    s += [measure("marks200_hud", 10), {"type": "orbitalatlas:open_map", "span": 1024, "timeout_seconds": 60},
          measure("marks200_wm1024", 8), map_state(), {"type": "orbitalatlas:pings", "action": "clear"}]
    # Wide zoom over explored land.
    for sp in (4096, 16384):
        s += [{"type": "orbitalatlas:open_map", "span": sp, "timeout_seconds": 60}]
        s += poll_map([0, 1, 2, 4, 8])
        s += [shot("wide_%d" % sp), measure("wide_still_%d" % sp, 6),
              {"type": "orbitalatlas:pan", "seconds": 6, "dx": 200, "dy": 60}, measure("wide_pan_%d" % sp, 6), map_state()]
    s += [{"type": "orbitalatlas:close_map"}, wait(1)]
    # GUI scale.
    for g in (2, 4):
        s += [{"type": "set_option", "key": "guiScale", "value": g}, wait(2), measure("gui%d_hud" % g, 8),
              {"type": "orbitalatlas:open_map", "span": 512, "timeout_seconds": 60}, measure("gui%d_wm512" % g, 6),
              {"type": "orbitalatlas:close_map"}]
    s += [{"type": "set_option", "key": "guiScale", "value": 3}, wait(1)]
    # Dimension round trip.
    s += [measure("dim_ow_before", 5),
          {"type": "teleport", "x": 412.5, "y": 70, "z": 225.5, "dimension": "minecraft:the_nether", "no_gravity": True, "timeout_seconds": 240}]
    s += poll_mm("nether_arrive", 10)
    s += [{"type": "orbitalatlas:fly", "seconds": 60, "speed": 0.28, "yaw": -90, "altitude": 70},
          measure("nether_walk_30s", 30), mm_state(), measure("nether_walk_60s", 30), mm_state(),
          {"type": "orbitalatlas:open_map", "span": 256, "timeout_seconds": 60}, measure("nether_wm256", 6), map_state(),
          {"type": "orbitalatlas:close_map"},
          {"type": "teleport", "x": 100.5, "y": 70, "z": 0.5, "dimension": "minecraft:the_end", "no_gravity": True, "timeout_seconds": 240}]
    s += poll_mm("end_arrive", 10)
    s += [measure("end_30s", 20),
          {"type": "teleport", "x": 3300.5, "y": 200, "z": 1800.5, "dimension": "minecraft:overworld", "no_gravity": True, "timeout_seconds": 240}]
    s += poll_mm("ow_return", 15)
    s += [measure("dim_ow_after", 10), map_state()]
    return {"name": "atlas-ps-n-mixed-" + host, "_comment": "perfsuite nightly mixed set", "with_candidate": True, "timeout_seconds": 1500,
            "screenshot_on_failure": True, "large_fixture": "large-gpc-basepack-v1", "steps": s + footer()}


def n_soak(host):
    """20 min mixed soak on base-pack: alternating flight legs, walking, world map opens; heap/GC/store per 60 s."""
    s = header(12) + [{"type": "orbitalatlas:wait_ready", "radius": 6, "coverage": 0.9, "stable_seconds": 2, "timeout_seconds": 180,
                       "require_idle": False},
                      {"type": "orbitalatlas:storage", "action": "state"}]
    legs = [(-53600.5, 4000.5, 79), (3300.5, 1800.5, -90), (-53600.5, 6000.5, 79), (3300.5, -1200.5, 180)]
    t = 0
    for k in range(7):
        x, z, yaw = legs[k % len(legs)]
        s += [{"type": "teleport", "x": x, "y": 230, "z": z, "yaw": yaw, "pitch": 20, "no_gravity": True, "timeout_seconds": 240},
              {"type": "orbitalatlas:fly", "seconds": 120, "speed": 1.0 if k % 2 == 0 else 0.28, "yaw": yaw, "altitude": 230 if k % 2 == 0 else 110}]
        for j in range(2):
            t += 60
            s += [measure("soak_%04ds" % t, 60), mm_state()]
        s += [{"type": "orbitalatlas:open_map", "span": 1024, "timeout_seconds": 60}, measure("soak_wm_%d" % k, 10), map_state(),
              {"type": "orbitalatlas:close_map"}, {"type": "orbitalatlas:storage", "action": "state"}]
    return {"name": "atlas-ps-n-soak-" + host, "_comment": "perfsuite nightly 20 min soak", "with_candidate": True, "timeout_seconds": 1500,
            "screenshot_on_failure": True, "large_fixture": "large-gpc-basepack-v1", "steps": s + footer()}


def q_basepack_off(host):
    """Atlas-off twin of q-basepack (arm off: orbitalatlas.harnessMapOff=true): same route, same measure labels and timing; map steps
    (wait_ready, open_map, map_state, minimap_*, pan, map_spin, close_map) become equal waits, since they need the map."""
    sc = q_basepack(host)
    out = []
    for x in sc["steps"]:
        t = x["type"]
        if t == "orbitalatlas:wait_ready":
            out += [{"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, wait(2)]
        elif t in ("orbitalatlas:pan", "orbitalatlas:map_spin"):
            out.append(wait(0.05))   # these were async; the following measure keeps its length
        elif t.startswith("orbitalatlas:") and t not in ("orbitalatlas:fly", "orbitalatlas:window"):
            out.append(wait(0.05))
        else:
            out.append(x)
    sc["steps"] = out
    sc["name"] = "atlas-ps-q-basepack-off-" + host
    sc["_comment"] = "Atlas-off twin of q-basepack (run with orbitalatlas.harnessMapOff=true)"
    return sc


BUILDERS = {"q_basepack_off": q_basepack_off, "q_basepack": q_basepack, "q_cave": q_cave, "n_mixed": n_mixed, "n_soak": n_soak}


def q_pingpong(host):
    """perf-climb 2026-11-15 (owner: the world map should REMEMBER what was just looked at): world map A <-> B return visits.
    A and B are 400 blocks west / east of the join position (explored land of the base-pack fixture, outside the minimap). Per visit:
    map_state at 0 / 0.5 / 1 / 2 / 4 s (lastRegional view/src/coarse, savedLane + regionalLane reads, evictions, budget) and a
    screenshot at each of those times, then a 2 s frame window. 4 cycles at span 256, then 2 cycles at span 1024."""
    s = header(12)
    s += join_block()
    # v2 (2026-11-16 01:00Z): v1's points (-976 / -176, 880) were uncharted parchment. These lie in the densely explored block of the
    # fixture (16x16-chunk regions 12..15 x 6..8 fully stored: blocks x 3072..4095, z 1536..2300), ~4000 blocks from the join point.
    ax, bx, z = 3300, 3900, 1750
    def visit(tag, x, span):
        v = [{"type": "orbitalatlas:open_map", "span": span, "x": x, "z": z, "yaw": 0, "tilt": 45, "timeout_seconds": 60}, map_state(), shot(tag + "_0s")]
        v += [wait(0.5), map_state(), shot(tag + "_0p5s"), wait(0.5), map_state(), shot(tag + "_1s"), wait(1), map_state(), shot(tag + "_2s"),
              wait(2), map_state(), shot(tag + "_4s"), measure(tag, 2), map_state()]
        return v
    for span, cycles in ((256, 4), (1024, 2)):
        for c in range(1, cycles + 1):
            s += visit("pp%d_A%d" % (span, c), ax, span)
            s += visit("pp%d_B%d" % (span, c), bx, span)
        s += visit("pp%d_A%d" % (span, cycles + 1), ax, span)
    s += [{"type": "orbitalatlas:close_map"}, wait(2), mm_state()]
    return {"name": "atlas-ps-q-pingpong-" + host, "description": "perf suite: world map A<->B return visits (does the map keep what was just shown?)",
            "with_candidate": True, "timeout_seconds": 1500, "screenshot_on_failure": True, "large_fixture": "large-gpc-basepack-v1", "steps": s + footer()}


BUILDERS["q_pingpong"] = q_pingpong


def q_flight(host):
    """perf-climb 2026-11-16 (flight fine-tile streaming): only the 30 b/s capflight of q-basepack (same teleport, route and windows), with a
    minimap screenshot after every 10 s window (owner evidence: the minimap during the same flight segment), then 10 s after landing."""
    s = header(12)
    s += [mm_state(), measure("join_00_10s", 10)]
    s += [{"type": "orbitalatlas:minimap_zoom", "mode": "auto"},
          {"type": "teleport", "x": -53600.5, "y": 230, "z": 4000.5, "yaw": 79, "pitch": 20, "no_gravity": True, "timeout_seconds": 240},
          {"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, wait(5), mm_state(), shot("fly_00s"),
          {"type": "orbitalatlas:fly", "seconds": 62, "speed": 1.5, "yaw": 79, "altitude": 230}]
    for i in range(6):
        s += [measure("fly_%02ds" % (i * 10 + 10), 10), mm_state(), shot("fly_%02ds" % (i * 10 + 10))]
    s += [{"type": "wait_chunks", "radius": 4, "timeout_seconds": 300}, mm_state(), shot("land_00s")]
    s += poll_mm("land", 10)
    s += [shot("land_10s"), measure("land_10_20s", 10), mm_state()]
    return {"name": "atlas-ps-q-flight-" + host, "description": "perf suite: the 30 b/s flight of q-basepack alone, minimap screenshots per window",
            "with_candidate": True, "timeout_seconds": 1200, "screenshot_on_failure": True, "large_fixture": "large-gpc-basepack-v1", "steps": s + footer()}


BUILDERS["q_flight"] = q_flight


def q_flightpg(host):
    """perf lane 3 2026-11-16: q-flight over PRE-GENERATED chunks. q-flight's route runs through land the save never generated: the integrated
    server's world generation falls 60 s behind (3-5 server ticks per 10 s window, 0 of the 289 chunks round the player loaded by mid flight),
    so its fine share measures worldgen, not Atlas. Here the route is generated first with /forceload (server side only: the client and Atlas
    see none of it), released, and then flown; chunks arrive from disk as on a pre-generated server."""
    import math
    s = header(12)
    s += [mm_state(), {"type": "orbitalatlas:minimap_zoom", "mode": "auto"},
          {"type": "teleport", "x": -53600.5, "y": 230, "z": 4000.5, "yaw": 79, "pitch": 20, "no_gravity": True, "timeout_seconds": 240},
          {"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}]
    dx, dz = -math.sin(math.radians(79)), math.cos(math.radians(79))
    cx0, cz0 = -53600.5 / 16, 4000.5 / 16
    cx_hi = int(math.floor(cx0)) + 13; cx_lo = int(math.floor((-53600.5 + dx * 1900) / 16)) - 13
    c = cx_hi
    while c > cx_lo:
        lo = max(cx_lo, c - 8); mid = (c + lo) / 2.0
        zc = cz0 + (mid - cx0) * dz / dx; z0 = int(math.floor(zc)) - 13; z1 = z0 + 27
        s.append(cmd("forceload add %d %d %d %d" % (lo * 16, z0 * 16, c * 16 + 15, z1 * 16 + 15)))
        c = lo - 1
    pre = {"gpc": 180, "mac": 240, "fd": 420, "ct224": 420}[host]
    s += [measure("pregen", pre), cmd("forceload remove all"), wait(15), mm_state(), shot("fly_00s"),
          {"type": "orbitalatlas:fly", "seconds": 62, "speed": 1.5, "yaw": 79, "altitude": 230}]
    for i in range(6):
        s += [measure("fly_%02ds" % (i * 10 + 10), 10), mm_state(), shot("fly_%02ds" % (i * 10 + 10))]
    s += [{"type": "wait_chunks", "radius": 4, "timeout_seconds": 300}, mm_state(), shot("land_00s")]
    s += poll_mm("land", 10)
    s += [shot("land_10s"), measure("land_10_20s", 10), mm_state()]
    return {"name": "atlas-ps-q-flightpg-" + host, "description": "perf lane 3: the q-flight route over pre-generated chunks (forceload first)",
            "with_candidate": True, "timeout_seconds": 1500, "screenshot_on_failure": True, "large_fixture": ("large-gpc-basepack-flightpg-20261116" if host == "ct224" else "large-gpc-basepack-v1"), "steps": s + footer()}


BUILDERS["q_flightpg"] = q_flightpg


def q_texture(host):
    """First open of a saved surface chart on the paired world and Atlas store."""
    route = station(host, "q-texture")
    description = "plain-surface close zoom time to first textured tile; zero in 60 s FAILS"
    if route:
        # Leave the saved player in place. Only the map camera moves to the remote chart.
        x, z = route["chart"]
        fixture, description = route["fixture"], route["description"]
        s = load_world(2, dialog_timeout=route.get("dialog_timeout", 1))
    else:
        x, z = {"fd": (-1330, 1460), "ct224": (-1330, 1460), "mac": (-2776, 2881), "gpc": (-53600, 4000)}[host]
        fixture = {"mac": "owner-lush-20261117-consistent", "gpc": "large-gpc-basepack-20261117-consistent"}.get(host, "owner-dungeon-20261117-consistent")
        s = header(10)
        arrival = cmd("execute as @p at @s positioned over motion_blocking_no_leaves run tp @s ~ ~1 ~")
        arrival["timeout_seconds"] = 120
        s += [{"type": "teleport", "x": x + 0.5, "y": 230, "z": z + 0.5,
               "yaw": 0, "pitch": 30, "no_gravity": True, "timeout_seconds": 180},
              {"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, arrival, wait(6),
              {"type": "orbitalatlas:interior_signals"}, shot("texture_surface_hud")]
    opening = {"type": "orbitalatlas:open_map", "span": 128, "x": x, "z": z,
               "yaw": 0, "tilt": 55, "timeout_seconds": 60}
    if "camera_y" in route:
        opening["camera_y"] = route["camera_y"]
    s.append(opening)
    # The scorer uses actual timestamps, not requested waits. A sample beyond
    # 60 s establishes complete observation but cannot rescue a missed deadline.
    s += poll_map([0, 0.5, 1, 2, 3, 5] + list(range(6, 61)))
    s.append(shot("texture_surface_60s"))
    if "close_after_heartbeat_seconds" in route:
        s.append({"type": "assert_log", "pattern": r"\[io\.github\.largeorbitalobject\.orbitalatlas\.client\.MacDiagnostics/\]: \[OrbitalAtlasDiag\] seq=[0-9]+ t=[0-9]+s",
                  "present": True, "since_step": len(s)-1, "timeout_seconds": 12})
        s.append(wait(route["close_after_heartbeat_seconds"]))
    s += [{"type": "orbitalatlas:close_map"}, wait(6)]
    return {"name": "atlas-ps-q-texture-" + host, "description": description,
            "with_candidate": True, "timeout_seconds": 900, "screenshot_on_failure": True,
            "large_fixture": fixture, "texture_gate": {"station": "plain_surface", "span": 128, "limit_seconds": 60},
            "steps": s + footer()}


BUILDERS["q_texture"] = q_texture


def q_detail60(host):
    """Capped-60 fps station (added 2026-11-20, Fable trusted-restore lane; no other station changed). A player on vsync or the
    default frame limit gets the map's per-frame work allowance 60 times a second instead of 260, so this is where detail arrives
    slowest. Three sessions at a visited site at maxFps 60: join, relog, relog. Each opens the span-128 map at the site the moment
    the world is in and holds it for 60 s in six 10 s frame windows (`fill_sN_tt`), with a map_state before and after each window
    (detT = detailed chunks, surfT = chunks on a saved surface model), then 10 s of HUD frames (`hud_sN`). Session 1 primes."""
    x, z, fixture = {"mac": (-2776, 2881, "owner-lush-outdoors-20261118b-consistent"), "fd": (-1330, 1460, "pc62b-fd-surface-rc172-consistent"),
                     "gpc": (24, 88, "large-gpc-grounded-20261118b-consistent")}[host]
    s = load_world(10)
    for step in s:
        if step["type"] == "set_option" and step["key"] == "maxFps":
            step["value"] = 60
    relog = [{"type": "orbitalatlas:leave_world"}, {"type": "wait_title", "timeout_seconds": 120}] + [dict(x_) for x_ in s if x_["type"] in ("open_world", "click_button", "wait_world")]
    if host == "gpc":
        s.append({"type": "teleport", "x": x + 0.5, "y": 140, "z": z + 0.5, "yaw": 0, "pitch": 30, "no_gravity": True, "timeout_seconds": 180})
    s += [{"type": "wait_chunks", "radius": 6, "timeout_seconds": 300}, wait(30)]
    for n in (1, 2, 3):
        if n > 1:
            s += relog
        s.append({"type": "orbitalatlas:open_map", "span": 128, "x": x, "z": z, "yaw": 0, "tilt": 55, "timeout_seconds": 60})
        s.append(map_state())
        for t in range(0, 60, 10):
            s += [measure("fill_s%d_%02d" % (n, t), 10), map_state()]
        s += [shot("detail60_s%d_60s" % n), {"type": "orbitalatlas:close_map"}, measure("hud_s%d" % n, 10), wait(10)]
    return {"name": "atlas-ps-q-detail60-" + host, "description": "perf suite: close-map detail arrival and frame time after a relog at a 60 fps cap",
            "with_candidate": True, "timeout_seconds": 1500, "screenshot_on_failure": True, "large_fixture": fixture, "steps": s + footer()}


BUILDERS["q_detail60"] = q_detail60


def est_seconds(sc):
    t = 0.0
    for x in sc["steps"]:
        if x["type"] in ("wait_seconds", "measure"):
            t += x["seconds"]
    return t


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--hosts", help="comma-separated host names")
    parser.add_argument("--scenarios", help="comma-separated scenario IDs")
    args = parser.parse_args()
    hosts = set(args.hosts.split(",")) if args.hosts else None
    ids = set(args.scenarios.split(",")) if args.scenarios else None
    scenarios = MANIFEST["scenarios"]
    if ids and ids.difference(scenarios):
        parser.error("unknown scenario ID")
    if hosts and hosts.difference(h for ent in scenarios.values() for h in ent["hosts"]):
        parser.error("unknown host")
    for sid, ent in scenarios.items():
        if ids and sid not in ids:
            continue
        for host in ent["hosts"]:
            if hosts and host not in hosts:
                continue
            sc = BUILDERS[ent["builder"]](host)
            route = station(host, sid)
            data = json.dumps(sc, indent=route.get("json_indent", 2) if route else 1)
            if route:
                # Preserve the measured input's wire identity, including its terminal newline.
                if host in ("fd", "gpc"):
                    data += "\n"
                if hashlib.sha256(data.encode()).hexdigest() != route["scenario_sha256"]:
                    raise ValueError(f"{sid}/{host} differs from its frozen scenario digest")
            p = os.path.join(OUT, sc["name"] + ".json")
            print("%-28s steps=%4d waits+measures=%5.0fs fixture=%s -> %s" % (sc["name"], len(sc["steps"]), est_seconds(sc), sc["large_fixture"], p))
            if not args.check:
                tmp = p + ".tmp"
                with open(tmp, "w") as stream:
                    stream.write(data)
                os.replace(tmp, p)


if __name__ == "__main__":
    main()
