from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Minecraft Dev Lab: MCP fleet, owned jobs and test evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "agent"):
        p = sub.add_parser(name); p.add_argument("--config", type=Path, required=name == "agent")
    for name in ("worker", "exec"):
        p = sub.add_parser(name); p.add_argument("--state", type=Path, required=True); p.add_argument("--job", required=True)
    p = sub.add_parser("call", help="Use a tool without an MCP client")
    p.add_argument("--config", type=Path); p.add_argument("tool"); p.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()
    if args.command == "serve":
        from .mcp import serve
        from .api import LabAPI
        serve(LabAPI(args.config)); return 0
    if args.command == "agent":
        from .node import Node
        from .common import MAX_JSON
        node_id = None
        try:
            node = Node(args.config); node_id = node.config["id"]
            data = sys.stdin.buffer.readline(MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise ValueError("Request exceeds1MiB")
            request = json.loads(data)
            if not isinstance(request, dict):
                raise ValueError("Expected a JSON request object")
            result = {"schema": "mcdev.reply.v1", "node_id": node_id, "ok": True, "result": node.dispatch(request)}
        except Exception as e:
            result = {"schema": "mcdev.reply.v1", "node_id": node_id, "ok": False, "error": str(e)[:1000], "error_type": type(e).__name__}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n"); return 0
    if args.command in {"worker", "exec"}:
        from .node import worker, exec_recipe
        return (worker if args.command == "worker" else exec_recipe)(args.state, args.job)
    from .api import LabAPI
    print(json.dumps(LabAPI(args.config).call(args.tool, json.loads(args.arguments)), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
