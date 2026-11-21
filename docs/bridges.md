# Game bridge providers

The plugin keeps game controls behind an operator-configured provider. It does not
send desktop input, attach to a game process, import a launcher account, or silently
replace an existing controller. `inspect_bridge(config)` reads configuration and
verifies file hashes without executing anything. A configured provider is not proof
of a running game or a successful native test.

`invoke_bridge(config, request)` sends one JSON request to the exact configured
argument vector through stdin. The enclosing API must own an exclusive instance
lease and persist the request ID before calling it. A timeout after dispatch means
unknown outcome. It must never cause an automatic replay.

## Configuration

Configuration belongs to the operator, outside MCP request parameters. A minimal
provider entry is:

```json
{
  "kind": "json-stdio",
  "argv": ["/absolute/python3", "/absolute/provider.py"],
  "cwd": "/absolute/provider-directory",
  "platforms": ["linux", "macos"],
  "pins": [
    {"path": "/absolute/python3", "sha256": "<executable-sha256>"},
    {"path": "/absolute/provider.py", "sha256": "<provider-sha256>"}
  ],
  "manifest": {
    "protocol": "mcdevlab.bridge/1",
    "provider_id": "my-reviewed-provider/1",
    "operations": {
      "status": {"effect": "observe"},
      "open_map": {"effect": "action", "description": "Open the map in the current world"}
    }
  },
  "timeout_seconds": 10,
  "output_limit_bytes": 65536,
  "freshness_ms": 5000
}
```

The executable and directly named file arguments must be pinned. Include imported
provider modules in `pins` as well: a script hash is not a dependency closure.
Pins are checked before and after execution. They detect drift but do not sandbox
trusted operator code or make concurrent hostile file replacement safe. Providers
run with the node account's permissions and inherited environment. Use a dedicated
test account and a provider with an explicit output allowlist; do not emit tokens,
account files, raw command lines, or unrelated logs.

Only declared operations dispatch. Command arguments travel as JSON data, never
as shell fragments or extra executable arguments. The provider is responsible for
validating each operation's argument fields and bounds before input. Providers must
not start a long-lived game as an untracked child of this one-shot request: game
launch and lifetime belong to the configured managed-job supervisor.

Provider children use the common owned-process supervisor: a separate process group
on POSIX and a kill-on-close Job Object on Windows. A bundled exec shim waits on a
private gate before starting the configured provider, so the Windows Job assignment
precedes any provider descendants. Failure to establish ownership refuses dispatch.
This shared process boundary does not establish native Minecraft control on any
platform. Windows and macOS execution remain unverified until their native test
runners produce evidence. Inspection remains portable and does not start a provider.

## Wire protocol

One UTF-8 JSON object followed by a newline is written to stdin, followed by EOF.
The request is at most 64 KiB. Stdout must contain exactly one JSON object and no
logging; the process must exit successfully within 0.1–30 seconds. Stdout is bounded
by configuration, at most 256 KiB; stderr is capped at 8 KiB and discarded. Duplicate
keys, nonfinite numbers, malformed JSON, nonzero exit, and oversized output fail.

```json
{
  "protocol": "mcdevlab.bridge/1",
  "provider_id": "my-reviewed-provider/1",
  "request_id": "d05ee98c-c1bf-4df6-85ca-061aec427a09",
  "operation": "open_map",
  "arguments": {},
  "expected": {
    "session_id": "actual-client-boot-identity",
    "world_id": "actual-server-world-dimension-identity",
    "frame_id": 123,
    "observed_at_ms": 1791140000000
  },
  "issued_at_ms": 1791140000100,
  "deadline_ms": 1791140010100
}
```

`status` uses `expected: null`. Every game action requires a recent expected witness
from a prior status call. Before any effect the provider must check that the actual
session/world still match, the deadline has not expired, and its own control grant
is valid. It must preserve request identity through its native backend and reject
unknown duplicate request IDs rather than replay them. The host's durable request
ledger is also mandatory; this module alone does not supply exactly-once effects.

A reply contains exactly `protocol`, `provider_id`, `request_id`, `operation`,
`status`, `code`, `before`, `after`, and `result`. The first four match the request.
`status` is `ok` or `rejected`; `code` is a bounded uppercase category; `result` is an
object. Both witnesses use the same four fields as `expected`. A status observation
may have `world_id: null` at the title screen; game actions require a concrete world.

