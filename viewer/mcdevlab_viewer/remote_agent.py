# Read-only remote reader for Dev Lab Viewer.
#
# This file is BOTH imported locally (for the controller host) AND piped over stdin to `python -` on a
# remote node (CT224, fd, GPC, Mac) by remote.py. It must stay: stdlib only, Python 3.8+, and strictly
# read-only: it opens files with mode 'rb', lists directories and reads clocks. It never writes, renames,
# deletes, spawns processes or touches the network. tests/test_remote_agent.py greps this file to enforce it.
#
# Request (JSON, base64 in ARGS_B64):  {"ops": [{"id": "a", "op": "tree|read|tail|fetch", ...}, ...]}
# Response on stdout (binary framed so file bytes survive ssh and the Windows console host):
#   b"MCDV\n" then frames:  b"J<len>\n<json>"  (results)   b"F<idx> <len>\n<bytes>"  b"X<idx> <len>\n<msg>"
import base64
import fnmatch
import json
import os
import sys
import time

MAX_READ_DEFAULT = 262144
MAX_FETCH_DEFAULT = 64 * 1024 * 1024


def _match(rel, pat):
    """Glob on a posix-style relative path. A pattern without '/' matches the basename at any depth;
    a pattern with '/' must have the same number of segments ('*' never crosses a '/')."""
    if "/" not in pat:
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pat)
    rp, pp = rel.split("/"), pat.split("/")
    return len(rp) == len(pp) and all(fnmatch.fnmatch(a, b) for a, b in zip(rp, pp))


def _join(root, rel):
    return os.path.join(root, *rel.split("/")) if rel else root


def _safe_text(data):
    return data.decode("utf-8", "replace")


def op_tree(spec):
    """List files under `root` (pruned walk) with size+mtime, and inline small changed files.

    spec: root, maxdepth, include [fnmatch on posix rel path], exclude_dirs [names], inline [fnmatch],
          inline_max, known {rel: [size, mtime]} (skip inline for unchanged), since_s, max_files
    """
    root = spec["root"]
    maxdepth = int(spec.get("maxdepth", 3))
    include = spec.get("include") or ["*"]
    exclude_dirs = set(spec.get("exclude_dirs") or [])
    inline_pat = spec.get("inline") or []
    inline_max = int(spec.get("inline_max", MAX_READ_DEFAULT))
    known = spec.get("known") or {}
    since_s = spec.get("since_s")
    max_files = int(spec.get("max_files", 5000))
    now = time.time()
    out = {"root": root, "exists": os.path.isdir(root), "files": [], "inline": {}, "truncated": False, "now": now,
           "errors": []}
    if not out["exists"]:
        return out

    def walk(path, rel, depth):
        try:
            it = list(os.scandir(path))
        except OSError as e:
            out["errors"].append("%s: %s" % (path, e))
            return
        it.sort(key=lambda e: e.name)
        for e in it:
            r = (rel + "/" + e.name) if rel else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in exclude_dirs or depth >= maxdepth:
                        continue
                    walk(e.path, r, depth + 1)
                elif e.is_file(follow_symlinks=False):
                    if not any(_match(r, pat) for pat in include):
                        continue
                    st = e.stat()
                    if since_s is not None and now - st.st_mtime > float(since_s):
                        continue
                    if len(out["files"]) >= max_files:
                        out["truncated"] = True
                        return
                    out["files"].append({"p": r, "s": st.st_size, "m": st.st_mtime})
            except OSError as ex:
                out["errors"].append("%s: %s" % (e.path, ex))

    walk(root, "", 0)
    for f in out["files"]:
        r = f["p"]
        if not any(_match(r, pat) for pat in inline_pat):
            continue
        k = known.get(r)
        if k and k[0] == f["s"] and k[1] == f["m"]:
            continue
        if f["s"] > inline_max:
            continue
        try:
            with open(_join(root, r), "rb") as fh:
                out["inline"][r] = _safe_text(fh.read(inline_max))
        except OSError as ex:
            out["errors"].append("%s: %s" % (r, ex))
    return out


def op_read(spec):
    """Read specific small files by absolute path. Missing files report exists=false."""
    mx = int(spec.get("max_bytes", MAX_READ_DEFAULT))
    res = {}
    for p in spec.get("paths") or []:
        try:
            st = os.stat(p)
            if os.path.isdir(p):
                res[p] = {"exists": True, "dir": True, "m": st.st_mtime}
                continue
            with open(p, "rb") as fh:
                data = fh.read(mx)
            res[p] = {"exists": True, "s": st.st_size, "m": st.st_mtime, "text": _safe_text(data),
                      "truncated": st.st_size > mx}
        except OSError:
            res[p] = {"exists": False}
    return {"files": res, "now": time.time()}


def op_tail(spec):
    """Bytes of a growing file. With offset: from that offset (incremental). Otherwise the last max_bytes."""
    p = spec["path"]
    mx = int(spec.get("max_bytes", 65536))
    try:
        st = os.stat(p)
    except OSError:
        return {"exists": False}
    size = st.st_size
    offset = spec.get("offset")
    if offset is None or offset > size:
        start = max(0, size - mx)
    else:
        start = max(offset, size - 4 * mx) if size - offset > 4 * mx else offset
    with open(p, "rb") as fh:
        fh.seek(start)
        data = fh.read(mx if offset is None else 4 * mx)
    return {"exists": True, "size": size, "m": st.st_mtime, "start": start, "end": start + len(data),
            "reset": offset is not None and offset > size, "text": _safe_text(data), "now": time.time()}


