# Minecraft Dev Lab

I built this so AI coding agents could test a Minecraft mod on real game clients across four machines (Linux/AMD, Windows/RTX 4080, Apple Silicon, and an RTX 4060 Ti container) and get measurements I could trust. Agents reach it as an MCP server. It is the evaluation harness behind my 3D map mod, and it ran thousands of native client runs.

The rules it enforces matter more than the code:

- Every build is pinned by hash. A result is bound to the exact JAR it measured.
- A/B comparisons are interleaved (ABBA), with n of at least 3 and an A/A noise baseline. Each machine is compared only with itself.
- Visual changes pass screenshot gates at fixed camera stations, checked mid-load and while the camera rotates.
- A test must reproduce the recorded bug before it is allowed to judge the fix.
- "PASS" is not trusted until the result file has been read.
- Machines are leased, so two agents never drive the same client at once.

This repository holds:

- the MCP plugin itself (`mcdevlab/`, `docs/`, `examples/`, `tests/`), version 0.1.0;
- `perfsuite/`, the per-release benchmark suite (scenario generation, run, score, summarize);
- `viewer/`, a small web app for browsing and comparing runs;
- `orchestrator/`, the multi-machine dispatcher.

Machine addresses and account names are replaced with placeholders. Use `examples/` and `viewer/config.example.json` as templates. Game assets, worlds, run archives and frames are not included.

## The plugin

An installable MCP plugin for developing mods across a fleet of Minecraft test machines. Python 3.11 or newer; no pip packages, Node, cloud account, exposed ports or game credentials required.

The first release provides named hosts and instances, guarded build/test/client recipes, exclusive resource ownership, durable job and action IDs, bounded diagnostic logs, JAR fingerprints and loader metadata, matched benchmark comparisons, and local release readiness. It includes a typed gameplay-provider interface. A provider must actually support a command before the plugin sends it.

## Install

Build the distributable, or unpack the provided release ZIP:

```sh
python3 scripts/package.py
```

Unpack `dist/minecraft-dev-lab-0.1.0.zip` and run the following installer from its
`minecraft-dev-lab-0.1.0/` directory. That directory includes the self-contained
`minecraft-dev-lab.pyz`; the source checkout's installer alone is not the bundle.

Copy `examples/fleet.json` to a private location. It starts with four disabled host slots: Mac, Proxmox test container, GPC and AMD. Configure one host before enabling it. Then:

```sh
python3 install.py --dest /absolute/path/minecraft-dev-lab --config /absolute/path/fleet.json
```

On Windows use `py -3` and absolute Windows paths. The installer prints the generated MCP configuration and marketplace location. Register that local marketplace in your client, or use the generated JSON/TOML stdio server configuration. The installer never overwrites global Codex settings or starts a game. Keep the installation destination in place: generated commands use its absolute paths.

The packaged server also has a CLI:

```sh
python3 minecraft-dev-lab.pyz call --config /absolute/path/fleet.json fleet_list
python3 minecraft-dev-lab.pyz serve --config /absolute/path/fleet.json
```

## One node per machine

Put the same `.pyz` on each permitted machine. Copy and edit `examples/node.json`: set a private local `state_dir`, instance roots and explicitly approved recipe argv. Enable the desired instances. No daemon or listening port is needed; the controller invokes the node once per request using an existing local, SSH or Proxmox route.

Each fleet host specifies `enabled`, `node_id` and a fixed `argv`. For a local node:

```json
{
  "enabled": true,
  "node_id": "local-test",
  "argv": ["/usr/bin/python3", "/opt/mcdev/minecraft-dev-lab.pyz", "agent", "--config", "/private/node.json"]
}
```

An existing Proxmox route can use `pct exec 224 -- python3 /path/to/minecraft-dev-lab.pyz agent --config /path/to/node.json` as an argv array. Existing SSH routes can use a fixed remote command with `BatchMode=yes` and `StrictHostKeyChecking=yes`; quote the remote command for that host's actual shell. The plugin never enrolls hosts, changes trust, installs SSH or copies credentials. Windows Python and macOS Python are selected explicitly in that host's configured route.

`state_dir` and instance `root` may be absolute or relative to the node configuration file. Fleet workspace paths are relative to the fleet configuration file. Different machines have separate paths. Keep one node state directory per machine so every controller sees the same leases.

Recipes have fixed command arrays, cwd within the instance root, kind, resource, timeout and optional exact artifact paths. Commands are administrator-authored configuration; tools cannot supply shell code. Give mutually exclusive builds and clients the same resource name. On Proxmox use the existing guarded build/client wrapper; do not replace its policy with a generic process launcher. A client recipe requires a configured live guard. The guard receives one JSON request on stdin and must return `{"allowed":true}`; denial, failure or malformed output blocks/stops the owned run. It runs before launch and periodically during the run.

## Daily workflow

1. Discover hosts and inspect a named instance. Compare actual component versions and mod filenames.
2. Start its build recipe with a stable request ID. Poll its job handle; read the redacted diagnostic tail on failure.
3. Inspect the built JAR. Verify exact hash and metadata before delivering it to a test instance.
4. Start an approved client recipe, then use only a configured provider's advertised game operations. Record completed scene/frame witnesses.
5. Compare matched independent runs and prepare a release readiness report bound to that JAR. Missing native tests remain `NOT_RUN`.

Use `run_cancel` for the exact job handle. A supervisor owns its process group on Linux/macOS or Windows Job Object, with a gate before recipe subprocess creation. Timeout, output limits and guard failures stop that owned tree. If cleanup is uncertain, the lease remains reserved; an operator must reconcile the host. The plugin does not kill arbitrary Java processes or reclaim uncertain ownership automatically.

## What is verified

See `VALIDATION.json` in the release for exact local tests and limitations. The Linux tests use real subprocesses, protocol messages, isolated fake providers and generated fixture JARs. They do not exercise Minecraft rendering. Windows/macOS test jobs are provided in CI and remain unverified until actually run there. Existing Atlas and companion code are not silently installed or replaced. Read [bridge integration](docs/bridges.md) and [evidence formats](docs/evidence.md).

This is a private/local plugin distribution. It does not publish mods or upload source, logs, worlds or benchmark evidence to third parties. Release tracking means preparing an evidence-backed local decision; Modrinth/CurseForge/GitHub publication requires a separate configured integration and user instruction.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 scripts/package.py
```

Packaging follows the supported [Codex compatibility manifest](https://developers.openai.com/plugins/build/plugins). The stdio server supports the explicitly tested [2025-11-25 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) and [2026-07-28 protocol](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning).

## License

Apache License 2.0. See `LICENSE`. Minecraft is a trademark of Mojang; no game code or assets are included.