For a successful game action, before/after must describe the same actual session
and world, and after must identify a later **completed rendered frame**. A current
requested camera, an input submission counter, an arbitrary wall-clock counter,
or a cached READY file is not a frame witness. A provider must verify the requested
postcondition in `result` before returning `ok`; a later frame alone does not prove
that the map opened or an item was transferred. The host verifies the envelope and
witness consistency, not the truth of untrusted provider assertions. A `rejected`
reply means no effect was submitted and must still carry actual fresh witnesses.
If input may have happened, do not claim rejection: terminate without an affirmative
receipt and preserve the backend's journal for reconciliation.

Actions intentionally changing world/session, title-menu actions, OS input, client
launch, and normal save/exit require separate explicit capabilities and are outside
this initial same-world game-command contract. They must not be disguised as map
commands. The enclosing service exposes configured managed launch/test jobs separately.

Normalized host outcomes distinguish `observed`, provider-attested `verified`,
`refused`, `unknown`, and `not_dispatched`. Failed action receipts after a provider
starts remain `unknown`, including identity/frame mismatch and provider code drift.
Neither process exit nor command submission is reported as successful game behavior.

## Existing implementation boundaries, audited 2026-10-04

* `/tmp/atlas-game-harness-controller-r4-review-20261004/atlas_harness.py`,
  `Controller` lines 194–415, already owns a disposable instance, artifact identity,
  durable intent journal, one worker claim, sequence limits, leases, exact native
  process/window binding, deadlines, and no-replay handling. Its CLI at lines 524–630
  runs an entire authorized lifecycle and stops its control session on exit. `run`
  and `run-owned` are not persistent command endpoints. They explicitly refuse
  non-Windows platforms, and the native context schema includes Windows station,
  desktop, and integrity fields.
* `/root/mc-client/GPC-GAME-HARNESS-MAC-INTEGRATION-BOUNDARY-20261004.json`
  records missing Mac lock/provider/launch/normal-quit integration and states that
  the existing Java camera is requested state, not completed-frame identity.
  `/root/mc-client/GPC-GAME-HARNESS-SOURCE-CLOSED-20261004.json` records source
  closure and a Java build while explicitly retaining `ALL_SIX_NOT_RUN` native
  status. Source inspection is not native acceptance.
* Therefore `kind: atlas-controller` explicitly reports
  `ATLAS_CONTROLLER_ADAPTER_NOT_IMPLEMENTED`. A future adapter must use its existing
  owner and journal, expose actual fresh frame evidence, and integrate real platform
  providers. Copying the controller or invoking `run` for each MCP call would create
  competing ownership or reuse an expired one-shot grant.
* `/root/minecraft-companion/companion.py`, `Bridge` lines 12–62, is a distinct local
  JSON-file protocol with canonical UUID requests, fresh per-world sessions, atomic
  inbox publication, bounded receipt wait, and unknown-outcome handling.
  `runtime/client.py` adds a 1.5-second generation/revision control lease and a
  durable gameplay action ledger. Its default live scope is a particular test server.
  Do not translate this into arbitrary shell or desktop commands or discard its
  gameplay ownership and material reconciliation rules. It is not automatically a
  provider for the protocol above. Its MIT license requires preserving attribution
  if its implementation is incorporated; none is copied here.
* `/root/mc-client/guard.py` is a host-specific Proxmox policy including production
  occupancy, pressure quarantine, and measured CPU busy limits. The older README
  describes superseded machine capacity and policy. Discover current capabilities
  from each configured node and preserve its existing guard instead of distributing
  copied hardcoded host thresholds as a universal policy.

The new tests use actual subprocess execution of temporary deterministic providers
and exercise hashes, deadlines, output limits, stale sessions, completed-frame
requirements, process cleanup, and unknown outcomes. They do not start Minecraft,
send OS input, or establish native behavior on any GPU.

Audited source identities (SHA-256, not a live deployment assertion):

| Source | SHA-256 |
| --- | --- |
| Atlas controller r4 `atlas_harness.py` (also identical in `controller-properties-decoder-r2-20261004`) | `ae5a84c20a51dd07018405c98d778ae42b1d76dfba9e785600862a12980eb1ca` |
| Companion `companion.py` | `d7aafb5774a90ed7f7f6422aac4d346a9121a2a411229b8c20d7755f9946fafe` |
| Companion `runtime/client.py` | `054c2a8337d9b1e32c1e56b669366a4cec76aec833805be5b0028e2ca2076e58` |
| Host `mc-client/guard.py` | `827b707cfd35e0cce5bb9a1c7a51a9b5ea1354d4790abe77b1aaaf95b046ff64` |
