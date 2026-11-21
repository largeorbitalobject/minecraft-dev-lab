#!/usr/bin/env python3
"""Build an offline stdlib-only zipapp and installable distribution ZIP."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "minecraft-dev-lab"
VERSION = "0.1.0"


def _zip(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return target.getvalue()


def build(output: Path | None = None, root: Path = ROOT):
    output = (output or root / "dist").resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (root / "mcdevlab/__main__.py").is_file():
        raise ValueError("The application entrypoint is missing")
    sources = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "mcdevlab").rglob("*.py")}
    sources["__main__.py"] = b'import runpy\nrunpy.run_module("mcdevlab", run_name="__main__")\n'
    app = _zip(sources)
    app_path = output / f"{NAME}-{VERSION}.pyz"
    app_path.write_bytes(app)
    package = {"install.py": (root / "install.py").read_bytes(), NAME + ".pyz": app,
               ".codex-plugin/plugin.json": (root / ".codex-plugin/plugin.json").read_bytes(),
               ".mcp.json": (root / ".mcp.json").read_bytes()}
    for directory in ("skills", "docs", "examples"):
        for p in (root / directory).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                package[p.relative_to(root).as_posix()] = p.read_bytes()
    for name in ("README.md", "LICENSE", "VALIDATION.json"):
        if (root / name).is_file():
            package[name] = (root / name).read_bytes()
    prefix = f"{NAME}-{VERSION}/"
    archive_path = output / f"{NAME}-{VERSION}.zip"
    archive_path.write_bytes(_zip({prefix + name: data for name, data in package.items()}))
    artifacts = {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
                 for p in (app_path, archive_path)}
    (output / "SHA256SUMS").write_text("".join(f'{v["sha256"]}  {k}\n' for k, v in artifacts.items()), encoding="ascii")
    return {"version": VERSION, "output": str(output), "artifacts": artifacts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    print(json.dumps(build(parser.parse_args().output), indent=2))
