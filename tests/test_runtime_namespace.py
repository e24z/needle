"""Needle runtime namespace migration surface.

Run: PYTHONPATH=. python3 tests/test_runtime_namespace.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from needle.runtime import naming  # noqa: E402
from needle.runtime.backends import FakePruner  # noqa: E402
from needle.runtime.manager import Manager  # noqa: E402
from needle.runtime.protocol import decode, encode  # noqa: E402
from pruner import naming as legacy_naming  # noqa: E402


def main() -> int:
    assert naming.code_version() == legacy_naming.code_version()
    assert decode(encode({"op": "stats"})) == {"op": "stats"}

    manager = Manager(lambda: FakePruner(), heavy=False)
    resp = manager.handle({"op": "prune", "text": "abc", "query": "letters"})
    assert resp["ok"] is True
    assert resp["text"] == "abc"
    assert resp["backend"] == "fake"

    print("test_runtime_namespace OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
