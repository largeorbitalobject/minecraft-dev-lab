# Artifact, comparison and local release evidence

`mcdevlab.evidence` uses Python 3.11+ and the standard library. It never launches
Minecraft, runs a build, uploads a log, extracts a JAR, or publishes a release.
The surrounding API must authorize filesystem paths before calling it. Functions
do not modify input receipts. JSON output contains no non-finite numbers.

The supplied receipts are assertions, **not signed attestations**. An exact hash
prevents accidental mixing; it cannot prove that a caller truthfully measured a
run. Keep raw run logs, checker/protocol hashes, fixture manifests, process exits,
and source/build receipts beside these summaries. Preserve failed attempts and
use explicit supersession instead of overwriting them.

## Artifact inspection

```python
inspect_artifact(path: pathlib.Path) -> dict
```

The `mcdev.artifact.v1` result includes `path`, streamed `sha256`, actual `bytes`,
`status` (`PASS` or `FAIL`), and `archive`. It rejects nonregular files, symlinks,
files larger than 512 MiB, and files whose inode/size/mtime/ctime change while
inspecting. The handle and final pathname must still identify the same file.
Caller authorization and a final reinspection remain necessary before upload.

Archive inspection reads only these metadata members:

| Member | Selected fields |
| --- | --- |
| `META-INF/MANIFEST.MF` | Main-section version/title/commit and main class; continuation lines supported |
| `fabric.mod.json` | id, version, name, environment, selected dependency strings |
| `META-INF/mods.toml` | Loader/version/license and mod id/version/display name |
| `META-INF/neoforge.mods.toml` | Same TOML summary, labeled NeoForge |

No member is extracted or executed. Nested JARs are not inspected. Metadata is
informational: it does not prove compatibility, class validity, artifact signing,
dependency resolution, a successful build, or a native runtime result. An ordinary
valid JAR without loader metadata can pass this limited inspection.

Before `ZipFile` allocates entries, the central directory is validated and capped
at 4 MiB and 20,000 entries. Each of the four selected members is capped at 128 KiB
compressed and uncompressed; expansion above `max(4096, compressed_bytes * 200)`
is refused. Only stored/deflated metadata is supported. Duplicate selected names,
bad CRC, malformed selected metadata, ZIP64 (including hidden locators), ambiguous
end-record signatures inside comments, noncanonical directories, encrypted
metadata and multidisk archives fail closed. These are conservative inspection
limits, not a claim that all rejected files are malicious or invalid for Minecraft.
Hashing remains available when archive inspection returns `FAIL`. Invalid paths,
changing files and input-limit violations raise `ValueError` or an I/O exception.

## Matched repeated benchmarks

```python
compare_runs(baseline: list[dict], candidate: list[dict],
             thresholds: dict | None = None) -> dict
```

Each arm is a list of at most 256 `mcdev.benchmark.v1` runs. Every run requires:

```json
{
  "schema": "mcdev.benchmark.v1",
  "run_id": "baseline-1",
  "launch_id": "fresh-jvm-baseline-1",
  "pair_id": "pair-1",
  "status": "PASS",
  "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "evidence_kind": "cpu_component",
  "context": {
    "scene": "pose-and-workload-v1",
    "fixture": {"manifest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
    "settings": {"page_limit": 1024},
    "hardware": {"worker": "offline-example", "cpu": "example", "gpu": "none"},
    "toolchain": {"java": "21", "minecraft": "1.21.1", "loader": "example"},
    "timing_origin": "encoder-entry-to-return/thread-cpu",
    "protocol": "encoder-v1",
    "cache_state": {"jvm": "fresh", "filesystem": "warm", "warmups": 0}
  },
  "metrics": {
    "mesh_cpu_v1": {"value": 100, "unit": "ms", "direction": "lower", "measurement": "direct"}
  }
}
```

Run/launch/pair IDs and metric IDs contain 1–128 ASCII letters, digits, underscores,
dots, colons or hyphens. SHA-256 values contain exactly 64 lowercase hexadecimal
characters. `evidence_kind` is one of `cpu_component`, `file_io`, `game_runtime`,
`native_gpu`, or `visible_pixels`. `status` is explicitly `PASS`, `FAIL`, or
`NOT_RUN`; only completed `PASS` runs can contribute a comparison.

