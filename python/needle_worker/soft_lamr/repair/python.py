"""AST-aware structural repair for pruned Python code.

Expands a semantic line mask to preserve syntactic integrity by walking
the AST and ensuring enclosing scopes, referenced symbols, and required
imports are retained. The rendered output is verified with `ast.parse`;
`RepairResult.parses` reports the outcome so callers can fall back to the
unrepaired render instead of trusting an unverified claim.

Inspired by the LaMR approach to dependency-aware mask repair, implemented
here as deterministic Python AST expansion over a relevance-scored line mask.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Iterable


_TRY_NODES = (ast.Try, *((ast.TryStar,) if hasattr(ast, "TryStar") else ()))


@dataclass(frozen=True)
class RepairResult:
    kept_lines: set[int]
    reasons: dict[int, set[str]]
    repaired_code: str
    parses: bool = True


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


def _header_end(node: ast.AST) -> int:
    """Last line of a compound statement's header. Headers can span lines
    (multi-line signatures/conditions); the body's first line bounds them."""
    start = int(getattr(node, "lineno"))
    body = getattr(node, "body", None)
    if isinstance(body, list) and body:
        return max(start, int(getattr(body[0], "lineno", start)) - 1)
    return start


def _add_class_shell(
    kept: set[int], reasons: dict[int, set[str]], node: ast.ClassDef, reason: str
) -> None:
    """Keep a class readable without automatically keeping every method."""
    _add_range(kept, reasons, _decorator_start(node), _header_end(node), reason)
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


def _opens_block(line: str) -> bool:
    """Best-effort: does this line end with a block-opening colon? Trailing
    comments are stripped; a `#` inside a string can fool this, in which case
    the parse gate downstream catches the bad render."""
    return line.split("#", 1)[0].rstrip().endswith(":")


_CONTINUATION_KEYWORDS = ("elif", "else", "except", "finally")


def _is_continuation_line(line: str) -> bool:
    """Block-continuation headers: a marker indented like one of these and
    placed before it would split the statement chain it continues."""
    stripped = line.strip()
    return any(
        stripped == keyword or stripped.startswith((f"{keyword} ", f"{keyword}:", f"{keyword}("))
        for keyword in _CONTINUATION_KEYWORDS
    )


def _marker_indent(lines: list[str], previous: int, next_line_no: int | None) -> str:
    if previous > 0 and _opens_block(lines[previous - 1]):
        return f"{_indent_of(lines[previous - 1])}    "
    if (
        next_line_no is not None
        and 1 <= next_line_no <= len(lines)
        and not _is_continuation_line(lines[next_line_no - 1])
    ):
        return _indent_of(lines[next_line_no - 1])
    if previous > 0:
        return _indent_of(lines[previous - 1])
    return ""


def _keyword_line(lines: list[str], block_start: int, keyword: str) -> int | None:
    """Locate the `else:`/`finally:` keyword line preceding a block's first
    statement. These lines belong to no AST node, so find them lexically."""
    pattern = re.compile(rf"{keyword}\s*:")
    for line_no in range(block_start - 1, 0, -1):
        if pattern.match(lines[line_no - 1].strip()):
            return line_no
    return None


def _add_try_structure(
    lines: list[str], kept: set[int], reasons: dict[int, set[str]], node: ast.AST
) -> None:
    """Keep the block headers a partially-kept `try` statement needs to parse.

    A bare `try:` header cannot stand alone: without its `except`/`finally`
    headers the render is a SyntaxError. Bodies stay prunable — the render's
    `[pruned]` marker is a valid expression statement that fills each block.
    """
    for handler in getattr(node, "handlers", []):
        header_end = max(handler.lineno, handler.body[0].lineno - 1)
        _add_range(kept, reasons, handler.lineno, header_end, "enclosing_try:except")
    orelse = getattr(node, "orelse", [])
    if orelse:
        line = _keyword_line(lines, orelse[0].lineno, "else")
        if line is not None:
            _add_line(kept, reasons, line, "enclosing_try:else")
    finalbody = getattr(node, "finalbody", [])
    if finalbody:
        line = _keyword_line(lines, finalbody[0].lineno, "finally")
        if line is not None:
            _add_line(kept, reasons, line, "enclosing_try:finally")


