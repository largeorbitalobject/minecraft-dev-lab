# Dev Lab Viewer

Read-only web app for watching and comparing Minecraft mod test runs across a fleet of test machines.
Python 3.13 standard library only (Pillow is used for JPEG thumbnails when installed, a pure-Python PNG
downscaler is the fallback). No CDN, no external calls: fonts and charts are vendored or hand drawn.

    ./run.sh                       foreground: collector threads + HTTP server on 0.0.0.0:8095
    ./run.sh --port 8099 --data /tmp/x --no-collector     (serve an existing data dir only)
    python3 -m mcdevlab_viewer.collector --once           (one collector pass, prints per-host result)
    python3 -m unittest discover -s tests                 (90 tests, about 20 s, includes a real-fleet end to end)
    python3 tests/demo.py --port 8096                     (fixture-backed demo with a "live" harness run)

## What it shows

* **Fleet**: per host state (idle, running with elapsed time, paused by pause file, stale heartbeat,
  unreachable), the candidate it holds (label, commit, jar sha), last verdict per gate and per recipe.
  While a Dev Lab job is RUNNING the host card shows live charts (fps, frame p95, heap from
  `progress.jsonl`), or the `progress.txt` phase timeline for feature-plan runs, plus a log tail, refreshed
  every 2 s in the browser and every 5 s in the collector.
* **Run page**: verdict and reason, candidate and environment, step or phase timeline, metrics table against
  a chosen baseline (better and worse use each metric's own direction; a delta inside 2 standard deviations of
  same-code runs is marked within noise once n>=3), frame-time series, screenshot gallery with side by side
  baseline, crash signature with culprit guess, log diff (only WARN, ERROR and exception signatures that are
  new relative to the baseline after normalising timestamps, numbers, hex ids and run dirs), checks, evidence files.
* **Compare**: baseline vs candidate commit, matrix of host x gate with both verdicts and key metric deltas.
  If renderer, host, OS, scenario, gate definition, heap, window, client manifest, settings, Minecraft or loader
  versions differ, or either run did not pass, the deltas are withheld and the mismatch is shown instead.
  Facts missing on one side are listed as unverified. A mod list that differs in exactly one mod is read as the
  candidate mod (stated on screen); two or more differing mods refuse.
* **History**: per host and gate, sparklines of key metrics over the last 24 candidates; failed runs stay in the line.

## Sources (all read-only)

| source type | where | what it gives |
|---|---|---|
| `harness` | any dir containing `result.json` (+ `progress.jsonl`, `client.log`/`latest.log`/`harness.log`, PNGs) | first-class: `mcdevlab.harness.result.v1` per `/root/mcdevlab-shared/HARNESS-RESULT-SCHEMA.md`, contract problems are listed on the run |
| `featureplan_dirs` | CT224 `/root/cubeworld-runtime/feature-plan`, fd `/opt/orbital-atlas/feature-plan/runs`, Mac `~/mcdevlab/results/feature-plan` | `result.txt` / `INCOMPLETE.txt` / `HARNESS_INCOMPLETE.txt`, every `key=value` number as `<check>.<key>`, `progress.txt`, PNGs, `client.log` (log4j XML and plain mixed) |
| `fd_runs` | fd `/opt/orbital-atlas/results/runs/<id>` | smoke summaries and `smoke-direct.log`; merged into the feature-plan run when `evidence_dir` matches |
| `node_smoke`, `instance_fp` | GPC `C:\mcdevlab\results` and the Prism instance output dir, Mac `~/mcdevlab/results` | node summaries (incl. history and dry runs), `latest.log`, PNG and `result.txt` of the run whose window contains them |
| `smoke_v2` | CT224 `/root/mcdevlab-atlas/mcdevlab-results` | wrapper summary attached to its feature-plan run |
| `c39` | ctl `c39-validations` | Mac frame metrics (median/p95/p99/max/hitches), flagged not usable for matched comparison when the receipt says so |
| Dev Lab API | `run_list` / `run_logs` through the `minecraft-dev-lab.pyz` | job state, request ids, errors; a job that failed overrides evidence that says PASS (stated on the run) and failed jobs without evidence become runs with the reason pulled from their log |

Nodes are read with `mcdevlab_viewer/remote_agent.py`, piped to `python -` over the same transports the
fleet already uses (`pct exec`, `tailscale ssh`, `ssh`). It opens files with mode `rb`, lists directories and
reads the clock; `tests/test_remote_agent.py` greps it to enforce that. Nothing is installed or written on a node.

## Configuration

`config.json` (optional, same dir as `run.sh`) deep-merges over `mcdevlab_viewer/config.py`. See
`config.example.json`: add a host, add a `harness` source root on any host, set a host to `null` to drop it.
Another mod needs only a `harness` source pointing at the directory where its harness writes `-Dmcdevlab.output`.

## Data (`data/`, written only by the collector)

`runs/<id>/run.json` (normalised run), `mirror.json` (remote fingerprint of every copied file), `log.txt.gz`,
`shots/` (full size, capped at 30 MB per run), `thumbs/`, `files/` (result.txt, summaries); plus `hosts/`, `live/`,
`jobs/`, `cache/`, `candidates.json`, `state.json`. A file is copied once and only again if its size or mtime
on the node changes. Full-size screenshots of the oldest runs are dropped once the originals exceed 1500 MB
(thumbnails stay).

## Memory

The server indexes every `run.json` without its heavy fields (`metrics`, `progress`, `steps`, `series`; ~85% of
the bytes) and keeps full runs in an LRU bounded by `run_cache_mb` of run.json (default 48). On 2026-10-21 that
took the server from ~1.9 GB to ~0.35 GB RSS over 3916 runs. After each poll the collector calls `malloc_trim`
so the transient parse peak goes back to the OS, and run.sh sets `MALLOC_ARENA_MAX=2`. RSS still drifts up slowly (allocator fragmentation; traced
Python memory stays flat across polls).

## Deploy (not done by the build)

    ./run.sh            # serves on :8095
    curl -s localhost:8095/api/status

Put it behind your own reverse proxy if you want a hostname.

The page is served from the root of its own
subdomain (`/api`, `/static` and `/files` are absolute paths).

### Large-runner live state (fd and CT224)

The `amd` and `proxmox-test` pollers also read the newest local
`results-archive/fd|ct224/<run>/controller-status.json`. This does not contact or
control the game. A fresh `RUNNING` mirror provides the scenario and start time;
active polls refresh every five seconds. Initial discovery retains the existing
60-second full-poll interval. The runner normally mirrors every 20 seconds.
After 90 seconds without a mirror, the display reports stale/unconfirmed rather
than claiming that a game is still running or has exited. Cleanup pending also
stays unconfirmed. PASS/FAIL receipts require `exit_confirmed=true` before the
controller observation becomes idle. A real API job still takes precedence;
terminal controller history cannot erase it or a remote pause.

This supplies job state, not live screenshot/log streaming. State evidence is in
`data/hosts/{amd,proxmox-test}.json` and `data/live/...`. Source regressions:
`python3 -B -m unittest discover -s tests -p test_harness_state.py -v`.