All eight context fields are mandatory, nonempty, and match exactly across every
run in both arms. Context must be finite JSON and fit in 64 KiB. Put camera/world
identity in `scene`, the content-hashed input identity in `fixture`, CPU/GPU and
resource limits in `hardware`, loader/JDK/dependency versions in `toolchain`, and
storage/cold-cache/warmup conditions in `cache_state`. The module compares declared
identities; it cannot discover omitted experimental confounders. Additional
details belong inside these objects, not extra top-level context fields.

Artifact hashes must be constant within each arm. `run_id` and `launch_id` must be
distinct across *both* arms. A baseline run and candidate run match by `pair_id`,
not list position, with exactly one of each. Record the actual alternating order
and restart/cooldown procedure in the external protocol and raw receipts. Many
frames from one launch are not many independent launches.

Each run has the same 1–64 semantic metric IDs. Version a metric ID when its
meaning changes. Every metric has exactly `value`, `unit`, `direction`, and
`measurement`. Values must be finite numeric scalars (booleans are not numbers).
Units are explicit, bounded identifiers, such as `ns`, `ms`, `bytes`, `KiB`,
`chunks/s`, or `%`; no implicit conversions occur. `direction` is `lower` or
`higher`, and `measurement` is `direct` or `proxy`. Metric set, unit, direction,
measurement and evidence kind must all match. Proxy metrics are refused even if
both arms use the same proxy. A directly counted service-call reduction can be
compared as calls, but it cannot become an actual-frame or visible-pixel claim.

One scalar represents a predeclared aggregate from one independent launch. CPU
time, wall time, process RSS, cgroup memory peak, actual rendered frames, and
pixels with chunk identity are different metrics. Never relabel an inert GL test,
queued/read/meshed count, modeled upload-frame lower bound, or source assertion
as native GPU or visible coverage evidence. Required byte/quality/parity gates
must be evaluated separately; performance cannot compensate for invalid output.

Optional threshold policy:

```json
{
  "min_pairs": 3,
  "metrics": {
    "mesh_cpu_v1": {"min_relative_improvement": 0.20, "min_absolute_improvement": 50},
    "bundle_wall_v1": {"max_regression": 0.10}
  }
}
```

The default minimum is three pairs; an explicitly selected minimum must be 2–256.
Unknown fields, misspelled metrics, negative thresholds and non-finite thresholds
are errors. Predeclare thresholds and repetition/stop rules **before** collecting
data; this function cannot verify when a supplied policy was chosen.

Per-pair absolute improvement is `baseline - candidate` for lower-is-better, or
`candidate - baseline` for higher-is-better. Relative improvement divides that
value by `abs(baseline)`. Thresholds apply to the **median paired improvements**:
minimum comparisons use `>=`, and maximum regression requires relative improvement
`>= -max_regression`. All configured gates must pass. Relative thresholds are
fractions: `0.20` means 20%. Absolute thresholds use the metric's declared unit.
A zero baseline produces no ratio; a required relative gate then fails instead of
inventing a percentage. Overflow is rejected even when individual inputs are finite.

The `mcdev.comparison.v1` output reports each matched pair, arm medians, median
absolute/relative improvement, absolute-improvement range and median absolute
deviation (MAD). These are descriptive statistics, not significance tests or
confidence intervals. A mismatched context, scope, reused launch, proxy, unfinished
run, missing pair or insufficient repetitions returns `status: NOT_RUN`,
`comparison_performed: false`, reasons in `issues`, and **no computed comparisons**.
Malformed schema or arithmetic is a `ValueError`.

Valid comparisons return `PASS` or `FAIL` for the configured thresholds. Without
thresholds they return comparison `PASS` but `threshold_status: NOT_RUN` and
explicitly make no efficacy claim. An early-stopped single pair cannot pass the
planned repeated comparison; preserve the separate early-cutoff failure receipt.
Never silently rerun a losing arm or discard outliers after seeing the results.

