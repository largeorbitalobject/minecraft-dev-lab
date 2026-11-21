---
name: minecraft-dev
description: Develop and test Minecraft mods with configured Minecraft Dev Lab hosts, owned build/client jobs, game-bridge capabilities, artifact fingerprints, comparable performance runs, and release evidence. Use for mod development or multi-machine Minecraft testing, not ordinary gameplay without a configured test instance.
---

Use the bundled Minecraft Dev Lab MCP to identify a host and named instance before acting. `fleet_list` is configuration only; `host_probe` establishes current endpoint identity. GPU labels in configuration are expectations, not proof of the active renderer. `instance_inspect` reads available recipes and loader components.

For builds and tests, select an existing configured recipe. `run_start` returns a durable handle. Record its request ID before dispatch; after transport failure query that original ID in the `job` field of `run_status`. Do not create a new ID to repeat an uncertain operation. A stale heartbeat or `UNKNOWN` retains ownership. `run_cancel` requests cancellation; verify terminal status before starting a replacement. Build exit zero proves only the configured process result, not successful world entry or visual quality.

For playing and native tests, inspect `bridge_capabilities`. Only advertised operations on the verified provider are supported. Call the advertised `status` operation through `game_action` to get a fresh status witness before an action and preserve its session, world, frame and observation time. If a plugin-owned client is running, supply its `owner_job`. After an uncertain action use `game_action_result`; do not replay or substitute keyboard/mouse automation. Honor existing user authorization and the machine's actual occupancy/resource guards. Installing this plugin does not grant desktop or game control.

Use `artifact_inspect` for exact JAR identity and loader metadata. `benchmark_compare` requires matching context and direct measurements; keep CPU components, native GPU times and actual visible coverage distinct. Insufficient independent pairs and mismatched scenes are inconclusive. `release_check` is a local readiness report; preserve missing `NOT_RUN` checks and exact artifact bindings. Uploading a release is a separate user-requested action.

Setup and existing adapter boundaries are documented in the bundled `docs/` and README. Host configuration contains command argv and local paths, not MCP-supplied shell text. Configure only existing permitted routes. Mac, Windows and Linux execution must be validated on those hosts; a simulated provider or Linux test is not cross-platform native evidence.
