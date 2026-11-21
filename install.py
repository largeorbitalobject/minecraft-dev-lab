#!/usr/bin/env python3
"""Install into a new explicit destination. Never edits host app configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

NAME = "minecraft-dev-lab"
VERSION = "0.1.0"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def install(dest: Path, config: Path, source: Path | None = None):
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or later is required")
    source = (source or Path(__file__).parent).resolve()
    # Do not resolve the last path component: a symlink is an existing destination.
    dest = Path(os.path.abspath(dest.expanduser()))
    config = config.expanduser().resolve(strict=True)
    if not config.is_file():
        raise ValueError("--config must point to an existing project configuration file")
    if dest.exists() or dest.is_symlink():
        raise FileExistsError("Destination already exists; choose a new directory")
    artifact = source / (NAME + ".pyz")
    if not artifact.is_file():
        artifact = source / "dist" / f"{NAME}-{VERSION}.pyz"
    if not artifact.is_file():
        raise FileNotFoundError("Build the package first: python scripts/package.py")
    manifest = source / ".codex-plugin/plugin.json"
    if not manifest.is_file() or not (source / "skills").is_dir():
        raise ValueError("Distribution is missing its plugin manifest or skills")
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".mcdev-install-", dir=dest.parent))
    claimed = False
    try:
        relative_plugin = Path("plugins") / NAME
        plugin = stage / relative_plugin
        plugin.mkdir(parents=True)
        shutil.copyfile(artifact, plugin / (NAME + ".pyz"))
        (plugin / ".codex-plugin").mkdir()
        shutil.copyfile(manifest, plugin / ".codex-plugin/plugin.json")
        shutil.copytree(source / "skills", plugin / "skills")
        command = {"command": str(Path(sys.executable).resolve()),
                   "args": [str(dest / relative_plugin / (NAME + ".pyz")), "serve", "--config", str(config)]}
        mcp = {"mcpServers": {NAME: command}}
        _write(plugin / ".mcp.json", mcp)
        _write(stage / "mcp.json", mcp)
        # Keep Unicode literal: JSON's surrogate-pair escapes are invalid in TOML.
        (stage / "mcp.toml").write_text(
            f'[mcp_servers."{NAME}"]\ncommand = {json.dumps(command["command"], ensure_ascii=False)}\n'
            f'args = {json.dumps(command["args"], ensure_ascii=False)}\n', encoding="utf-8")
        _write(stage / ".agents/plugins/marketplace.json", {
            "name": "minecraft-dev-lab-local", "interface": {"displayName": "Minecraft Dev Lab Local"},
            "plugins": [{"name": NAME, "source": {"source": "local", "path": "./plugins/" + NAME},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                         "category": "Productivity"}]})
        receipt = {"name": NAME, "version": VERSION, "destination": str(dest), "config": str(config),
                   "python": command["command"], "pyz_sha256": hashlib.sha256((plugin / (NAME + ".pyz")).read_bytes()).hexdigest(),
                   "global_configuration_modified": False}
        _write(stage / "install-receipt.json", receipt)
        (stage / "REGISTER.txt").write_text(
            "The local files are installed. Host registration and UI installation are separate steps.\n"
            "Register this marketplace root with: codex plugin marketplace add <this-directory>\n"
            "Then install Minecraft Dev Lab from the local marketplace in the desktop app.\n"
            "Alternatively merge the provided MCP JSON or TOML snippet into your chosen client's configuration.\n"
            "Keep this directory and the referenced project configuration at their installed paths.\n"
            "No global config, trust settings, services, or network connections were changed.\n", encoding="utf-8")
        # Exclusive directory creation refuses even an empty destination created by a racer.
        dest.mkdir()
        claimed = True
        for child in stage.iterdir():
            child.rename(dest / child.name)
        return receipt
    except BaseException:
        if claimed:
            shutil.rmtree(dest)
        raise
    finally:
        shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = install(args.dest, args.config)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Install failed: {error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
