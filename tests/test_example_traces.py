"""Validate committed Pi session trace records against their fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACE_PATH = ROOT / "examples" / "traces" / "pi-sessions.jsonl"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_records() -> list[dict]:
    return [
        json.loads(line)
        for line in TRACE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_example_traces_are_in_sync() -> None:
    records = load_records()
    assert {record["id"] for record in records} == {
        "pi-read-batch-guardrail",
        "pi-bash-noisy-sentinel",
        "pi-read-late-statusline-cost",
    }

    for record in records:
        fixture = ROOT / record["input"]["fixture"]
        original = fixture.read_text(encoding="utf-8")
        output = record["session"]["tool_result"]["text"]
        details = record["needle"]["details"]
        counters = record["needle"]["counters"]

        assert record["schema_version"] == 1
        assert record["kind"] == "pi-session-trace"
        assert fixture.exists()
        assert record["input"]["chars"] == len(original)
        assert record["input"]["sha256"] == sha256(original)
        assert record["session"]["tool_result"]["chars"] == len(output)
        assert record["session"]["tool_result"]["sha256"] == sha256(output)
        assert record["session"]["needle_original"]["chars"] == len(original)
        assert record["session"]["needle_original"]["sha256"] == sha256(original)
        assert record["session"]["needle_original"]["matches_fixture"] is True
        assert details["decision"] == "pruned"
        assert counters["calls"] >= 1
        assert counters["originalChars"] >= len(original)
        assert counters["prunedChars"] >= len(output)

        for needle in record["assertions"]["must_contain"]:
            assert needle in output, f"{record['id']} missing {needle!r}"
        for needle in record["assertions"]["must_not_contain"]:
            assert needle not in output, f"{record['id']} unexpectedly kept {needle!r}"


if __name__ == "__main__":
    test_example_traces_are_in_sync()
    print("test_example_traces OK")
