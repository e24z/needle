"""Wire format: newline-delimited JSON. One request object per line, one
response object per line. Small enough to read in full here."""

from __future__ import annotations

import json
from typing import Any


def encode(obj: Any) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def decode(line: bytes) -> Any:
    return json.loads(line.decode("utf-8"))
