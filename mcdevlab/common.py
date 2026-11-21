from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import stat
import tempfile
import time
from pathlib import Path

MAX_JSON = 1_048_576
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Expected a 1–96 character identifier (letters, digits, _, ., -)")
    return value


def load_json(path: Path):
    with path.open("rb") as f:
        data = f.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError("JSON file exceeds 1 MiB")
    return json.loads(data, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def atomic_json(path: Path, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(path: Path, max_bytes=512 * 1024 * 1024):
    sha = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError("Fingerprint input must be a bounded regular file")
        while block := f.read(1024 * 1024):
            size += len(block)
            if size > max_bytes:
                raise ValueError("Fingerprint input exceeded byte limit")
            sha.update(block)
        after = os.fstat(f.fileno())
        current = path.stat(follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, stat.S_IFMT(s.st_mode))
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError("File changed during fingerprinting")
    return {"name": path.name, "bytes": size, "sha256": sha.hexdigest()}


def within(root: Path, relative: str):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("A relative path inside the configured workspace is required")
    if ".." in Path(relative).parts:
        raise ValueError("Parent traversal is not permitted")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path escapes the configured workspace")
    return path


def argv(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 96 or any(
        not isinstance(s, str) or not s or "\0" in s or len(s) > 8192 for s in value
    ):
        raise ValueError("Configured command must be a nonempty bounded argv array")
    return value


def json_process(command, request, *, timeout=15, max_output=MAX_JSON):
    """Operator-configured transport; request arguments never become shell text.

    Disk-spooled bounded replies avoid communicate() retaining arbitrary output.
    A timed-out mutating request has an unknown remote outcome; caller never retries.
    """
    command = argv(command)
    body = json.dumps(request, allow_nan=False).encode() + b"\n"
    if len(body) > MAX_JSON:
        raise ValueError("Request exceeds 1 MiB")
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        inp.write(body)
        inp.seek(0)
        p = subprocess.Popen(command, stdin=inp, stdout=out, stderr=err, shell=False)
        end = time.monotonic() + timeout
        failure = None
        try:
            while p.poll() is None:
                if time.monotonic() >= end:
                    failure = "Transport deadline exceeded; remote outcome may be unknown"
                    break
                if os.fstat(out.fileno()).st_size > max_output or os.fstat(err.fileno()).st_size > 65536:
                    failure = "Transport output limit exceeded; remote outcome may be unknown"
                    break
                time.sleep(0.01)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
        out.seek(0)
        data = out.read(max_output + 1)
        if failure:
            raise RuntimeError(failure)
        if p.returncode != 0:
            raise RuntimeError(f"Configured transport exited {p.returncode}; inspect its local diagnostic log")
        if len(data) > max_output:
            raise RuntimeError("Transport response exceeds limit")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as e:
            raise RuntimeError("Transport returned invalid JSON") from e
        if not isinstance(result, dict):
            raise RuntimeError("Transport response must be a JSON object")
        return result