## Local release checklist

```python
prepare_release(artifact: dict, checks: list[dict], required: list[str]) -> dict
```

`artifact` is an `inspect_artifact` receipt. A check has this form:

```json
{
  "name": "build:jar",
  "evidence_kind": "build",
  "status": "PASS",
  "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Required names are scope-qualified, for example `build:jar`, `source:review`,
`component:decoder`, `comparison:rejoin`, and `visible_pixels:frozen453`. The prefix
must equal `evidence_kind`. Supported prefixes are the five benchmark kinds plus
`source`, `build`, `unit`, `component`, and `comparison`. A source check named
`visible_pixels:frozen453` is rejected. Unqualified names are rejected rather than
assigned an inferred scope.

The caller chooses 1–256 requirements; this is not a universal release policy.
At most 256 receipts are accepted. Duplicate names are rejected: resolve which
receipt supersedes another explicitly. Missing requirements become `NOT_RUN`.
Wrong artifact hashes become `FAIL`; matching source commit names alone do not
suffice. Required failed checks or failed archive inspection produce `FAIL`;
otherwise missing/unrun checks produce `NOT_RUN`. Only all required `PASS` checks
on the exact artifact yield `status: PASS, readiness: READY`. Unused check names
are disclosed but do not change the caller-selected policy.

`mcdev.release.v1` always returns `published: false` and
`publication: NOT_REQUESTED`. READY means only that this local checklist is
satisfied. Source/build checks do not imply native, performance, compatibility or
visible-coverage results. Receipts can be forged by their supplier; no runner or
signature authentication is performed. Publishing requires a separate reviewed
target/version/visibility decision and reinspection of the actual upload bytes.
This function performs no filesystem or network actions.

## Private local crash hints

```python
redact_log(text: str) -> str
```

Produces useful diagnostic text for an **authorized** local or remote development
route. It examines at most 64 Ki characters, adds an explicit truncation marker
when the input is longer, strips common ANSI controls, and masks common
`--accessToken`, `access_token`, `token` and `Authorization` Bearer/Basic forms.
Quoted JSON/properties, launcher argument lists and ordinary unquoted values are
handled. Entire `[CHAT]` and recognized vanilla player-chat lines are removed,
including player names. Compiler error lines, source locations and stack traces
remain available. The node/API may impose a smaller byte-based tail limit.

This is **best-effort redaction, not complete PII or credential sanitization**.
Paths, usernames outside recognized chat lines, IP addresses, unrecognized
credential names, multiline secrets, and values whose field names were cut off
at the start of a tailed log may remain. Treat the output as sensitive diagnostics
and restrict its route accordingly. The helper performs no I/O or uploads; the
calling diagnostic tool controls where its return value is sent.

```python
triage_log(text: str) -> dict
```

Examines at most two million characters and returns `HEURISTIC_ONLY`, categories,
match counts and fixed generic next steps. Recognizes common memory, Java-version,
binary API, missing-class, mixin, loader, graphics and watchdog patterns. It emits
**no source excerpts**, exception messages, stack class names, paths, addresses,
tokens or player identifiers. This avoids pretending regex replacements can
sanitize every possible secret format. Nothing is uploaded. False positives and
missed causes are possible; no findings does not mean a healthy runtime.

## Validation and examples

Run the offline tests with:

```sh
python3 -m unittest discover -s tests -p test_evidence.py -v
```

`examples/benchmark-baseline.json`, `benchmark-candidate.json`, and
`benchmark-thresholds.json` are **synthetic schema examples**, not Orbital Atlas
measurements or a Minecraft performance claim. Load the first two as arrays and
the third as the optional policy passed to `compare_runs`.

The design follows actual local evidence failures: a built JAR whose native run
was held remains build-only evidence; a 1-pair CPU rejection cannot be turned into
a 3-pair median pass; reduced service calls cannot replace a frozen pixel/depth/
chunk-identity denominator; and a source-only test with an inaccurate stub cannot
stand in for compilation against the real game API.
