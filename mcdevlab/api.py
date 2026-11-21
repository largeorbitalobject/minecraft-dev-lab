from __future__ import annotations
import os
from pathlib import Path
from .common import load_json, json_process, within, identifier


def obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


S = {"type": "string", "minLength": 1, "maxLength": 4096}
HOST = {"host": S}
INSTANCE = {**HOST, "instance": S}
JOB = {**HOST, "job": S}


def tool(name, description, properties=None, required=(), *, read=True):
    return {"name": name, "description": description, "inputSchema": obj(properties, required),
            "annotations": {"readOnlyHint": read, "destructiveHint": not read, "idempotentHint": read,
                            "openWorldHint": name not in {"fleet_list", "benchmark_compare", "release_check"}}}


def validate(value, schema, where="arguments"):
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    kind = schema.get("type")
    if kind in types and (not isinstance(value, types[kind]) or kind in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"{where} must be {kind}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise ValueError(f"{where}.{k} is required")
        if schema.get("additionalProperties") is False and set(value) - props.keys():
            raise ValueError(f"Unexpected field in {where}: {sorted(set(value) - props.keys())[0]}")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], where + "." + k)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 1024):
            raise ValueError(f"{where} has too many items")
        for v in value:
            validate(v, schema.get("items", {}), where + "[]")
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 65536):
        raise ValueError(f"{where} string length is outside bounds")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where} is not a supported value")
    if kind in {"number", "integer"}:
        import math
        if not math.isfinite(value) or not schema.get("minimum", -1e100) <= value <= schema.get("maximum", 1e100):
            raise ValueError(f"{where} numeric value is outside bounds")