def _expand_enclosing_scopes(
    tree: ast.Module, lines: list[str], kept: set[int], reasons: dict[int, set[str]]
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
                *_TRY_NODES,
            ),
        ):
            continue
        span = _node_span(node)
        if span is None:
            continue
        start, end = span
        # A kept decorator line must pull in its definition's header: a
        # decorator followed by a pruned-out def is a syntax error.
        trigger_start = (
            _decorator_start(node)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            else start
        )
        if not any(trigger_start <= line <= end for line in kept):
            continue
        before = len(kept)
        if isinstance(node, ast.ClassDef):
            _add_class_shell(kept, reasons, node, "enclosing_class")
        elif isinstance(node, _TRY_NODES):
            _add_range(kept, reasons, start, start, "enclosing_control_flow")
            _add_try_structure(lines, kept, reasons, node)
        else:
            reason = "enclosing_definition" if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) else "enclosing_control_flow"
            range_start = (
                _decorator_start(node) if reason == "enclosing_definition" else start
            )
            _add_range(kept, reasons, range_start, _header_end(node), reason)
        changed = changed or len(kept) != before
    return changed


def _is_compound(node: ast.stmt) -> bool:
    body = getattr(node, "body", None)
    return bool(body) and isinstance(body, list) and isinstance(body[0], ast.AST)


def _expand_statement_spans(
    tree: ast.Module, kept: set[int], reasons: dict[int, set[str]]
) -> bool:
    """Multi-line simple statements render whole or not at all.

    A kept line inside a multi-line call, dict literal, or docstring is a
    syntax fragment on its own; the enclosing-scope pass only handles
    statements with bodies. (`match` has no `body` field, so a partially-kept
    match statement is deliberately kept whole here too.)
    """
    changed = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or _is_compound(node):
            continue
        span = _node_span(node)
        if span is None:
            continue
        start, end = span
        if end <= start:
            continue
        if not any(start <= line <= end for line in kept):
            continue
        if all(line in kept for line in range(start, end + 1)):
            continue
        _add_range(kept, reasons, start, end, "statement_integrity")
        changed = True
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
    kept_sorted = [n for n in sorted(kept) if 1 <= n <= len(lines)]
    previous = 0
    for idx, line_no in enumerate(kept_sorted):
        gap = line_no - previous - 1
        if gap > 0:
            gap_lines = lines[previous : line_no - 1]
            gap_chars = sum(len(l) for l in gap_lines) + gap
            # Indent the marker like the next non-blank kept line: a blank
            # kept line has no indent to offer, and a column-0 marker inside
            # an indented block makes the following line an indent error.
            anchor = next(
                (n for n in kept_sorted[idx:] if lines[n - 1].strip()), line_no
            )
            marker = f"{_marker_indent(lines, previous, anchor)}[pruned]"
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
    referenced symbols, and necessary imports. The rendered result is
    verified with `ast.parse`; callers must treat `parses=False` output
    as untrusted and fall back to the unrepaired render.
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
        changed = _expand_statement_spans(tree, kept, reasons) or changed
        changed = _expand_enclosing_scopes(tree, lines, kept, reasons) or changed
        changed = _expand_referenced_top_level(tree, lines, kept, reasons) or changed

    _expand_imports(tree, lines, kept, reasons)

    repaired_code = render_filtered(lines, kept)
    try:
        ast.parse(repaired_code)
        parses = True
    except SyntaxError:
        parses = False

    return RepairResult(
        kept_lines=kept,
        reasons=reasons,
        repaired_code=repaired_code,
        parses=parses,
    )
