"""Bounded stdio MCP subset: 2025-11-25 and 2026-07-28, tools only.

Protocol references: modelcontextprotocol.io/specification/{version}/basic.
Build execution belongs to the API's explicit job handles, not this transport.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time

LEGACY = "2025-11-25"
MODERN = "2026-07-28"
VERSIONS = [MODERN, LEGACY]
INFO = {"name": "minecraft-dev-lab", "version": "0.1.0"}
CAPABILITIES = {"tools": {"listChanged": False}}
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_PENDING = 16
EOF_GRACE_SECONDS = 1.0
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"


class ProtocolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.data = code, data


def _id(value):
    return type(value) in (str, int)


def _constant(value):
    raise ValueError("Non-finite JSON number")


def _lines(stream):
    while True:
        raw = stream.readline(MAX_MESSAGE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_MESSAGE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(65536)
            yield None
        else:
            yield raw


class _Server:
    def __init__(self, api, output):
        self.api, self.output = api, output
        self.initialized = self.ready = self.closed = False
        self.lock = threading.RLock()
        self.active = {}
        self.jobs = queue.Queue(MAX_PENDING)
        self.worker = threading.Thread(target=self._work, name="mcdev-mcp", daemon=True)
        self.worker.start()

    def emit(self, value, token=None):
        with self.lock:
            if self.closed or token is not None and token[0]:
                return
            try:
                raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise ValueError("Response exceeds transport bound")
            except (ValueError, TypeError, RecursionError):
                raw = json.dumps({"jsonrpc": "2.0", "id": value.get("id"), "error": {
                    "code": -32603, "message": "Result cannot be encoded within transport bounds"}}).encode("utf-8")
            self.output.write(raw + b"\n")
            self.output.flush()

    def error(self, rid, error, token=None):
        detail = {"code": error.code, "message": str(error)}
        if error.data is not None:
            detail["data"] = error.data
        self.emit({"jsonrpc": "2.0", "id": rid, "error": detail}, token)

    def result(self, rid, result, modern, token=None):
        if modern:
            result = dict(result, resultType="complete")
            result["_meta"] = {"io.modelcontextprotocol/serverInfo": INFO}
        self.emit({"jsonrpc": "2.0", "id": rid, "result": result}, token)

    def receive(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            raise ProtocolError(-32600, "Invalid JSON-RPC request")
        method = message["method"]
        if "id" not in message:
            params = message.get("params", {})
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            elif method == "notifications/cancelled" and isinstance(params, dict) and _id(params.get("requestId")):
                with self.lock:
                    token = self.active.get(params["requestId"])
                    if token is not None:
                        token[0] = True
            return  # Notifications never receive responses or invoke tools.
        rid = message["id"]
        if not _id(rid):
            raise ProtocolError(-32600, "Request id must be a string or integer")
        params = message.get("params", {})
        try:
            if not isinstance(params, dict):
                raise ProtocolError(-32602, "params must be an object")
            meta = params.get("_meta", {})
            if not isinstance(meta, dict):
                raise ProtocolError(-32602, "_meta must be an object")
            modern = VERSION_KEY in meta or method == "server/discover"
            if modern:
                if not isinstance(meta.get(VERSION_KEY), str) or not isinstance(meta.get(CAPABILITIES_KEY), dict):
                    raise ProtocolError(-32602, "Modern requests require protocolVersion and clientCapabilities metadata")
                if meta[VERSION_KEY] != MODERN:
                    raise ProtocolError(-32022, "Unsupported protocol version", {"supported": VERSIONS, "requested": meta[VERSION_KEY]})
            elif method == "initialize":
                if self.initialized:
                    raise ProtocolError(-32600, "Already initialized")
                client = params.get("clientInfo")
                if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(client, dict) or not all(isinstance(client.get(k), str) for k in ("name", "version")):
                    raise ProtocolError(-32602, "Invalid initialize parameters")
                self.initialized = True
                self.result(rid, {"protocolVersion": LEGACY, "capabilities": CAPABILITIES, "serverInfo": INFO}, False)
                return
            elif method == "ping":
                self.result(rid, {}, False)
                return
            elif not self.ready:
                raise ProtocolError(-32000, "Initialize and send notifications/initialized first")
            if method == "server/discover":
                self.result(rid, {"capabilities": CAPABILITIES, "supportedVersions": VERSIONS, "ttlMs": 0, "cacheScope": "private"}, True)
            elif method == "tools/list":
                if params.get("cursor") is not None:
                    raise ProtocolError(-32602, "This complete tool list has no pagination cursor")
                result = {"tools": sorted(self.api.list_tools(), key=lambda tool: tool["name"])}
                if modern:
                    result.update(ttlMs=0, cacheScope="private")
                self.result(rid, result, modern)
            elif method == "tools/call":
                if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
                    raise ProtocolError(-32602, "Tool name and object arguments are required")
                if params["name"] not in {tool["name"] for tool in self.api.list_tools()}:
                    raise ProtocolError(-32602, "Unknown tool")
                if "task" in params or "inputResponses" in params or "requestState" in params:
                    raise ProtocolError(-32602, "Task and multi-round-trip extensions are not supported")
                with self.lock:
                    if rid in self.active:
                        raise ProtocolError(-32600, "Duplicate active request id")
                    if len(self.active) >= MAX_PENDING:
                        raise ProtocolError(-32000, "Too many pending tool calls")
                    token = [False]
                    self.active[rid] = token
                    self.jobs.put_nowait((rid, params, modern, token))
            else:
                raise ProtocolError(-32601, "Method not found")
        except ProtocolError as error:
            self.error(rid, error)
        except Exception:
            self.error(rid, ProtocolError(-32603, "Internal server error"))

    def _work(self):
        while True:
            try:
                item = self.jobs.get(timeout=0.1)
            except queue.Empty:
                if self.closed:
                    return
                continue
            if item is None:
                self.jobs.task_done()
                return
            rid, params, modern, token = item
            try:
                if not token[0]:
                    try:
                        payload = self.api.call(params["name"], params.get("arguments", {}))
                    except ValueError as error:
                        raise ProtocolError(-32602, str(error)) from error
                    except RuntimeError as error:
                        result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
                    else:
                        result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True, allow_nan=False)}], "structuredContent": payload, "isError": False}
                    self.result(rid, result, modern, token)
            except ProtocolError as error:
                self.error(rid, error, token)
            except Exception:
                self.error(rid, ProtocolError(-32603, "Internal server error"), token)
            finally:
                with self.lock:
                    self.active.pop(rid, None)
                self.jobs.task_done()

    def close(self):
        # API calls are bounded control operations; running builds use explicit job IDs.
        deadline = time.monotonic() + EOF_GRACE_SECONDS
        while self.active and time.monotonic() < deadline:
            time.sleep(0.001)
        with self.lock:
            self.closed = True
            for token in self.active.values():
                token[0] = True
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        self.worker.join(max(0.0, deadline - time.monotonic()))


def serve(api, stdin=None, stdout=None):
    """Serve binary streams until EOF. Cancellation suppresses pending responses.

    It cannot forcibly interrupt a Python API call already executing. Long work must
    use the API's job start/status/cancel tools, which outlive request cancellation.
    """
    server = _Server(api, stdout if stdout is not None else sys.stdout.buffer)
    try:
        for raw in _lines(stdin if stdin is not None else sys.stdin.buffer):
            try:
                if raw is None:
                    raise ProtocolError(-32600, "Message exceeds transport byte limit")
                try:
                    message = json.loads(raw.decode("utf-8"), parse_constant=_constant)
                except (UnicodeError, ValueError, RecursionError) as error:
                    raise ProtocolError(-32700, "Parse error") from error
                server.receive(message)
            except ProtocolError as error:
                server.error(None, error)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        server.close()
