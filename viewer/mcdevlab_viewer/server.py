"""HTTP server: static single-page UI + JSON API. Python stdlib only, read-only.

  /                              the UI (web/index.html), hash-routed
  /static/...                    UI assets (fonts are vendored in web/fonts, no CDN)
  /api/fleet                     host board
  /api/runs?host=&gate=&recipe=&verdict=&kind=&commit=&limit=&gates=1
  /api/run/<id>?base=auto|none|<run id>
  /api/log/<id>?tail=&level=&grep=
  /api/logdiff?base=<run>&cand=<run>
  /api/compare?base=<commit|run>&cand=<commit|run>
  /api/candidates, /api/history?host=&gate=&limit=, /api/status, /api/scorecard
  /api/live/<host>/<job>         live progress (poll every ~2 s); add /stream for Server-Sent Events
  /files/<run>/<rel>             mirrored screenshots, thumbnails, evidence files
"""
from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import posixpath
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .app import App

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")


def _int(q, key, default, lo=1, hi=100000):
    try:
        return max(lo, min(hi, int(q.get(key, [default])[0])))
    except (ValueError, TypeError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "DevLabViewer/0.1"
    protocol_version = "HTTP/1.1"
    app: App = None          # set by make_server
    web_dir = os.path.join(ROOT, "web")
    quiet = True

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ---------------------------------------------------------------------------------
    def _send(self, code, body, ctype, extra=None, head=False):
        gz = None
        if (len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or "")
                and ctype.split(";")[0] in ("application/json", "text/html", "text/css", "text/javascript", "image/svg+xml")):
            gz = gzip.compress(body, 5)
        data = gz if gz is not None else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if gz is not None:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _json(self, obj, code=200, head=False):
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"}, head)

    def _err(self, code, msg, head=False):
        self._json({"error": msg}, code, head)

    def do_HEAD(self):
        self._route(head=True)

    def do_GET(self):
        self._route(head=False)

    # -- routing ----------------------------------------------------------------------------------
    def _route(self, head):
        try:
            u = urlparse(self.path)
            path = unquote(u.path)
            q = parse_qs(u.query)
            if path == "/" or path == "/index.html":
                return self._static("index.html", head)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], head)
            if path.startswith("/files/"):
                return self._run_file(path[len("/files/"):], head)
            if path.startswith("/api/"):
                return self._api(path[len("/api/"):], q, head)
            if path == "/healthz":
                return self._json({"ok": True}, head=head)
            return self._err(404, "not found", head)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # never kill the server thread; report
            try:
                self._err(500, "%s: %s" % (type(e).__name__, e), head)
            except Exception:
                pass

    def _safe_join(self, base, rel):
        rel = posixpath.normpath("/" + rel).lstrip("/")
        full = os.path.realpath(os.path.join(base, rel))
        if not (full == os.path.realpath(base) or full.startswith(os.path.realpath(base) + os.sep)):
            return None
        return full

    def _static(self, rel, head):
        full = self._safe_join(self.web_dir, rel)
        if not full or not os.path.isfile(full):
            return self._err(404, "not found", head)
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "text/javascript"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=604800, immutable" if full.endswith(".woff2") else "no-cache"
        self._send(200, body, ctype, {"Cache-Control": cache}, head)

    def _run_file(self, rel, head):
        parts = rel.split("/", 1)
        if len(parts) != 2:
            return self._err(404, "not found", head)
        base = self.app.store.p("runs", parts[0])
        if not os.path.isdir(base) or ".." in parts[0] or "/" in parts[0]:
            return self._err(404, "not found", head)
        full = self._safe_join(base, parts[1])
        if not full or not os.path.isfile(full):
            return self._err(404, "not found", head)
        if os.path.basename(full) in ("mirror.json",):
            return self._err(404, "not found", head)
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = mimetypes.guess_type(full)[0] or "text/plain"
        if full.endswith((".txt", ".log", ".jsonl", ".csv")):
            ctype = "text/plain"
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        cache = "public, max-age=86400" if full.endswith((".png", ".jpg")) else "no-cache"
        self._send(200, body, ctype, {"Cache-Control": cache}, head)

    def _api(self, path, q, head):
        app = self.app
        seg = [s for s in path.split("/") if s]
        if not seg:
            return self._err(404, "not found", head)
        name = seg[0]
        if name == "fleet":
            return self._json(app.fleet(), head=head)
        if name == "scorecard":
            # Atlas scorecard written by /root/minecraft-dev-lab-config/bin/atlas-scorecard (one headline + three gates).
            sc = "/root/minecraft-dev-lab-config/scorecard/scorecard.json"
            if not os.path.isfile(sc):
                return self._json({"schema": "atlas.scorecard.v1", "hosts": {}, "note": "not generated yet"}, head=head)
            with open(sc) as fh:
                return self._json(json.load(fh), head=head)
        if name == "status":
            return self._json(app.status(), head=head)
        if name == "runs":
            lst, total = app.runs(host=q.get("host", [None])[0], gate=q.get("gate", [None])[0], recipe=q.get("recipe", [None])[0],
                                  verdict=q.get("verdict", [None])[0], kind=q.get("kind", [None])[0],
                                  commit=q.get("commit", [None])[0], limit=_int(q, "limit", 200, 1, 2000),
                                  gates_only=q.get("gates", ["0"])[0] in ("1", "true"))
            return self._json({"runs": lst, "total": total}, head=head)
        if name == "run" and len(seg) == 2:
            d = app.run_detail(seg[1], q.get("base", ["auto"])[0])
            return self._json(d, head=head) if d else self._err(404, "no such run", head)
        if name == "log" and len(seg) == 2:
            v = app.log_view(seg[1], tail=_int(q, "tail", 300, 1, 5000), level=q.get("level", [None])[0], grep=q.get("grep", [None])[0])
            return self._json(v, head=head) if v else self._err(404, "no log for that run", head)
        if name == "logdiff":
            b, c = q.get("base", [None])[0], q.get("cand", [None])[0]
            if not b or not c:
                return self._err(400, "base and cand run ids required", head)
            return self._json(app.log_diff(b, c), head=head)
        if name == "compare":
            b, c = q.get("base", [None])[0], q.get("cand", [None])[0]
            if not b or not c:
                return self._err(400, "base and cand required (commit, label, jar sha prefix or run id)", head)
            return self._json(app.compare(b, c), head=head)
        if name == "candidates":
            return self._json({"candidates": app.candidates()}, head=head)
        if name == "history":
            return self._json(app.history(q.get("host", [None])[0], q.get("gate", [None])[0], _int(q, "limit", 24, 2, 200)), head=head)
        if name == "live" and len(seg) >= 2:
            host = seg[1]
            job = seg[2] if len(seg) >= 3 else None
            if len(seg) == 4 and seg[3] == "stream":
                return self._sse(host, job)
            data = app.live(host, job)
            data = dict(data)
            data["poll_ms"] = 2000 if data.get("active") else 10000
            data["server_ts"] = time.time()
            return self._json(data, head=head)
        return self._err(404, "unknown endpoint", head)

    def _sse(self, host, job):
        """Server-Sent Events: one `data:` message whenever the live file changes. Ends 10 min in; EventSource reconnects."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        path = self.app.store.p("live", host + ".json")
        last, t_end, last_ping = None, time.time() + 600, 0
        try:
            while time.time() < t_end:
                try:
                    mt = os.stat(path).st_mtime
                except OSError:
                    mt = None
                if mt != last:
                    last = mt
                    data = dict(self.app.live(host, job))
                    data["server_ts"] = time.time()
                    self.wfile.write(b"data: " + json.dumps(data, separators=(",", ":"), default=str).encode() + b"\n\n")
                    self.wfile.flush()
                    if not data.get("active"):
                        break
                elif time.time() - last_ping > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(data_dir, port=8095, host="0.0.0.0", web_dir=None, config=None):
    app = App(data_dir, config)
    handler = type("BoundHandler", (Handler,), {"app": app, "web_dir": web_dir or os.path.join(ROOT, "web")})
    srv = Server((host, port), handler)
    srv.app = app
    return srv


def main(argv=None):
    from . import config as cfgmod
    ap = argparse.ArgumentParser(description="Dev Lab Viewer server")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--no-collector", action="store_true", help="serve existing data only")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    cfg = cfgmod.load_config(args.config)
    port = args.port if args.port is not None else cfg["port"]
    Handler.quiet = not args.verbose
    srv = make_server(args.data, port, args.host, config=cfg)
    col = None
    if not args.no_collector:
        from .collector import Collector
        col = Collector(cfg, args.data)
        col.run_forever()
    print("Dev Lab Viewer on http://%s:%d  data=%s  collector=%s" % (args.host, port, args.data, "on" if col else "off"),
          file=sys.stderr, flush=True)
    import signal

    def _term(_sig, _frm):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if col:
            col.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
