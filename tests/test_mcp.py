import io
import json
import threading
import unittest
from unittest.mock import patch

from mcdevlab import mcp


def modern(method, rid=1, **params):
    params["_meta"] = {mcp.VERSION_KEY: mcp.MODERN, mcp.CAPABILITIES_KEY: {}}
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def legacy_start(version=mcp.LEGACY):
    return [{"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"}]


class API:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [{"name": n, "description": "A protocol test boundary", "inputSchema": {
            "type": "object", "properties": {}, "additionalProperties": False}} for n in ("z", "echo")]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if arguments.get("failure") == "invalid":
            raise ValueError("Invalid option")
        if arguments.get("failure") == "operation":
            raise RuntimeError("Job failed")
        if arguments.get("failure") == "unexpected":
            raise LookupError("private implementation detail")
        return {"echo": arguments}


def exchange(messages, api=None):
    source = b"".join(m if isinstance(m, bytes) else json.dumps(m, ensure_ascii=False).encode("utf-8") + b"\n" for m in messages)
    target = io.BytesIO()
    mcp.serve(api or API(), io.BytesIO(source), target)
    return [json.loads(line) for line in target.getvalue().splitlines()]


class ProtocolTests(unittest.TestCase):
    def test_modern_discover_required_schema_fields(self):
        result = exchange([modern("server/discover")])[0]["result"]
        # Required sets verified against official schema/2026-07-28/schema.json.
        self.assertTrue({"capabilities", "supportedVersions", "resultType", "ttlMs", "cacheScope"} <= result.keys())
        self.assertEqual(result["supportedVersions"], ["2026-07-28", "2025-11-25"])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["_meta"]["io.modelcontextprotocol/serverInfo"]["version"], "0.1.0")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})

    def test_modern_list_and_call_without_initialization(self):
        results = {x["id"]: x["result"] for x in exchange([modern("tools/list"), modern("tools/call", 2, name="echo", arguments={"text": "雪\nline"})])}
        self.assertTrue({"tools", "resultType", "ttlMs", "cacheScope"} <= results[1].keys())
        self.assertEqual([x["name"] for x in results[1]["tools"]], ["echo", "z"])
        self.assertEqual(results[2]["structuredContent"], {"echo": {"text": "雪\nline"}})
        self.assertEqual(json.loads(results[2]["content"][0]["text"]), results[2]["structuredContent"])

    def test_legacy_lifecycle_and_version_negotiation(self):
        messages = legacy_start("unsupported-future-version") + [{"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 4, "method": "ping"}]
        replies = exchange(messages)
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["protocolVersion"], mcp.LEGACY)
        self.assertNotIn("resultType", replies[1]["result"])
        self.assertEqual(replies[2]["result"], {})
        self.assertEqual(exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])[0]["error"]["code"], -32000)

    def test_modern_does_not_inherit_metadata(self):
        absent = {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {}}
        replies = exchange([modern("server/discover"), absent])
        self.assertEqual(replies[1]["error"]["code"], -32602)
        bad = modern("tools/list")
        bad["params"]["_meta"][mcp.VERSION_KEY] = "1900-01-01"
        error = exchange([bad])[0]["error"]
        self.assertEqual(error["code"], -32022)
        self.assertEqual(error["data"]["requested"], "1900-01-01")
        self.assertIn(mcp.MODERN, error["data"]["supported"])

    def test_protocol_and_operational_errors_are_distinct(self):
        replies = {x["id"]: x for x in exchange([modern("tools/call", i, name="echo", arguments={"failure": f}) for i, f in enumerate(("invalid", "operation", "unexpected"))])}
        self.assertEqual(replies[0]["error"]["code"], -32602)
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertEqual(replies[2]["error"]["code"], -32603)
        self.assertNotIn("private implementation", str(replies[2]))

    def test_unknown_method_tool_and_bad_arguments(self):
        for msg, code in [(modern("missing"), -32601), (modern("ping"), -32601),
                          (modern("tools/call", name="missing"), -32602),
                          (modern("tools/call", name="echo", arguments=[]), -32602),
                          (modern("tools/list", cursor="x"), -32602),
                          (modern("tools/call", name="echo", task={}), -32602)]:
            with self.subTest(msg=msg):
                self.assertEqual(exchange([msg])[0]["error"]["code"], code)

    def test_notifications_are_silent_and_cannot_call_tools(self):
        api = API()
        self.assertEqual(exchange([{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "echo"}}, {"jsonrpc": "2.0", "method": "unknown"}], api), [])
        self.assertEqual(api.calls, [])

    def test_invalid_json_utf8_and_envelopes(self):
        for raw, code in [(b"{\n", -32700), (b"\xff\n", -32700), (b'{"x": NaN}\n', -32700),
                          (b"[]\n", -32600), (b"null\n", -32600)]:
            with self.subTest(raw=raw):
                response = exchange([raw])[0]
                self.assertEqual(response["error"]["code"], code)
                self.assertIsNone(response["id"])
        for invalid in (None, True, 1.25, {}, []):
            response = exchange([modern("tools/list", invalid)])[0]
            self.assertEqual(response["error"]["code"], -32600)
            self.assertIsNone(response["id"])

    def test_crlf_string_id_and_escaped_newline(self):
        raw = json.dumps(modern("tools/call", "a\nb", name="echo", arguments={"unicode": "雪"})).encode() + b"\r\n"
        self.assertEqual(exchange([raw])[0]["id"], "a\nb")

    def test_oversized_line_drains_and_recovers(self):
        replies = exchange([b"x" * (mcp.MAX_MESSAGE_BYTES + 10) + b"\n", modern("tools/list")])
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertIn("tools", replies[1]["result"])

    def test_cancel_active_and_queued_does_not_emit_or_execute_queued_call(self):
        entered, release = threading.Event(), threading.Event()
        api = API()
        original = api.call
        def blocked(name, args):
            entered.set()
            release.wait(2)
            return original(name, args)
        api.call = blocked
        output = io.BytesIO()
        server = mcp._Server(api, output)
        try:
            server.receive(modern("tools/call", 1, name="echo"))
            self.assertTrue(entered.wait(1))
            server.receive(modern("tools/call", 2, name="echo"))
            for rid in (1, 2):
                server.receive({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rid}})
            release.set()
        finally:
            release.set()
            server.close()
        self.assertEqual(output.getvalue(), b"")
        self.assertEqual(len(api.calls), 1)

    def test_pending_bound_and_duplicate_id(self):
        entered, release = threading.Event(), threading.Event()
        api = API()
        api.call = lambda n, a: (entered.set(), release.wait(2), {})[-1]
        output = io.BytesIO()
        server = mcp._Server(api, output)
        try:
            server.receive(modern("tools/call", 1, name="echo"))
            self.assertTrue(entered.wait(1))
            server.receive(modern("tools/call", 1, name="echo"))
            for rid in range(2, mcp.MAX_PENDING + 2):
                server.receive(modern("tools/call", rid, name="echo"))
            errors = [json.loads(line)["error"]["code"] for line in output.getvalue().splitlines()]
            self.assertEqual(errors, [-32600, -32000])
            self.assertEqual(len(server.active), mcp.MAX_PENDING)
        finally:
            release.set()
            server.close()

    def test_empty_eof_reaps_worker(self):
        before = {t.ident for t in threading.enumerate()}
        self.assertEqual(exchange([]), [])
        self.assertEqual({t.ident for t in threading.enumerate()}, before)


if __name__ == "__main__":
    unittest.main()
