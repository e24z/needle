from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_sock() -> Path:
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        yield Path(td) / "manager.sock"
