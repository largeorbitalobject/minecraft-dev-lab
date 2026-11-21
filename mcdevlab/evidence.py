"""Offline, hash-bound Minecraft build and performance evidence (stdlib only).

These functions do not run builds, launch a game, upload logs, or publish releases.
Receipts are assertions supplied by callers, not cryptographic attestations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import tomllib
import zipfile
import zlib

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_DIRECTORY_BYTES = 4 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_METADATA_BYTES = 128 * 1024
MAX_RUNS = 256
MAX_METRICS = 64
MAX_LOG_CHARS = 2 * 1024 * 1024
MAX_REDACT_CHARS = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
CONTEXT_KEYS = (
    "scene", "fixture", "settings", "hardware", "toolchain", "timing_origin",
    "protocol", "cache_state",
)
BENCHMARK_KINDS = frozenset({"cpu_component", "file_io", "game_runtime", "native_gpu", "visible_pixels"})
CHECK_KINDS = BENCHMARK_KINDS | {"source", "build", "unit", "component", "comparison"}
_METADATA = ("META-INF/MANIFEST.MF", "fabric.mod.json", "META-INF/mods.toml", "META-INF/neoforge.mods.toml")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a 1..128 character identifier")
    return value


def _finite(value: object, label: str) -> float:
    if type(value) not in (float, int):
        raise ValueError(f"{label} must be a finite number, not a boolean")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{label} is too large") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    # Avoid overflowing (a + b) / 2 for otherwise finite inputs.
    return ordered[middle] if len(ordered) % 2 else ordered[middle - 1] / 2 + ordered[middle] / 2


def _canonical(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("Context must contain finite, JSON-compatible values") from error
    if len(encoded) > 65536:
        raise ValueError("Context exceeds 64 KiB")
    return encoded


def _file_identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _zip_bounds(stream, size: int) -> None:
    """Bound the central directory *before* ZipFile allocates its entries.

    ZIP64 and multi-disk archives are deliberately unsupported, not extracted.
    """
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    # An EOCD signature can also occur in its own comment. Only the terminal
    # record with the exact declared comment length is a candidate.
    position = len(tail)
    while True:
        position = tail.rfind(b"PK\x05\x06", 0, position)
        if position < 0:
            raise ValueError("Missing bounded ZIP end record")
        if len(tail) - position >= 22:
            fields = struct.unpack_from("<4s4H2IH", tail, position)
            if position + 22 + fields[-1] == len(tail):
                break
    _, disk, directory_disk, disk_entries, entries, directory_size, offset, _ = fields
    # ZipFile chooses the last EOCD signature, even if its comment length does
    # not reach EOF. Bind its parser to the same record that we just bounded.
    if tail.rfind(b"PK\x05\x06") != position:
        raise ValueError("Ambiguous ZIP end record in comment")
    if disk or directory_disk or disk_entries != entries:
        raise ValueError("Multi-disk ZIP is unsupported")
    if entries == 65535 or directory_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise ValueError("ZIP64 inspection is unsupported")
    eocd_offset = size - len(tail) + position
    if eocd_offset >= 20:
        stream.seek(eocd_offset - 20)
        if stream.read(4) == b"PK\x06\x07":
            # ZipFile consults this locator even without ZIP64 sentinel fields.
            raise ValueError("ZIP64 locator inspection is unsupported")
    if entries > MAX_ZIP_ENTRIES or directory_size > MAX_DIRECTORY_BYTES:
        raise ValueError("ZIP central directory exceeds inspection bounds")
    if offset + directory_size != eocd_offset:
        raise ValueError("Noncanonical ZIP directory or ZIP64 is unsupported")
    stream.seek(offset)
    directory = stream.read(directory_size)
    cursor = 0
    counted = 0
    while cursor < len(directory):
        if directory[cursor:cursor + 4] != b"PK\x01\x02" or cursor + 46 > len(directory):
            raise ValueError("Invalid ZIP central directory")
        lengths = struct.unpack_from("<3H", directory, cursor + 28)
        cursor += 46 + sum(lengths)
        counted += 1
        if counted > MAX_ZIP_ENTRIES:
            raise ValueError("ZIP entry count exceeds inspection bound")
    if cursor != len(directory) or counted != entries:
        raise ValueError("ZIP directory count or length mismatch")
    stream.seek(0)


def _selected_text(data: dict, names: tuple[str, ...]) -> dict:
    return {key: value[:1024] for key in names
            if isinstance((value := data.get(key)), str)}


def _metadata_member(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ValueError("Encrypted or unsupported metadata compression")
    if entry.file_size > MAX_METADATA_BYTES or entry.compress_size > MAX_METADATA_BYTES:
        raise ValueError("Metadata member exceeds 128 KiB")
    if entry.file_size > max(4096, entry.compress_size * 200):
        raise ValueError("Metadata expansion ratio exceeds inspection bound")
    with archive.open(entry) as member:
        payload = member.read(MAX_METADATA_BYTES + 1)
    if len(payload) > MAX_METADATA_BYTES or len(payload) != entry.file_size:
        raise ValueError("Metadata member size mismatch")
    return payload


def _inspect_zip(stream, size: int) -> dict:
    _zip_bounds(stream, size)
    result = {"status": "PASS", "loaders": [], "metadata": {}, "errors": [],
              "scope": "Selected loader metadata only; no class, signature, dependency or nested-JAR verification"}
    with zipfile.ZipFile(stream) as archive:
        entries = archive.infolist()
        result["entries"] = len(entries)
        for name in _METADATA:
            matches = [entry for entry in entries if entry.filename == name]
            if len(matches) > 1:
                raise ValueError("Duplicate loader metadata member")
            if not matches:
                continue
            payload = _metadata_member(archive, matches[0])
            try:
                text = payload.decode("utf-8")
                if name.endswith(".MF"):
                    # Manifest continuation lines join to the preceding field.
                    lines = text.replace("\r\n", "\n").split("\n")
                    joined: list[str] = []
                    for line in lines:
                        if not line:
                            break  # Main attributes only, not per-entry sections.
                        if line.startswith(" ") and joined:
                            joined[-1] += line[1:]
                        else:
                            joined.append(line)
                    values = dict(line.split(": ", 1) for line in joined if ": " in line)
                    parsed = _selected_text(values, ("Manifest-Version", "Implementation-Title", "Implementation-Version",
                                                     "Implementation-Commit", "Build-Commit", "Git-Commit", "Main-Class"))
                elif name.endswith(".json"):
                    values = json.loads(text)
                    if not isinstance(values, dict):
                        raise ValueError("Fabric metadata must be an object")
                    parsed = _selected_text(values, ("id", "version", "name", "environment"))
                    dependencies = values.get("depends", {})
                    if isinstance(dependencies, dict):
                        parsed["depends"] = _selected_text(dependencies, ("minecraft", "java", "fabricloader", "fabric-api"))
                    result["loaders"].append("fabric")
                else:
                    values = tomllib.loads(text)
                    mods = values.get("mods", [])
                    if not isinstance(mods, list) or len(mods) > 256 or any(not isinstance(mod, dict) for mod in mods):
                        raise ValueError("TOML mods must be a bounded table array")
                    parsed = _selected_text(values, ("modLoader", "loaderVersion", "license"))
                    parsed["mods"] = [_selected_text(mod, ("modId", "version", "displayName")) for mod in mods]
                    result["loaders"].append("neoforge" if "neoforge." in name else "forge")
                result["metadata"][name] = parsed
            except (UnicodeError, ValueError, TypeError, RecursionError) as error:
                # Do not echo attacker-controlled metadata text or parser excerpts.
                raise ValueError(f"Invalid metadata in {name}") from error
    return result


def inspect_artifact(path: Path) -> dict:
    """Stream a stable regular file's hash, then inspect bounded JAR metadata.

    Caller authorizes the path. Symlinks, changing files and oversized inputs are
    rejected. Archive inspection errors are explicit FAIL, while the file's exact
    hash remains available. No archive member is extracted or executed.
    """
    path = Path(path).absolute()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact must be a regular file no larger than 512 MiB")
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise ValueError("Artifact grew beyond inspection bound")
            digest.update(chunk)
        try:
            archive = _inspect_zip(stream, size)
        except (ValueError, zipfile.BadZipFile, EOFError, OSError, NotImplementedError, zlib.error) as error:
            archive = {"status": "FAIL", "loaders": [], "metadata": {},
                       "errors": [str(error) if isinstance(error, ValueError) else "Invalid or unreadable ZIP metadata"]}
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current) or size != before.st_size:
            raise ValueError("Artifact changed during inspection")
    return {"schema": "mcdev.artifact.v1", "status": archive["status"], "path": str(path),
            "sha256": digest.hexdigest(), "bytes": size, "archive": archive,
            "claim": "Byte identity and bounded metadata inspection only; no build or runtime verification"}


def _run(run: dict) -> tuple[str, dict]:
    if not isinstance(run, dict) or run.get("schema") != "mcdev.benchmark.v1":
        raise ValueError("Each run must use schema mcdev.benchmark.v1")
    for key in ("run_id", "launch_id", "pair_id"):
        _identifier(run.get(key), key)
    _digest(run.get("artifact_sha256"), "artifact_sha256")
    if run.get("status") not in {"PASS", "FAIL", "NOT_RUN"}:
        raise ValueError("Run status must be PASS, FAIL or NOT_RUN")
    if run.get("evidence_kind") not in BENCHMARK_KINDS:
        raise ValueError("Unsupported benchmark evidence_kind")
    context = run.get("context")
    if not isinstance(context, dict) or set(context) != set(CONTEXT_KEYS):
        raise ValueError("Run context must contain exactly the documented matching keys")
    if any(context[key] is None or context[key] == "" or context[key] == {} or context[key] == [] for key in CONTEXT_KEYS):
        raise ValueError("Matching context keys cannot be empty or unknown")
    signature = _canonical({"context": context, "evidence_kind": run["evidence_kind"]})
    metrics = run.get("metrics")
    if not isinstance(metrics, dict) or not 1 <= len(metrics) <= MAX_METRICS:
        raise ValueError("Runs need 1..64 metrics")
    shapes = {}
    for name, metric in metrics.items():
        _identifier(name, "metric id")
        if not isinstance(metric, dict) or set(metric) != {"value", "unit", "direction", "measurement"}:
            raise ValueError("Metrics require exactly value, unit, direction, measurement")
        _finite(metric["value"], f"{name}.value")
        if not isinstance(metric["unit"], str) or not re.fullmatch(r"[A-Za-z%][A-Za-z0-9_./%:-]{0,31}", metric["unit"]):
            raise ValueError("Metric unit must be a bounded unit identifier")
        if metric["direction"] not in {"lower", "higher"} or metric["measurement"] not in {"direct", "proxy"}:
            raise ValueError("Metric direction/measurement is invalid")
        shapes[name] = {key: metric[key] for key in ("unit", "direction", "measurement")}
    return signature, shapes


def _thresholds(thresholds: dict | None, metrics: set[str]) -> dict:
    value = {"min_pairs": 3, "metrics": {}} if thresholds is None else thresholds
    if not isinstance(value, dict) or set(value) - {"min_pairs", "metrics"}:
        raise ValueError("Unknown threshold field")
    count = value.get("min_pairs", 3)
    if type(count) is not int or not 2 <= count <= MAX_RUNS:
        raise ValueError("min_pairs must be an integer between 2 and 256")
    gates = value.get("metrics", {})
    if not isinstance(gates, dict) or set(gates) - metrics:
        raise ValueError("Threshold references an unknown metric")
    supported = {"min_relative_improvement", "min_absolute_improvement", "max_regression"}
    for name, gate in gates.items():
        if not isinstance(gate, dict) or not gate or set(gate) - supported:
            raise ValueError("Unknown or empty metric threshold")
        for key, number in gate.items():
            if _finite(number, key) < 0:
                raise ValueError("Thresholds cannot be negative")
    return {"min_pairs": count, "metrics": gates}


def compare_runs(baseline: list[dict], candidate: list[dict], thresholds: dict | None = None) -> dict:
    """Compare matched, repeated, independent launches; refuse mixed/proxy data.

    Each run contributes one predeclared aggregate per metric. Frame samples are
    not treated as independent launches. Thresholds apply to paired improvements,
    not to a ratio of arm medians. PASS with no thresholds means comparability,
    explicitly not demonstrated improvement.
    """
    if not isinstance(baseline, list) or not isinstance(candidate, list) or max(len(baseline), len(candidate)) > MAX_RUNS:
        raise ValueError("Each arm must be a list of at most 256 runs")
    output = {"schema": "mcdev.comparison.v1", "status": "NOT_RUN", "comparison_performed": False,
              "issues": [], "metrics": {}, "baseline_runs": len(baseline), "candidate_runs": len(candidate),
              "claim": "No performance comparison performed", "publication": "NOT_REQUESTED"}
    if not baseline or not candidate:
        output["issues"].append("Both arms require repeated runs")
        return output
    runs = baseline + candidate
    validated = [_run(run) for run in runs]
    signature, shape = validated[0]
    policy = _thresholds(thresholds, set(shape))
    output["thresholds"] = policy
    issues = output["issues"]
    if any(run["status"] != "PASS" for run in runs):
        issues.append("All contributing runs must have status PASS")
    if any(item != (signature, shape) for item in validated):
        issues.append("Context, evidence kind, metric set, units, direction or measurement mismatch")
    if any(metric["measurement"] != "direct" for _, shapes in validated for metric in shapes.values()):
        issues.append("Proxy metrics cannot support this comparison")
    for key in ("run_id", "launch_id"):
        if len({run[key] for run in runs}) != len(runs):
            issues.append(f"Every run needs a distinct {key}")
    for label, arm in (("baseline", baseline), ("candidate", candidate)):
        if len({run["pair_id"] for run in arm}) != len(arm):
            issues.append(f"Duplicate pair_id in {label}")
        if len({run["artifact_sha256"] for run in arm}) != 1:
            issues.append(f"Mixed artifact hashes within {label}")
    if {run["pair_id"] for run in baseline} != {run["pair_id"] for run in candidate}:
        issues.append("Pair membership differs between arms")
    if len(baseline) < policy["min_pairs"] or len(candidate) < policy["min_pairs"]:
        issues.append("Insufficient independent matched pairs")
    if issues:
        return output
    # All structural/provenance checks complete before computing any comparison.
    other = {run["pair_id"]: run for run in candidate}
    failed = False
    for name, metadata in shape.items():
        pairs = []
        for run in baseline:
            base = _finite(run["metrics"][name]["value"], name)
            new = _finite(other[run["pair_id"]]["metrics"][name]["value"], name)
            improvement = (base - new) if metadata["direction"] == "lower" else (new - base)
            relative = improvement / abs(base) if base != 0 else None
            # Even finite input subtraction/division can overflow.
            if not math.isfinite(improvement) or relative is not None and not math.isfinite(relative):
                raise ValueError("Metric comparison overflow")
            pairs.append({"pair_id": run["pair_id"], "baseline": base, "candidate": new,
                          "absolute_improvement": improvement, "relative_improvement": relative})
        gains = [pair["absolute_improvement"] for pair in pairs]
        median_gain = _median(gains)
        relative_gains = [pair["relative_improvement"] for pair in pairs]
        median_relative = _median(relative_gains) if None not in relative_gains else None
        deviations = [_finite(abs(gain - median_gain), "improvement deviation") for gain in gains]
        gate = policy["metrics"].get(name, {})
        checks = {}
        if "min_absolute_improvement" in gate:
            checks["min_absolute_improvement"] = median_gain >= gate["min_absolute_improvement"]
        if "min_relative_improvement" in gate:
            checks["min_relative_improvement"] = median_relative is not None and median_relative >= gate["min_relative_improvement"]
        if "max_regression" in gate:
            checks["max_regression"] = median_relative is not None and median_relative >= -gate["max_regression"]
        failed |= not all(checks.values())
        output["metrics"][name] = dict(metadata, pairs=pairs, pair_count=len(pairs),
            baseline_median=_median(pair["baseline"] for pair in pairs),
            candidate_median=_median(pair["candidate"] for pair in pairs),
            median_absolute_improvement=median_gain, median_relative_improvement=median_relative,
            improvement_mad=_median(deviations),
            improvement_min=min(gains), improvement_max=max(gains), threshold_checks=checks,
            threshold_status=("PASS" if all(checks.values()) else "FAIL") if checks else "NOT_RUN")
    output.update(status="FAIL" if failed else "PASS", comparison_performed=True,
                  baseline_artifact_sha256=baseline[0]["artifact_sha256"],
                  candidate_artifact_sha256=candidate[0]["artifact_sha256"],
                  evidence_kind=baseline[0]["evidence_kind"],
                  context_sha256=hashlib.sha256(signature.encode()).hexdigest(),
                  threshold_status=("FAIL" if failed else "PASS") if policy["metrics"] else "NOT_RUN",
                  claim="Matched comparison; conclusions apply only to the declared evidence kind. "
                        + ("Configured thresholds evaluated." if policy["metrics"] else "No efficacy threshold was requested."),
                  uncertainty="Descriptive paired medians and MAD only; no confidence interval or population significance claim")
    return output


def _check_name(name: object) -> str:
    name = _identifier(name, "check name")
    kind, separator, label = name.partition(":")
    if not separator or not label or kind not in CHECK_KINDS:
        raise ValueError("Check names must be scope-qualified, e.g. build:jar or visible_pixels:frozen453")
    return kind


def prepare_release(artifact: dict, checks: list[dict], required: list[str]) -> dict:
    """Evaluate a local release checklist bound to one exact artifact hash.

    This consumes caller-provided receipts; it neither independently reruns checks
    nor authorizes publishing. Reinspect bytes immediately before any later upload.
    """
    if not isinstance(artifact, dict) or artifact.get("schema") != "mcdev.artifact.v1":
        raise ValueError("Expected an inspect_artifact receipt")
    digest = _digest(artifact.get("sha256"), "artifact.sha256")
    if type(artifact.get("bytes")) is not int or artifact["bytes"] <= 0:
        raise ValueError("Artifact byte count must be a positive integer")
    if not isinstance(checks, list) or len(checks) > 256 or not isinstance(required, list) or not 1 <= len(required) <= 256:
        raise ValueError("Provide 1..256 required names and at most 256 check receipts")
    for name in required:
        _check_name(name)
    if len(set(required)) != len(required):
        raise ValueError("Duplicate required check name")
    indexed = {}
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Checks must be objects")
        kind = _check_name(check.get("name"))
        if check["name"] in indexed:
            raise ValueError("Duplicate check receipt; resolve supersession explicitly")
        if check.get("status") not in {"PASS", "FAIL", "NOT_RUN"}:
            raise ValueError("Check status must be PASS, FAIL or NOT_RUN")
        if check.get("evidence_kind") != kind:
            raise ValueError("Check scope does not match its evidence_kind")
        _digest(check.get("artifact_sha256"), "check.artifact_sha256")
        indexed[check["name"]] = check
    results = []
    for name in required:
        check = indexed.get(name)
        if check is None:
            results.append({"name": name, "status": "NOT_RUN", "reason": "No receipt supplied"})
        elif check["artifact_sha256"] != digest:
            results.append({"name": name, "status": "FAIL", "reason": "Receipt binds a different artifact hash"})
        else:
            results.append({"name": name, "status": check["status"], "evidence_kind": check["evidence_kind"],
                            "artifact_sha256": digest})
    inspection = artifact.get("status") == "PASS" and isinstance(artifact.get("archive"), dict) and artifact["archive"].get("status") == "PASS"
    status = "FAIL" if not inspection or any(check["status"] == "FAIL" for check in results) else (
        "NOT_RUN" if any(check["status"] == "NOT_RUN" for check in results) else "PASS")
    return {"schema": "mcdev.release.v1", "status": status, "readiness": "READY" if status == "PASS" else "NOT_READY",
            "artifact_sha256": digest, "artifact_bytes": artifact["bytes"], "artifact_inspection": "PASS" if inspection else "FAIL",
            "required_checks": results, "unused_checks": sorted(set(indexed) - set(required)),
            "publication": "NOT_REQUESTED", "published": False,
            "claim": "Local checklist readiness only, relative to caller-selected requirements and supplied receipts; "
                     "no automatic publication, installation, native verification or efficacy claim",
            "next_boundary": "Reinspect exact bytes and obtain target-specific authorization before publishing"}


_CRASH_PATTERNS = (
    ("memory_exhaustion", r"OutOfMemoryError|GC overhead limit exceeded", "Inspect heap/native resource limits and allocation evidence."),
    ("java_version", r"UnsupportedClassVersionError|class file version", "Match the JDK major version to the game and loader requirements."),
    ("binary_api_mismatch", r"NoSuchMethodError|NoSuchFieldError|AbstractMethodError", "Check exact loader, Minecraft and dependency versions."),
    ("missing_class", r"ClassNotFoundException|NoClassDefFoundError", "Check dependencies and client/server-side class loading."),
    ("mixin_failure", r"MixinApplyError|InvalidMixinException|InjectionError|MixinTransformerError", "Inspect the owning mod's mixin target and mappings."),
    ("mod_loading", r"ModLoadingException|LoadingFailedException|Incompatible mod set", "Review required mod versions and loader dependency constraints."),
    ("graphics_context", r"GLFW error|GL_INVALID_OPERATION|Failed to create.*(?:OpenGL|window)", "Inspect the graphics context and driver evidence in the intended test environment."),
    ("watchdog", r"ServerHangWatchdog|A single server tick took", "Inspect a thread dump and tick workload before attributing the stall."),
)


# Keep this separate from triage_log: excerpts are useful for authorized remote
# debugging, but their best-effort sanitization is not a confidentiality proof.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_LOG_VALUE = r'''(?:"(?:\\.|[^"\\\r\n])*(?:"|$)|'(?:\\.|[^'\\\r\n])*(?:'|$)|[^\s,;&\]\}"']+)'''
_TOKEN_FIELD = re.compile(
    r'''(?P<prefix>(?<![\w-])--(?:access[_-]?token|token)["']?[ \t]*(?:[=:,][ \t]*|[ \t]+)'''
    r'''|(?<![\w-])(?:access[_-]?token|token)["']?[ \t]*[=:][ \t]*)'''
    r"(?P<value>" + _LOG_VALUE + r")", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r'''(?P<prefix>(?<![\w-])authorization["']?[ \t]*(?:[:=][ \t]*|[ \t]+))'''
    r'''(?P<value>"(?:\\.|[^"\\\r\n])*(?:"|$)|'(?:\\.|[^'\\\r\n])*(?:'|$)'''
    r'''|(?:Bearer|Basic)[ \t]+[^\s,;&\]\}"']+)''', re.IGNORECASE)
_PLAYER_CHAT = re.compile(
    r"\[CHAT\]|^(?:\[[^\]\r\n]*\][ \t]*)+(?::[ \t]*)?<[^>\r\n]{1,64}>[ \t]"
    r"|^<[A-Za-z0-9_]{1,16}>[ \t]", re.IGNORECASE)


def _redacted_value(match: re.Match) -> str:
    value = match.group("value")
    quote = value[0] if value[0] in "\"'" else ""
    return match.group("prefix") + quote + "[REDACTED]" + quote


def redact_log(text: str) -> str:
    """Preserve useful diagnostics while masking common tokens and player chat.

    At most 64 Ki characters are examined; a longer input is visibly truncated.
    This is best effort: paths, usernames outside chat, IPs, unusual credentials,
    multiline/partial-tail secrets and other private data can remain. The caller
    must authorize the diagnostic route; this function performs no I/O/uploads.
    """
    if not isinstance(text, str):
        raise ValueError("Log input must be text")
    bounded = _ANSI.sub("", text[:MAX_REDACT_CHARS])
    lines = []
    for line in bounded.splitlines(keepends=True):
        if _PLAYER_CHAT.search(line):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append("[REDACTED PLAYER CHAT]" + ending)
        else:
            lines.append(_TOKEN_FIELD.sub(_redacted_value, _AUTHORIZATION.sub(_redacted_value, line)))
    result = "".join(lines)
    if len(text) > MAX_REDACT_CHARS:
        result += "\n[DIAGNOSTIC TRUNCATED AT 64 Ki CHARACTERS]\n"
    return result


def triage_log(text: str) -> dict:
    """Bounded local heuristics. Return no raw log text or personal identifiers.

    Avoiding excerpts is stronger than trying to enumerate every token, path,
    address or player-name format. Findings are hints, never a root-cause proof.
    """
    if not isinstance(text, str):
        raise ValueError("Log input must be text")
    bounded = text[:MAX_LOG_CHARS]
    findings = []
    for category, pattern, hint in _CRASH_PATTERNS:
        count = sum(1 for _ in re.finditer(pattern, bounded, re.IGNORECASE))
        if count:
            findings.append({"category": category, "matches": count, "hint": hint})
    return {"schema": "mcdev.triage.v1", "status": "HEURISTIC_ONLY", "findings": findings,
            "truncated": len(text) > MAX_LOG_CHARS, "characters_examined": len(bounded),
            "privacy": "No log excerpts, paths, tokens, addresses or player identifiers are returned; no uploads",
            "claim": "Pattern hints only; an empty result does not establish a healthy build or runtime"}
