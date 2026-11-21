"""Fleet configuration: where evidence lives on each host and how to reach it (all read-only).

Defaults describe the Orbital fleet. A `config.json` next to run.sh deep-merges over them, so another mod
or fleet can add hosts, add `harness` sources, or point at other roots without touching code.

Source types (see adapters.py):
  harness          generic harness output dirs (result.json + progress.jsonl), any depth
  featureplan_dirs feature-plan run dirs  <root>/<run>/{input.json,exit.json,client.log,probe/...}
  smoke_v2         CT224 wrapper summaries <root>/smoke-*/summary.json
  fd_runs          fd smoke summaries      <root>/<run>/{summary.json,smoke-direct.log,result-*.txt}
  node_smoke       GPC/Mac node summaries  <root>/{smoke-summary*.json,history/*.json,latest.log}
  instance_fp      a Prism instance's mcdevlab-feature-plan output dir (result.txt/INCOMPLETE.txt/PNGs)
  c39              Mac c39 frame-metric validations <root>/*.json
"""
from __future__ import annotations

import copy
import json
import os

PYZ = "/root/minecraft-dev-lab-install/plugins/minecraft-dev-lab/minecraft-dev-lab.pyz"
FLEET_JSON = "/root/minecraft-dev-lab-config/fleet.json"

_FP_INCLUDE = ["*/input.json", "*/exit.json", "*/process.json", "*/final-verification.json", "*/fixture-gate-note.txt",
               "*/client.log", "*/probe/*"]
_FP_INLINE = ["*/input.json", "*/exit.json", "*/process.json", "*/final-verification.json", "*/fixture-gate-note.txt",
              "*/probe/*.txt"]

HARNESS_INCLUDE = ["result.json", "progress.jsonl", "harness.log", "client.log", "latest.log", "game.log", "*.png"]


def _harness(root, maxdepth=4):
    return {"type": "harness", "root": root, "maxdepth": maxdepth, "include": HARNESS_INCLUDE,
            "inline": ["result.json", "progress.jsonl"], "inline_max": 4_000_000,
            "exclude_dirs": ["game", "saves", "mods", "config", "crash-reports", "cache"]}


def _fp(root):
    return {"type": "featureplan_dirs", "root": root, "maxdepth": 3, "include": _FP_INCLUDE, "inline": _FP_INLINE,
            "exclude_dirs": ["game", "remote-slots", "saves"]}


