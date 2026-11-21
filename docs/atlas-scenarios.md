# Use the existing Atlas controller as one finite scenario

The plugin can supervise the existing Atlas controller as a named recipe without
copying its launch, desktop ownership, input, or cleanup implementation. This route
does not need a `json-stdio` game provider. It exposes a **whole predefined scenario**,
not an interactive `game_action` endpoint.

`examples/atlas-scenario.windows.json` is a disabled reference node configuration.
`OPERATOR_CONFIGURED` and `PREPARED_SESSION_UUID` are deliberate placeholders. The
guard executable is an interface slot, not a shipped or verified guard. Nothing in
this example prepares a session, changes launcher settings, or grants control.

## Verified command shape

The inspected controller's actual argument parser accepts:

```text
python atlas_harness.py run-owned DIRECTORY GRANT NATIVE_CONFIG ACTION [ACTION ...]
python atlas_harness.py run DIRECTORY GRANT BINDING ACTION [ACTION ...]
python atlas_harness.py reconcile DIRECTORY
```

Allowed actions are exactly `status`, `screenshot`, `open_map`, and `close_map`.
The example uses this finite batch:

```text
status open_map screenshot close_map
```

`run-owned` performs the existing setup/launch readiness checks, waits for an
operator to enter the approved disposable world, executes the batch, attempts its
existing menu Save & Quit and normal application exit, then restores its settings.
It reserves requests and time for cleanup. It does not navigate into the world
automatically. `run` instead adopts an explicitly granted concrete process/window
binding; its `finally` revokes the controller lease, and it does **not** perform the
owned launch/save/exit/restore lifecycle. Do not substitute one form for the other.

The CLI deliberately stops its session when a run finishes. A second scenario needs
the existing controller's fresh session and authorization procedure; reusing the old
directory/grant is not an interactive continuation. Read-only `reconcile DIRECTORY`
inspects its existing journal and does not reacquire input authority. It may be
configured separately as an `inspect` recipe, using the same desktop resource and
an operator-selected directory, after the original plugin job has ended.

## Before enabling the reference recipe

Use the existing permitted delivery and native-controller procedure to establish:

1. The actual Windows node and reviewed controller assembly. Replace placeholder
   executable/script paths with that installation; do not use a source-audit packet
   as proof of an approved live deployment. No checksum is supplied here as a claim
   about a future installation.
2. A disposable instance marked by the existing controller, a fresh prepared session
   directory, the exact installed artifact identity, and a matching unexpired native
   desktop grant. Grant permissions must cover the configured batch and the
   controller's owned setup/launch/normal-close/restore operations. The original
   controller checks actual user/session/desktop/integrity and process/window identity.
3. The reviewed native configuration and the same approved disposable world. In the
   audited `FixedLaunch`, the instance is hardcoded to
   `C:\Users\<user>\AppData\Roaming\ModrinthApp\profiles\Atlas-GPC-QA-cac7ce3` and
   the world to `Atlas GPC compat 1266031`; the Modrinth launch URI and jcmd path/hash
   are also fixed in source. Merely replacing those paths in this example does not
   retarget the controller. Another profile requires the existing adapter's reviewed
   change process. Its native CLI refuses Linux and macOS.
4. An existing approved admission guard matching the plugin's guard contract: consume
   the JSON request `{"op":"guard","kind":"client"}` on stdin and return a bounded
   JSON object with `"allowed": true` only when the real machine's current policy
   permits this scenario. A static allow-all script is not an occupancy, desktop,
   resource, artifact, or native-lease check. Preserve the controller's own checks.
5. Exclusive use of `gpc-desktop` for every configured job/provider touching that
   desktop. Enable only this named instance after its routes and current authority
   are established. Keep launcher account data, owner tokens, and grant contents on
   the node; do not paste them into MCP arguments.

The outer 960-second job deadline is longer than the controller's maximum
900-second session; it does not extend the native grant. The native controller may
refuse earlier to preserve cleanup. Plugin cancellation, a failed guard, or the
outer deadline stops the owned recipe process group/job; it is **not** an alternate
Save & Quit implementation. In particular, a URI launch can be serviced by an
existing Modrinth process outside the recipe's descendant tree. The plugin cannot
infer ownership or exit of that game from its controller process exiting. Retain
the existing native controller's process binding, cleanup receipt, and operator
recovery procedure whenever normal cleanup is unconfirmed.

## Dispatch and uncertain transport

After `fleet_list`, `host_probe`, and `instance_inspect` establish the configured
endpoint and named recipe, submit one `run_start` with a recorded stable request ID:

```json
{
  "host": "gpc",
  "instance": "atlas-disposable",
  "recipe": "atlas-map-scenario",
  "request_id": "atlas-map-scenario-unique-run-id"
}
```

If the transport times out, query `run_status` using that **original request ID**
in its `job` field. Do not create another ID to run/reopen the map. Once a job ID is
known, retain both IDs. `run_logs` returns bounded diagnostic triage; raw controller
responses remain in the node's local job output and controller session journal.
Process `PASSED` means exit zero, not proof of every action's success: the controller
can return a list containing a refused action. Verify the actual action responses,
fresh screenshot evidence, saved-title/process-exit proof, and cleanup state before
claiming the scenario passed. Generic plugin job receipts do not parse or replace
those Atlas-specific proofs.

An `UNKNOWN` plugin job or unknown native action retains uncertainty. Do not clear
its durable claim, synthesize an unlocked desktop, create a replacement grant, or
fall back to mouse/keyboard input to finish the sequence. Inspect the recorded result
and use the existing owner's reconciliation procedure. An independently authorized
later scenario is distinct from retrying the uncertain action.

## Evidence scope

The CLI schema and lifecycle above were inspected in
`/tmp/atlas-game-harness-controller-r4-review-20261004/atlas_harness.py` and its
`fixed_windows_launch.py`, `windows_actions.py`, and `menu_cleanup.py` dependencies.
The matching controller file at
`/root/mc-client/controller-properties-decoder-r2-20261004/atlas_harness.py` has the
same audited SHA-256 recorded in `docs/bridges.md`. Existing saved receipts reported
source closure and a Java build while explicitly leaving native acceptance unrun.
This example has been parsed and checked against that actual argument parser; no
Windows process, launcher, Minecraft client, or OS input was executed to validate it.
