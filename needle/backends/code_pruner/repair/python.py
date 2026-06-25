"""AST-aware structural repair for pruned Python code.

Expands a semantic line mask to preserve syntactic integrity by walking
the AST and ensuring enclosing scopes, referenced symbols, and required
imports are retained.

Inspired by the LaMR approach to dependency-aware mask repair, implemented
here as deterministic Python AST expansion over a relevance-scored line mask.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RepairResult:
    kept_lines: set[int]
    reasons: dict[int, set[str]]
    repaired_code: str


def _add_line(
    kept: set[int], reasons: dict[int, set[str]], line: int, reason: str
) -> None:
    kept.add(line)
    reasons.setdefault(line, set()).add(reason)


def _add_range(
    kept: set[int],
    reasons: dict[int, set[str]],
    start: int,
    end: int,
    reason: str,
) -> None:
    for line in range(start, end + 1):
        _add_line(kept, reasons, line, reason)


def _node_span(node: ast.AST) -> tuple[int, int] | None:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None or end is None:
        return None
    return int(start), int(end)


def _decorator_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    if not decorators:
        return int(getattr(node, "lineno"))
    return min(int(getattr(decorator, "lineno")) for decorator in decorators)


def _add_class_shell(
    kept: set[int], reasons: dict[int, set[str]], node: ast.ClassDef, reason: str
) -> None:
    """Keep a class readable without automatically keeping every method."""
    _add_range(kept, reasons, _decorator_start(node), node.lineno, reason)
    for child in node.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            span = _node_span(child)
            if span is not None:
                _add_range(kept, reasons, span[0], span[1], f"{reason}:field")
        elif isinstance(child, ast.Pass):
            span = _node_span(child)
            if span is not None:
                _add_range(kept, reasons, span[0], span[1], f"{reason}:pass")


def _has_kept_descendant(node: ast.AST, kept: set[int]) -> bool:
    span = _node_span(node)
    if span is None:
        return False
    start, end = span
    return any(start <= line <= end for line in kept)


def _top_level_symbols(tree: ast.Module) -> dict[str, ast.AST]:
    symbols: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = node
    return symbols


def _top_level_imports(tree: ast.Module) -> list[ast.AST]:
    return [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]


def _import_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names}
    return set()


def _text_for_lines(lines: list[str], kept: Iterable[int]) -> str:
    return "\n".join(lines[line - 1] for line in sorted(kept))


def _referenced_names(text: str) -> set[str]:
    return set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text))


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _marker_indent(lines: list[str], previous: int, next_line_no: int | None) -> str:
    if previous > 0 and lines[previous - 1].rstrip().endswith(":"):
        return f"{_indent_of(lines[previous - 1])}    "
    if next_line_no is not None and 1 <= next_line_no <= len(lines):
        return _indent_of(lines[next_line_no - 1])
    if previous > 0:
        return _indent_of(lines[previous - 1])
    return ""


def _expand_enclosing_scopes(
    tree: ast.Module, kept: set[int], reasons: dict[int, set[str]]
) -> bool:
    changed = False
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (
                ast.ClassDef,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.With,
                ast.AsyncWith,
                ast.Try,
            ),
        ):
            continue
        span = _node_span(node)
        if span is None:
            continue
        start, end = span
        if not any(start <= line <= end for line in kept):
            continue
        before = len(kept)
        if isinstance(node, ast.ClassDef):
            _add_class_shell(kept, reasons, node, "enclosing_class")
        else:
            reason = "enclosing_definition" if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) else "enclosing_control_flow"
            range_start = (
                _decorator_start(node) if reason == "enclosing_definition" else start
            )
            _add_range(kept, reasons, range_start, start, reason)
        changed = changed or len(kept) != before
    return changed


def _expand_referenced_top_level(
    tree: ast.Module, lines: list[str], kept: set[int], reasons: dict[int, set[str]]
) -> bool:
    changed = False
    text = _text_for_lines(lines, kept)
    refs = _referenced_names(text)
    for name, node in _top_level_symbols(tree).items():
        if name not in refs:
            continue
        span = _node_span(node)
        if span is None:
            continue
        start, end = span
        if all(line in kept for line in range(start, end + 1)):
            continue
        before = len(kept)
        if isinstance(node, ast.ClassDef):
            _add_class_shell(kept, reasons, node, f"referenced_top_level:{name}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_name = child.name
                    if child_name == "__init__" or _has_kept_descendant(child, kept):
                        child_span = _node_span(child)
                        if child_span is not None:
                            _add_range(
                                kept,
                                reasons,
                                _decorator_start(child),
                                child_span[1],
                                f"referenced_top_level:{name}:method",
                            )
        else:
            range_start = _decorator_start(node) if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) else start
            _add_range(kept, reasons, range_start, start, f"referenced_top_level:{name}")
        changed = changed or len(kept) != before
    return changed


def _expand_imports(
    tree: ast.Module, lines: list[str], kept: set[int], reasons: dict[int, set[str]]
) -> None:
    text = _text_for_lines(lines, kept)
    refs = _referenced_names(text)
    for node in _top_level_imports(tree):
        names = _import_names(node)
        if refs.isdisjoint(names):
            continue
        span = _node_span(node)
        if span is None:
            continue
        _add_range(kept, reasons, span[0], span[1], "referenced_import")


def render_filtered(lines: list[str], kept: set[int]) -> str:
    output: list[str] = []
    previous = 0
    for line_no in sorted(kept):
        if line_no < 1 or line_no > len(lines):
            continue
        gap = line_no - previous - 1
        if gap > 0:
            gap_lines = lines[previous : line_no - 1]
            gap_chars = sum(len(l) for l in gap_lines) + gap
            marker = f"{_marker_indent(lines, previous, line_no)}[pruned]"
            if gap_chars >= len(marker):
                output.append(marker)
            else:
                output.extend(gap_lines)
        output.append(lines[line_no - 1])
        previous = line_no
    trailing = len(lines) - previous
    if trailing > 0:
        gap_lines = lines[previous:]
        gap_chars = sum(len(l) for l in gap_lines) + trailing
        indent = _marker_indent(lines, previous, None)
        marker = f"{indent}[pruned]"
        if gap_chars >= len(marker):
            output.append(marker)
        else:
            output.extend(gap_lines)
    return "\n".join(output)


def repair_python_mask(code: str, semantic_lines: Iterable[int]) -> RepairResult:
    """Expand a semantic line mask to preserve Python AST integrity.

    Takes the original code and a set of line numbers selected by the
    relevance model, then expands the mask to include enclosing scopes,
    referenced symbols, and necessary imports.
    """
    lines = code.splitlines()
    kept = {line for line in semantic_lines if 1 <= line <= len(lines)}
    reasons: dict[int, set[str]] = {
        line: {"semantic"} for line in kept
    }
    tree = ast.parse(code)

    changed = True
    while changed:
        changed = False
        changed = _expand_enclosing_scopes(tree, kept, reasons) or changed
        changed = _expand_referenced_top_level(tree, lines, kept, reasons) or changed

    _expand_imports(tree, lines, kept, reasons)

    return RepairResult(
        kept_lines=kept,
        reasons=reasons,
        repaired_code=render_filtered(lines, kept),
    )
