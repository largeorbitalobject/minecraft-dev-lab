# Skill forward check: GPC timeout and map reopening

Prompt under review: **“Use GPC run and reopen map after transport timeout.”**

This is a source-level walkthrough of the bundled `skills/minecraft-dev/SKILL.md`
and the actual public `LabAPI.list_tools()` metadata. It does not invoke any node,
native adapter, process recipe, or game operation.

Expected decisions:

1. Read configured endpoints with `fleet_list`. `host_probe` establishes the GPC node
   identity and `instance_inspect` exposes its enabled instance and named recipes.
   An expected NVIDIA label does not establish the actual rendering device.
2. Identify which request timed out. If it was `run_start`, retain the original
   request ID and call `run_status` with that ID in `job`; a missing returned job
   handle does not justify another launch. If it was `game_action`, call
   `game_action_result` with the original request ID. The two result tools recover
   different durable ledgers and are not interchangeable.
3. If the result remains unknown, report that the original operation may have
   happened and preserve ownership. Do not start another run, invent another action
   ID, reopen the map, or substitute desktop input. A status observation from a new
   process/session cannot reconcile an older action's effect by itself.
4. If the configured route is the finite Atlas recipe, let the existing controller
   complete its predefined batch and cleanup; do not issue a concurrent generic
   game action. The direct Atlas `json-stdio` adapter is currently unavailable.
   A subsequent authorized finite scenario needs its own prepared native session.
5. If a separately configured game provider supports the intended operation and
   the original action is definitively resolved, obtain a fresh `status` through
   `game_action`, preserve its actual session/world/frame/time witness, and use the
   existing active `owner_job` where applicable. Only then can a new, intentional
   action be distinguished from replay of the timed-out one. The new action needs
   its own recorded ID and the provider's existing native authority.

The skill and metadata already direct recovery by original ID, retain `UNKNOWN`
ownership, require fresh witnesses, and prohibit keyboard/mouse fallback. The
important practical clarification is that `status` is an advertised provider
operation inside `game_action`; there is no public tool named `game.status` or
`game.command`. Likewise `run_status.job` accepts the original run request ID, and
`game_action_result` only accepts an action request ID. These mappings should remain
explicit in future skill revisions.

Verdict: the current guidance avoids replay in this walkthrough. This check is
not an autonomous agent evaluation, native gameplay trial, or proof that every
future model will follow the guidance.