def op_live(spec):
    """Find the most recently active run group under root and return its file list plus tails.

    spec: root, maxdepth, exclude_dirs, group ('first' = first path segment, 'dir' = dirname, 'root' = one group),
          roles [{role, include[], tail_bytes (0 = list only), offset_key}], since_s, offsets {rel: size already read}
    Only files matching some role are considered. The group with the newest matching file wins.
    """
    root = spec["root"]
    maxdepth = int(spec.get("maxdepth", 3))
    exclude_dirs = set(spec.get("exclude_dirs") or [])
    roles = spec.get("roles") or []
    since_s = spec.get("since_s", 3600)
    offsets = spec.get("offsets") or {}
    group_mode = spec.get("group", "first")
    now = time.time()
    out = {"root": root, "exists": os.path.isdir(root), "group": None, "files": [], "tails": {}, "now": now}
    if not out["exists"]:
        return out
    found = []

    def walk(path, rel, depth):
        try:
            it = list(os.scandir(path))
        except OSError:
            return
        for e in it:
            r = (rel + "/" + e.name) if rel else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in exclude_dirs or depth >= maxdepth:
                        continue
                    walk(e.path, r, depth + 1)
                elif e.is_file(follow_symlinks=False):
                    role = None
                    for ro in roles:
                        if any(_match(r, pat) for pat in ro.get("include") or []):
                            role = ro
                            break
                    if role is None:
                        continue
                    st = e.stat()
                    if now - st.st_mtime > float(since_s):
                        continue
                    found.append((r, st.st_size, st.st_mtime, role))
            except OSError:
                continue

    walk(root, "", 0)
    if not found:
        return out

    def gkey(rel):
        if group_mode == "root":
            return ""
        if group_mode == "dir":
            return rel.rsplit("/", 1)[0] if "/" in rel else ""
        return rel.split("/")[0] if "/" in rel else ""

    newest = {}
    for r, sz, m, role in found:
        k = gkey(r)
        newest[k] = max(newest.get(k, 0), m)
    best = max(newest, key=lambda k: newest[k])
    out["group"] = best
    for r, sz, m, role in sorted(found):
        if gkey(r) != best:
            continue
        out["files"].append({"p": r, "s": sz, "m": m, "role": role.get("role")})
        tb = int(role.get("tail_bytes", 0))
        if tb:
            off = offsets.get(r)
            try:
                out["tails"][r] = op_tail({"path": _join(root, r), "max_bytes": tb, "offset": off})
            except OSError as ex:
                out["tails"][r] = {"exists": False, "err": str(ex)}
    return out


def _frame_json(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return b"J" + str(len(raw)).encode() + b"\n" + raw


def run_ops(req):
    """Execute ops. Returns (results_dict, binary_frames list of bytes)."""
    results = {}
    frames = []
    findex = 0
    for op in req.get("ops") or []:
        oid = op.get("id") or op.get("op")
        kind = op.get("op")
        try:
            if kind == "tree":
                results[oid] = op_tree(op)
            elif kind == "read":
                results[oid] = op_read(op)
            elif kind == "tail":
                results[oid] = op_tail(op)
            elif kind == "live":
                results[oid] = op_live(op)
            elif kind == "fetch":
                mx = int(op.get("max_bytes", MAX_FETCH_DEFAULT))
                meta = []
                for f in op.get("files") or []:
                    path = _join(f["root"], f["rel"])
                    idx = findex
                    findex += 1
                    try:
                        st = os.stat(path)
                        if st.st_size > mx:
                            msg = ("too big: %d > %d" % (st.st_size, mx)).encode()
                            frames.append(b"X" + ("%d %d\n" % (idx, len(msg))).encode() + msg)
                            meta.append({"idx": idx, "ok": False, "err": "too big", "root": f["root"], "rel": f["rel"]})
                            continue
                        with open(path, "rb") as fh:
                            data = fh.read(st.st_size)
                        frames.append(b"F" + ("%d %d\n" % (idx, len(data))).encode() + data)
                        meta.append({"idx": idx, "ok": True, "s": len(data), "m": st.st_mtime, "root": f["root"], "rel": f["rel"]})
                    except OSError as ex:
                        msg = str(ex).encode()
                        frames.append(b"X" + ("%d %d\n" % (idx, len(msg))).encode() + msg)
                        meta.append({"idx": idx, "ok": False, "err": str(ex), "root": f["root"], "rel": f["rel"]})
                results[oid] = {"files": meta}
            else:
                results[oid] = {"error": "unknown op %r" % kind}
        except Exception as ex:  # keep going: one bad op must not hide the others
            results[oid] = {"error": "%s: %s" % (type(ex).__name__, ex)}
    return results, frames


def main():
    args = globals().get("ARGS_B64")
    if args is None:
        args = sys.stdin.read()
    req = json.loads(base64.b64decode(args).decode("utf-8"))
    results, frames = run_ops(req)
    out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
    out.write(b"MCDV\n")
    node = os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else "")
    out.write(_frame_json({"ok": True, "results": results, "t": time.time(), "node": node}))
    for fr in frames:
        out.write(fr)
    out.flush()


if __name__ == "__main__":
    main()
