"""AST repair invariants: partially-kept try blocks, statement integrity,
multi-line headers, and the parse-verification flag.

Run: PYTHONPATH=python python3 tests/test_repair.py
"""

from __future__ import annotations

import ast
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from needle_worker.soft_lamr.repair import repair_python_mask  # noqa: E402


TRY_CODE = """\
import os

def load(path):
    try:
        handle = open(path)
        data = handle.read()
    except FileNotFoundError:
        data = ""
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    else:
        print("loaded")
    finally:
        print("done")
    return data
"""


def _line_of(code: str, needle: str) -> int:
    for line_no, line in enumerate(code.splitlines(), start=1):
        if needle in line:
            return line_no
    raise AssertionError(f"line not found: {needle}")


def test_kept_line_inside_try_body_parses() -> None:
    result = repair_python_mask(TRY_CODE, [_line_of(TRY_CODE, "handle.read()")])

    assert result.parses
    ast.parse(result.repaired_code)
    assert "except FileNotFoundError:" in result.repaired_code
    assert "except OSError as exc:" in result.repaired_code
    assert "finally:" in result.repaired_code


def test_kept_line_inside_handler_body_parses() -> None:
    result = repair_python_mask(TRY_CODE, [_line_of(TRY_CODE, "RuntimeError")])

    assert result.parses
    ast.parse(result.repaired_code)
    assert "try:" in result.repaired_code


def test_try_finally_without_handlers_parses() -> None:
    code = """\
def close(resource):
    try:
        resource.flush()
    finally:
        resource.close()
"""
    result = repair_python_mask(code, [_line_of(code, "flush")])

    assert result.parses
    ast.parse(result.repaired_code)
    assert "finally:" in result.repaired_code


def test_docstring_interior_line_kept_whole() -> None:
    code = '''\
"""Module docstring.

Interior line that a semantic mask might select alone.
"""

VALUE = 1
'''
    result = repair_python_mask(code, [_line_of(code, "Interior line")])

    assert result.parses
    ast.parse(result.repaired_code)


def test_multiline_statement_kept_whole() -> None:
    code = """\
result = compute(
    first_argument,
    second_argument,
)
"""
    result = repair_python_mask(code, [_line_of(code, "second_argument")])

    assert result.parses
    ast.parse(result.repaired_code)
    assert "compute(" in result.repaired_code


def test_multiline_signature_kept_whole() -> None:
    code = """\
def configure(
    alpha,
    beta,
):
    alpha.update(beta)
    return alpha
"""
    result = repair_python_mask(code, [_line_of(code, "alpha.update")])

    assert result.parses
    ast.parse(result.repaired_code)


def test_random_masks_over_real_sources_parse() -> None:
    """Every repaired render of a random mask over this repo's own worker
    sources must parse, and the `parses` flag must tell the truth."""
    root = Path(__file__).resolve().parent.parent / "python" / "needle_worker"
    sources = sorted(root.rglob("*.py"))
    assert sources, "worker sources not found"
    rng = random.Random(24)
    checked = 0
    for source in sources:
        code = source.read_text()
        line_count = len(code.splitlines())
        if line_count < 5:
            continue
        for _ in range(8):
            size = rng.randrange(1, max(2, line_count // 3))
            mask = rng.sample(range(1, line_count + 1), min(size, line_count))
            result = repair_python_mask(code, mask)
            assert result.parses, f"{source.name}: mask={sorted(mask)[:8]}"
            ast.parse(result.repaired_code)
            checked += 1
    assert checked >= 50


def main() -> int:
    test_kept_line_inside_try_body_parses()
    test_kept_line_inside_handler_body_parses()
    test_try_finally_without_handlers_parses()
    test_docstring_interior_line_kept_whole()
    test_multiline_statement_kept_whole()
    test_multiline_signature_kept_whole()
    test_random_masks_over_real_sources_parse()
    print("test_repair OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
