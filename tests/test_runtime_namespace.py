"""Needle runtime namespace surface.

Run: PYTHONPATH=src python3 tests/test_runtime_namespace.py
"""

from __future__ import annotations

import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.runtime import naming  # noqa: E402
from needle.runtime.backends import FakePruner  # noqa: E402
from needle.runtime.manager import Manager  # noqa: E402
from needle.runtime.protocol import decode, encode  # noqa: E402
from needle.runtime.__main__ import main as runtime_main  # noqa: E402


def main() -> int:
    assert naming.code_version()
    assert decode(encode({"op": "stats"})) == {"op": "stats"}

    manager = Manager(lambda: FakePruner(), heavy=False)
    resp = manager.handle({"op": "prune", "text": "abc", "query": "letters"})
    assert resp["ok"] is True
    assert resp["text"] == "abc"
    assert resp["backend"] == "fake"

    out = StringIO()
    err = StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            runtime_main(["definitely-not-a-command"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("needle.runtime entrypoint should delegate CLI parsing")
    assert "invalid choice" in err.getvalue()

    print("test_runtime_namespace OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