DEFAULT_CONFIG = {
    "port": 8095,
    "poll_interval_s": 60,
    "live_interval_s": 5,
    "host_timeout_s": 60,
    "fetch_timeout_s": 240,
    "per_run_cap_mb": 30,
    "total_originals_budget_mb": 1500,
    "max_runs_per_host": 120,
    "hosts": {
        "ctl": {
            "label": "ctl (orbital-pve controller)", "platform": "Linux",
            "hardware": "Proxmox host, candidate distribution",
            "transport": {"type": "local"},
            "candidate_files": ["/root/minecraft-dev-lab-config/ctl/candidates/candidate.json"],
            "pause_files": [],
            "sources": [
                {"type": "c39", "root": "/root/minecraft-dev-lab-config/ctl/c39-validations", "maxdepth": 1,
                 "include": ["*.json"], "inline": ["*.json"], "host_override": "mac"},
                _harness("/root/mcdevlab-shared/harness/test-runs"),
                # Mac and GPC scenario runs from bin/mac-harness and bin/gpc-harness (their host comes from result.json).
                # Screenshots stay out of the mirror: the archive is several GB.
                dict(_harness("/root/mcdevlab-shared/harness/results-archive/mac", maxdepth=2),
                     include=["result.json", "progress.jsonl", "harness.log", "meta.json"]),
                dict(_harness("/root/mcdevlab-shared/harness/results-archive/gpc", maxdepth=2),
                     include=["result.json", "progress.jsonl", "harness.log", "meta.json"]),
                # L0 2026-11-02: fd-harness-large (accepted 02:45Z) and the coming ct224-harness-large archive here too;
                # their result.json host ("fd"/"ct224") maps to amd/proxmox-test via HOST_ALIASES.
                dict(_harness("/root/mcdevlab-shared/harness/results-archive/fd", maxdepth=2),
                     include=["result.json", "progress.jsonl", "harness.log", "meta.json", "controller-status.json"]),
                dict(_harness("/root/mcdevlab-shared/harness/results-archive/ct224", maxdepth=2),
                     include=["result.json", "progress.jsonl", "harness.log", "meta.json", "controller-status.json"]),
                {"type": "candidate_archive", "root": "/root/minecraft-dev-lab-config/ctl/archive", "maxdepth": 2,
                 "include": ["candidate.json"], "inline": ["candidate.json"]},
            ],
        },
        "proxmox-test": {
            "controller_archive": "/root/mcdevlab-shared/harness/results-archive/ct224",
            "controller_host": "ct224",
            "label": "proxmox-test (CT224)", "platform": "Linux", "hardware": "RTX 4060 Ti, Linux",
            "transport": {"type": "argv", "argv": ["pct", "exec", "224", "--", "python3", "-"]},
            "candidate_files": ["/root/mcdevlab-atlas/mcdevlab-results/candidate.json"],
            "pause_files": ["/etc/orbital-atlas-benchmarks-paused", "/root/minecraft-dev-lab/PAUSE"],
            "sources": [
                _fp("/root/cubeworld-runtime/feature-plan"),
                {"type": "smoke_v2", "root": "/root/mcdevlab-atlas/mcdevlab-results", "maxdepth": 2,
                 "include": ["smoke-*/summary.json", "smoke-*/wrapper.log", "candidate.json", "latest-smoke.json"],
                 "inline": ["smoke-*/summary.json", "smoke-*/wrapper.log", "candidate.json"]},
            ],
        },
        "amd": {
            "controller_archive": "/root/mcdevlab-shared/harness/results-archive/fd",
            "controller_host": "fd",
            "label": "amd (orbital-fd)", "platform": "Linux", "hardware": "Strix Halo, radeonsi, Linux",
            "transport": {"type": "argv", "argv": ["pct", "exec", "214", "--", "tailscale", "ssh", "root@orbital-fd", "python3 -"]},
            "candidate_files": ["/opt/orbital-atlas/results/latest-install.json"],
            "pause_files": ["/etc/orbital-atlas-benchmarks-paused"],
            "sources": [
                _fp("/opt/orbital-atlas/feature-plan/runs"),
                {"type": "fd_runs", "root": "/opt/orbital-atlas/results/runs", "maxdepth": 2,
                 "include": ["*/summary.json", "*/result-*.txt", "*/smoke-direct.log"],
                 "inline": ["*/summary.json", "*/result-*.txt"]},
            ],
        },
        "gpc": {
            "label": "gpc (Windows, RTX 4080)", "platform": "Windows", "hardware": "RTX 4080, Windows (owner desktop)",
            "owner_machine": True,
            "transport": {"type": "argv", "argv": [
                "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
                "user@<lan-ip>", "C:\\Python313\\python.exe -"]},
            "candidate_files": ["C:\\mcdevlab\\results\\installed.json"],
            "pause_files": ["C:\\mcdevlab\\PAUSE"],
            "sources": [
                {"type": "node_smoke", "root": "C:\\mcdevlab\\results", "maxdepth": 2,
                 "include": ["smoke-summary*.json", "dryrun-summary.json", "history/*.json", "latest.log"],
                 "inline": ["smoke-summary*.json", "dryrun-summary.json", "history/*.json"]},
                {"type": "instance_fp",
                 "root": "C:\\Users\\<user>\\OrbitalTest\\PrismLauncher\\instances\\McDevLab-Atlas\\minecraft\\orbitalatlas\\mcdevlab-feature-plan",
                 "maxdepth": 1, "include": ["*.txt", "*.png"], "inline": ["*.txt"]},
            ],
        },
        "mac": {
            "label": "mac (Apple M4 Pro)", "platform": "macOS", "hardware": "Apple M4 Pro, macOS (owner laptop)",
            "owner_machine": True,
            "transport": {"type": "argv", "argv": [
                "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-i", "/root/.ssh/mcdevlab_ed25519",
                "-o", "ProxyCommand=pct exec 214 -- nc <tailnet-ip> 22", "-o", "ConnectTimeout=15",
                "user@orbital-mac", "/Users/<user>/mcdevlab/python/bin/python3 -"]},
            "candidate_files": ["/Users/<user>/mcdevlab/results/installed.json"],
            "pause_files": ["/Users/<user>/mcdevlab/PAUSE"],
            "sources": [
                {"type": "node_smoke", "root": "/Users/<user>/mcdevlab/results", "maxdepth": 2,
                 "include": ["smoke-summary*.json", "dryrun-summary.json", "history/*.json", "latest.log"],
                 "inline": ["smoke-summary*.json", "dryrun-summary.json", "history/*.json"]},
                _fp("/Users/<user>/mcdevlab/results/feature-plan"),
                {"type": "instance_fp",
                 "root": "/Users/<user>/Library/Application Support/PrismLauncher/instances/McDevLab-Atlas/minecraft/orbitalatlas/mcdevlab-feature-plan",
                 "maxdepth": 1, "include": ["*.txt", "*.png"], "inline": ["*.txt"]},
            ],
        },
    },
}


def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def load_config(path=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = path or os.environ.get("DEVLAB_VIEWER_CONFIG")
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    # a host set to null in config.json is removed
    cfg["hosts"] = {k: v for k, v in cfg["hosts"].items() if v}
    return cfg
