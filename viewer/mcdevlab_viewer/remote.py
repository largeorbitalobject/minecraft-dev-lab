"""Transports that run remote_agent.py on a fleet node and parse its framed reply.

Every transport is read-only by construction: the only thing sent is the fixed remote_agent source plus a
JSON request of read ops. LocalTransport runs the same functions in-process for the controller host.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time

from . import remote_agent

AGENT_SOURCE_PATH = remote_agent.__file__


class TransportError(RuntimeError):
    pass


def agent_source() -> str:
    with open(AGENT_SOURCE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def build_payload(request: dict) -> bytes:
    b64 = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    return ('ARGS_B64 = "%s"\n' % b64).encode("ascii") + agent_source().encode("utf-8")


def parse_frames(raw: bytes):
    """Parse b'MCDV\\n' + frames. Returns (json_reply, {idx: bytes}, {idx: error})."""
    marker = raw.find(b"MCDV\n")
    if marker < 0:
        raise TransportError("no MCDV marker in reply (%d bytes): %r" % (len(raw), raw[:200]))
    pos = marker + 5
    reply = None
    blobs, errs = {}, {}
    n = len(raw)
    while pos < n:
        kind = raw[pos:pos + 1]
        nl = raw.find(b"\n", pos)
        if nl < 0:
            raise TransportError("truncated frame header")
        head = raw[pos + 1:nl].decode("ascii", "replace").split()
        body_start = nl + 1
        if kind == b"J":
            ln = int(head[0])
            reply = json.loads(raw[body_start:body_start + ln].decode("utf-8"))
        elif kind in (b"F", b"X"):
            idx, ln = int(head[0]), int(head[1])
            chunk = raw[body_start:body_start + ln]
            if len(chunk) != ln:
                raise TransportError("truncated file frame %d" % idx)
            if kind == b"F":
                blobs[idx] = chunk
            else:
                errs[idx] = chunk.decode("utf-8", "replace")
        else:
            raise TransportError("bad frame kind %r" % kind)
        pos = body_start + ln
    if reply is None:
        raise TransportError("reply had no JSON frame")
    return reply, blobs, errs


class Transport:
    name = "base"

    def call(self, request: dict, timeout: float):
        """-> (results dict, blobs dict, errs dict). Raises TransportError on failure."""
        raise NotImplementedError


class LocalTransport(Transport):
    name = "local"

    def call(self, request, timeout):
        results, frames = remote_agent.run_ops(request)
        raw = b"MCDV\n" + remote_agent._frame_json({"ok": True, "results": results, "t": time.time()}) + b"".join(frames)
        reply, blobs, errs = parse_frames(raw)
        return reply["results"], blobs, errs


class ProcTransport(Transport):
    """argv ends with a command that runs `python -` (stdin = agent payload) on the node."""

    def __init__(self, name, argv):
        self.name = name
        self.argv = list(argv)

    def call(self, request, timeout):
        payload = build_payload(request)
        try:
            cp = subprocess.run(self.argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise TransportError("%s: timed out after %.0fs" % (self.name, timeout))
        except OSError as e:
            raise TransportError("%s: cannot start transport: %s" % (self.name, e))
        if cp.returncode != 0 and b"MCDV\n" not in cp.stdout:
            tail = cp.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise TransportError("%s: exit %s: %s" % (self.name, cp.returncode, " | ".join(tail)[:300]))
        reply, blobs, errs = parse_frames(cp.stdout)
        return reply["results"], blobs, errs


def make_transport(host_id: str, spec: dict) -> Transport:
    kind = spec.get("transport", {}).get("type")
    if kind == "local":
        return LocalTransport()
    if kind == "argv":
        return ProcTransport(host_id, spec["transport"]["argv"])
    raise ValueError("unknown transport for %s: %r" % (host_id, kind))
