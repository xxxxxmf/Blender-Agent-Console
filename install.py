#!/usr/bin/env python3
"""Install the Agent Console addon and bridge into Blender user configuration.

Usage:
    python install.py                 # install into every detected Blender version
    python install.py --version 4.2   # install into one version
    python install.py --uninstall     # remove the addon from every version
    python install.py --install-cli   # also install CodeBuddy CLI if missing
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADDON_SRC = ROOT / "addon"
BRIDGE_SRC = ROOT / "bridge"
ADDON_NAME = "agent_console"


def blender_config_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Blender Foundation" / "Blender"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "blender"


def detect_versions(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    found = [p.name for p in root.iterdir() if p.is_dir() and re.match(r"^\d+\.\d+$", p.name)]
    return sorted(found, key=lambda item: tuple(int(part) for part in item.split(".")))


def addon_destination(version: str) -> Path:
    return blender_config_root() / version / "scripts" / "addons" / ADDON_NAME


def prepare_bridge() -> None:
    config = BRIDGE_SRC / "config.json"
    example = BRIDGE_SRC / "config.example.json"
    if not config.exists() and example.exists():
        shutil.copy2(example, config)
        print("[setup] created bridge/config.json")


def install(version: str) -> None:
    prepare_bridge()
    destination = addon_destination(version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(ADDON_SRC, destination)
    marker = destination / "bridge_path.txt"
    marker.write_text(str(BRIDGE_SRC / "agent_bridge.py"), encoding="utf-8")
    print("[ok] installed addon to", destination)
    print("[ok] bridge script:", BRIDGE_SRC / "agent_bridge.py")


def uninstall(version: str) -> None:
    destination = addon_destination(version)
    if destination.exists():
        shutil.rmtree(destination)
        print("[ok] removed", destination)
    else:
        print("[skip] not installed:", destination)


def find_codebuddy_cli() -> str:
    for name in ("codebuddy.cmd", "codebuddy"):
        found = shutil.which(name)
        if found:
            return found
    pattern = os.path.join(
        os.environ.get("USERPROFILE", ""), ".workbuddy", "binaries",
        "node", "versions", "*", "codebuddy.cmd",
    )
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else ""


def install_codebuddy_cli() -> str:
    if os.name == "nt":
        command = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-Command", "irm https://www.codebuddy.cn/cli/install.ps1 | iex",
        ]
    else:
        command = ["bash", "-c", "curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash"]
    print("[cli] running official installer:", " ".join(command))
    subprocess.run(command, check=True)
    return find_codebuddy_cli()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Blender version folder, e.g. 4.2 or 5.3")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--install-cli", action="store_true",
        help="install CodeBuddy CLI with the official installer if it is missing",
    )
    args = parser.parse_args()

    root = blender_config_root()
    versions = [args.version] if args.version else detect_versions(root)
    if not versions:
        print("[error] no Blender version found. Use --version X.Y")
        return 1

    print("[info] Blender config root:", root)
    print("[info] target versions:", ", ".join(versions))
    for version in versions:
        if args.uninstall:
            uninstall(version)
        else:
            install(version)

    if not args.uninstall:
        cli = find_codebuddy_cli()
        if not cli and args.install_cli:
            try:
                cli = install_codebuddy_cli()
            except Exception as exc:
                print("[warn] CLI auto-install failed:", exc)
        if cli:
            print("[ok] CodeBuddy CLI found:", cli)
            print("[next] First time only: run `codebuddy` in a terminal and complete login.")
        else:
            print("[warn] CodeBuddy CLI not found.")
            print("       Re-run with --install-cli, or see https://www.codebuddy.cn/docs/cli/quickstart")

        print("\nNext steps:")
        print("  1. In Blender: Edit > Preferences > Add-ons > Install/Enable 'Agent Console'")
        print("  2. In the 3D view sidebar, open 'Agent Console'")
        print("  3. Click 'Start Bridge' or run: python start_bridge.py")
        print("  4. Send a message. Only local CLI agents are supported; configure any CLI in bridge/config.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