class LabAPI:
    def __init__(self, config_path=None):
        raw = config_path or os.environ.get("MCDEV_CONFIG")
        self.path = Path(raw).expanduser().resolve() if raw else None
        self.config = load_json(self.path) if self.path else {"schema": "mcdev.fleet.v1", "hosts": {}, "workspaces": {}}
        if self.config.get("schema") != "mcdev.fleet.v1":
            raise ValueError("Expected mcdev.fleet.v1 fleet configuration")

    def list_tools(self):
        from .bridges import REQUEST_SCHEMA
        return [
            tool("fleet_list", "List configured Minecraft test hosts and workspaces. Does not connect to hosts or launch anything."),
            tool("host_probe", "Read capabilities and Python/OS identity from a configured host. GPU labels are expectations unless witnessed by the game.", HOST, HOST),
            tool("instance_inspect", "Inspect a named instance, loader components, mod filenames and available recipes without starting it.", INSTANCE, INSTANCE),
            tool("run_start", "Start one configured build/test/client recipe with exclusive resource ownership. Reuse the same request_id after uncertain transport; never invent a second ID to retry. Returns a durable job handle; exit0 is not game acceptance.", {**INSTANCE, "recipe": S, "request_id": S}, [*INSTANCE, "recipe", "request_id"], read=False),
            tool("run_status", "Read a durable job or request ID, heartbeat, outcome and exact artifact receipt. UNKNOWN keeps the resource reserved.", JOB, JOB),
            tool("run_list", "List recent owned jobs and active/uncertain resource holders on one host, including jobs started by another controller session.", {**HOST, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, HOST),
            tool("run_cancel", "Request cancellation of this plugin's exact owned job. No process-name or broad kill. Poll status to establish completion.", JOB, JOB, read=False),
            tool("run_logs", "Read a bounded redacted diagnostic tail and crash triage from one owned job. Raw logs remain on the node.", {**JOB, "bytes": {"type": "integer", "minimum": 1, "maximum": 65536}}, JOB),
            tool("bridge_capabilities", "Inspect a configured gameplay provider's platform support, code pins and operations. Does not send game input.", INSTANCE, INSTANCE),
            tool("game_action", "Send one advertised typed game operation through a pinned provider. Actions need a fresh session/world/frame witness. Unknown outcomes are recorded and never replayed. Native authority and idle/desktop guards belong to the provider; no generic input fallback.", {**INSTANCE, "request": REQUEST_SCHEMA, "owner_job": S}, [*INSTANCE, "request"], read=False),
            tool("game_action_result", "Recover a recorded bridge result by original request_id after connection loss without dispatching it again.", {**HOST, "request_id": S}, [*HOST, "request_id"]),
            tool("artifact_inspect", "Fingerprint a mod JAR in an allowed workspace and inspect bounded Fabric/Forge/NeoForge metadata without extracting or executing it.", {"workspace": S, "path": S}, ["workspace", "path"]),
            tool("benchmark_compare", "Compare matched independent baseline/candidate runs. Reject mismatched fixtures, hardware, scene, timing origin, units or proxy evidence; report dispersion and insufficient pairs honestly.", {"baseline": {"type": "array", "items": {"type": "object"}, "maxItems": 128}, "candidate": {"type": "array", "items": {"type": "object"}, "maxItems": 128}, "thresholds": {"type": "object"}}, ["baseline", "candidate"]),
            tool("release_check", "Prepare a LOCAL release readiness decision bound to an actual artifact hash. Missing checks are NOT_RUN. Source/build checks cannot stand in for native or visible-pixel acceptance. Does not upload or publish.", {"workspace": S, "path": S, "checks": {"type": "array", "items": {"type": "object"}, "maxItems": 128}, "required": {"type": "array", "items": S, "maxItems": 128}}, ["workspace", "path", "checks", "required"]),
        ]

    def remote(self, host, payload):
        identifier(host)
        hosts = self.config.get("hosts", {})
        if host not in hosts:
            raise ValueError("Unknown configured host")
        spec = hosts[host]
        if spec.get("enabled") is not True:
            raise RuntimeError("Host is not configured/enabled; use the local setup guide")
        result = json_process(spec.get("argv"), payload, timeout=45 if payload.get("op") == "game_action" else 15)
        if result.get("schema") != "mcdev.reply.v1" or result.get("node_id") != spec.get("node_id", host):
            raise RuntimeError("Unexpected node identity or protocol; outcome may be unknown")
        if result.get("ok") is not True:
            raise RuntimeError(result.get("error", "Node refused request"))
        return result["result"]

    def artifact(self, workspace, relative):
        identifier(workspace)
        workspaces = self.config.get("workspaces", {})
        if workspace not in workspaces:
            raise ValueError("Unknown allowed workspace")
        path = Path(workspaces[workspace]).expanduser()
        if not path.is_absolute() and self.path:
            path = self.path.parent / path
        from .evidence import inspect_artifact
        return inspect_artifact(within(path.resolve(), relative))

    def call(self, name, arguments):
        spec = next((t for t in self.list_tools() if t["name"] == name), None)
        if spec is None:
            raise ValueError("Unknown tool")
        validate(arguments, spec["inputSchema"])
        a = arguments
        if name == "fleet_list":
            return {"schema": "mcdev.fleet.v1", "hosts": [{"id": k, "label": v.get("label", k), "enabled": v.get("enabled", False), "node_id": v.get("node_id", k)} for k, v in self.config.get("hosts", {}).items()], "workspaces": sorted(self.config.get("workspaces", {})), "connected": False}
        if name == "benchmark_compare":
            from .evidence import compare_runs
            return compare_runs(a["baseline"], a["candidate"], a.get("thresholds"))
        if name in {"artifact_inspect", "release_check"}:
            artifact = self.artifact(a["workspace"], a["path"])
            if name == "artifact_inspect":
                return artifact
            from .evidence import prepare_release
            return prepare_release(artifact, a["checks"], a["required"])
        op = {"host_probe": "probe", "instance_inspect": "inspect", "run_start": "start", "run_status": "status", "run_cancel": "cancel", "run_logs": "logs", "run_list": "list"}.get(name, name)
        return self.remote(a["host"], {"op": op, **{k: v for k, v in a.items() if k != "host"}})
