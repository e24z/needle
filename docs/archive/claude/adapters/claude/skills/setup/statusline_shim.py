#!/usr/bin/env python3
"""Stable Hay statusline launcher installed by `/hay:setup`.

Claude's statusLine setting needs an absolute path. Git-based plugin installs
live under versioned cache paths, so pointing statusLine directly at the plugin
can strand it on an old version after updates. This shim stays at
~/.hay/statusline.py and execs the currently installed Hay statusline.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_NAME = "hay"
STATUSLINE_REL = Path("adapters/claude/statusline.py")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _candidate_roots() -> list[Path]:
    claude = Path.home() / ".claude"
    settings = _load_json(claude / "settings.json")
    enabled = settings.get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}

    known = _load_json(claude / "plugins" / "known_marketplaces.json")
    installed = _load_json(claude / "plugins" / "installed_plugins.json").get("plugins")
    installed = installed if isinstance(installed, dict) else {}

    keys = [key for key in installed if key.startswith(f"{PLUGIN_NAME}@")]
    keys.sort(key=lambda key: (not enabled.get(key, False), key))

    roots: list[Path] = []
    for key in keys:
        marketplace = key.split("@", 1)[1]
        market = known.get(marketplace)
        if isinstance(market, dict):
            source = market.get("source")
            source = source if isinstance(source, dict) else {}
            if source.get("source") == "directory":
                for raw in (market.get("installLocation"), source.get("path")):
                    if raw:
                        roots.append(Path(raw).expanduser())

        entries = installed.get(key)
        if isinstance(entries, list):
            for entry in reversed(entries):
                if isinstance(entry, dict) and entry.get("installPath"):
                    roots.append(Path(entry["installPath"]).expanduser())

    return roots


def main() -> int:
    seen: set[str] = set()
    for root in _candidate_roots():
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        target = root / STATUSLINE_REL
        if target.exists():
            os.execv(sys.executable, [sys.executable, str(target)])

    sys.stdout.write("- hay - run /hay:setup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
