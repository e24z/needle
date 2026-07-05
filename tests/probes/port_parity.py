"""Optional port-parity probe for upstream SWE-Pruner vs Needle MLX.

This is deliberately outside the normal test suite. It compares the line masks
from the upstream PyTorch implementation against Needle's MLX port on identical
``(observation, goal hint)`` pairs.

Examples:

    # Validate case extraction and report shape without loading models.
    python3 tests/probes/port_parity.py --synthetic --backends none

    # Validate the local diagnostic fixture shape without loading models.
    python3 tests/probes/port_parity.py \
      --cases tests/probes/fixtures/port_parity_cases.jsonl \
      --backends none

    # MLX-only smoke with the local Needle worker venv.
    PYTHONPATH=python NEEDLE_MODEL_DIR=/path/to/code-pruner \
      /tmp/needle-pr37-home/python/venv/bin/python tests/probes/port_parity.py \
      --synthetic --backends mlx --limit 1

    # Full parity against a local checkout of https://github.com/Ayanami1314/swe-pruner.
    PYTHONPATH=python /path/to/python-with-torch-and-mlx tests/probes/port_parity.py \
      --cases trajectories.jsonl \
      --upstream-repo /tmp/swe-pruner-upstream/swe-pruner \
      --model-dir /path/to/code-pruner \
      --backends torch,mlx --torch-device mps

    # Split-env parity when Torch/MPS and MLX live in different virtualenvs.
    /path/to/torch-venv/bin/python tests/probes/port_parity.py \
      --cases tests/probes/fixtures/port_parity_cases.jsonl \
      --model-dir /path/to/code-pruner \
      --backends torch --torch-device mps --max-length 512 \
      --output /tmp/needle-port-parity-torch.json

    PYTHONPATH=python /path/to/needle-worker-venv/bin/python tests/probes/port_parity.py \
      --cases tests/probes/fixtures/port_parity_cases.jsonl \
      --model-dir /path/to/code-pruner \
      --backends mlx --max-length 512 \
      --output /tmp/needle-port-parity-mlx.json

    python3 tests/probes/port_parity.py \
      --merge-reports /tmp/needle-port-parity-torch.json /tmp/needle-port-parity-mlx.json \
      --output /tmp/needle-port-parity-merged.json
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "python"

QUERY_KEYS = (
    "query",
    "goal_hint",
    "goal",
    "hint",
    "context_focus_question",
    "question",
)
TEXT_KEYS = (
    "code",
    "text",
    "observation",
    "output",
    "tool_output",
    "content",
    "stdout",
)


@dataclass(frozen=True)
class Case:
    case_id: str
    query: str
    text: str
    source: str
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()


@dataclass
class MaskResult:
    backend: str
    kept_lines: list[int]
    line_count: int
    pruned_sha256: str | None = None
    elapsed_ms: float | None = None
    stats: dict[str, Any] | None = None
    skipped: str | None = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _extract_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return None


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _first_string(record: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        if key not in record:
            continue
        value = _extract_text(record[key])
        if value and value.strip():
            return value
    return None


def _load_json_records(path: Path) -> list[Any]:
    if path.suffix == ".jsonl":
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("cases", "records", "trajectories", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    raise ValueError(f"unsupported JSON root in {path}")


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _cases_from_record(record: Any, *, index: int, source: str) -> Iterator[Case]:
    for pos, item in enumerate(_walk_dicts(record)):
        query = _first_string(item, QUERY_KEYS)
        text = _first_string(item, TEXT_KEYS)
        if not query or not text:
            continue
        if len(text) < 20 or len(query) < 8:
            continue
        case_id = str(item.get("id") or item.get("case_id") or f"{index}:{pos}")
        yield Case(
            case_id=case_id,
            query=query.strip(),
            text=text,
            source=source,
            must_contain=_string_list(item.get("must_contain")),
            must_not_contain=_string_list(item.get("must_not_contain")),
        )


def load_cases(paths: list[Path], *, synthetic: bool, limit: int) -> list[Case]:
    cases: list[Case] = []
    if synthetic:
        cases.extend(synthetic_cases())
    for path in paths:
        for index, record in enumerate(_load_json_records(path)):
            cases.extend(_cases_from_record(record, index=index, source=str(path)))

    seen: set[tuple[str, str]] = set()
    unique: list[Case] = []
    for case in cases:
        key = (_sha(case.text), case.query)
        if key in seen:
            continue
        seen.add(key)
        unique.append(case)
        if limit and len(unique) >= limit:
            break
    return unique


def synthetic_cases() -> list[Case]:
    text = "\n".join(
        [
            "import json",
            "",
            "DEFAULTS = {'timeout': 30, 'retries': 2}",
            "",
            "def load_config(path):",
            "    raw = json.loads(open(path).read())",
            "    merged = {**DEFAULTS, **raw}",
            "    validate_required_keys(merged)",
            "    return merged",
            "",
            "def validate_required_keys(config):",
            "    missing = [key for key in ('api_key', 'endpoint') if not config.get(key)]",
            "    if missing:",
            "        raise ValueError('missing config keys: ' + ', '.join(missing))",
            "",
            "def unrelated_helper(payload):",
            "    return {'value': payload.get('value'), 'ok': True}",
        ]
    )
    tail_lines: list[str] = []
    for index in range(140):
        tail_lines.extend(
            [
                f"def unrelated_prefix_{index}(value):",
                f"    return value + {index}",
                "",
            ]
        )
    tail_lines.extend(
        [
            "class TailRateLimitError(Exception):",
            "    pass",
            "",
            "def parse_retry_after_tail(headers):",
            "    value = headers.get('retry-after')",
            "    return int(value) if value and value.isdigit() else None",
            "",
            "def should_retry_tail(status_code, headers):",
            "    if status_code == 429:",
            "        raise TailRateLimitError(parse_retry_after_tail(headers))",
            "    return status_code >= 500",
        ]
    )
    tail_text = "\n".join(tail_lines)
    return [
        Case(
            case_id="synthetic-config-validation",
            query="Where does configuration validation raise errors for missing required keys?",
            text=text,
            source="synthetic",
            must_contain=("def validate_required_keys", "missing config keys"),
            must_not_contain=("def unrelated_helper",),
        ),
        Case(
            case_id="synthetic-tail-relevance",
            query="Find retry-after parsing and retry decision code near the end of the file.",
            text=tail_text,
            source="synthetic",
            must_contain=(
                "class TailRateLimitError",
                "def parse_retry_after_tail",
                "def should_retry_tail",
            ),
            must_not_contain=("def unrelated_prefix_0",),
        ),
    ]


def _integrity_stats(case: Case, pruned: str) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if case.must_contain:
        stats["missing_must_contain"] = [
            needle for needle in case.must_contain if needle not in pruned
        ]
    if case.must_not_contain:
        stats["present_must_not_contain"] = [
            needle for needle in case.must_not_contain if needle in pruned
        ]
    return stats


def _all_lines(case: Case, backend: str, reason: str) -> MaskResult:
    return MaskResult(
        backend=backend,
        kept_lines=list(range(1, _line_count(case.text) + 1)),
        line_count=_line_count(case.text),
        skipped=reason,
    )


class MlxRunner:
    def __init__(
        self,
        *,
        model_dir: str | None,
        max_batch_size: int,
        max_batch_tokens: int | None,
    ):
        if str(PYTHON) not in sys.path:
            sys.path.insert(0, str(PYTHON))
        from needle_worker.soft_lamr.model import MLXSwePrunerBackend

        self.impl = MLXSwePrunerBackend(
            model_name=model_dir or _required_model_dir(),
            repair=False,
        )
        self.max_batch_size = max_batch_size
        self.max_batch_tokens = max_batch_tokens

    def score(self, case: Case, *, threshold: float, max_length: int) -> MaskResult:
        from needle_worker.soft_lamr.batching import (
            score_batches_with_retry,
            split_batches_by_padded_token_budget,
        )
        from needle_worker.soft_lamr.chunking import (
            bucket_token_chunks,
            estimate_token_count,
            merge_token_scores_from_chunks,
            split_text_into_token_chunks,
        )
        from needle_worker.soft_lamr.lines import (
            aggregate_token_scores_to_lines,
            prune_code_lines,
        )
        from needle_worker.soft_lamr.model import _MIN_CODE_TOKENS, _is_mlx_resource_error

        started = time.perf_counter()
        prefix_ids, query_ids, suffix_ids = self.impl._prompt_token_ids(case.query)
        prompt_tokens = len(prefix_ids) + len(query_ids) + len(suffix_ids)
        original_tokens = estimate_token_count(case.text, self.impl.tokenizer)
        if max_length <= 0:
            max_length = 8192
        code_token_budget = max_length - prompt_tokens
        if code_token_budget < _MIN_CODE_TOKENS:
            return _all_lines(case, "mlx", "query-too-long")

        chunks = split_text_into_token_chunks(
            case.text,
            self.impl.tokenizer,
            chunk_max_tokens=code_token_budget,
            overlap_tokens=50,
        )
        prepared_batches = [
            [
                self.impl._prepare_chunk_row(
                    chunk=chunk,
                    prefix_ids=prefix_ids,
                    query_ids=query_ids,
                    suffix_ids=suffix_ids,
                    max_length=max_length,
                )
                for chunk in batch
            ]
            for batch in bucket_token_chunks(chunks, max_batch_size=self.max_batch_size)
        ]
        budgeted = split_batches_by_padded_token_budget(
            prepared_batches,
            max_padded_tokens=self.max_batch_tokens,
            length_fn=lambda item: item.real_len,
        )

        def score_batch(prepared: list[Any]):
            return self.impl._score_prepared_batch(
                prepared,
                max_length=max_length,
                use_viterbi=False,
                profile_forced_eval=False,
            )

        scored_results, batch_stats, retry_summary = score_batches_with_retry(
            budgeted.batches,
            score_batch,
            _is_mlx_resource_error,
        )
        token_scores, offsets = merge_token_scores_from_chunks(
            case.text,
            [
                (scores, token_offsets, start_char)
                for _score, scores, token_offsets, start_char in scored_results
            ],
        )
        line_scores = aggregate_token_scores_to_lines(case.text, token_scores, offsets)
        pruned, kept_lines = prune_code_lines(case.text, line_scores, threshold, False)
        stats = {
            "original_tokens": original_tokens,
            "chunks": len(chunks),
            "batches": len(batch_stats),
            "batch_retry_count": retry_summary.get("batch_retry_count", 0),
            **_integrity_stats(case, pruned),
        }
        return MaskResult(
            backend="mlx",
            kept_lines=kept_lines,
            line_count=_line_count(case.text),
            pruned_sha256=_sha(pruned),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            stats=stats,
        )


class TorchRunner:
    def __init__(self, *, upstream_repo: Path, model_dir: str | None, device_name: str):
        src = upstream_repo / "src"
        if src.exists():
            sys.path.insert(0, str(src))
        else:
            print(
                f"warning: upstream src directory not found at {src}; "
                "using installed swe_pruner package",
                file=sys.stderr,
            )

        import torch
        from swe_pruner.prune_wrapper import SwePrunerForCodePruning

        self.torch = torch
        self.model = SwePrunerForCodePruning.from_pretrained(
            model_dir or _required_model_dir(),
            trust_remote_code=True,
        )
        device = self._select_device(device_name)
        self.model.to(device)
        self.model._device = device
        self.model.eval()

    def _select_device(self, requested: str):
        torch = self.torch
        mps = getattr(torch.backends, "mps", None)
        if requested == "auto":
            if mps is not None and mps.is_available():
                requested = "mps"
            elif torch.cuda.is_available():
                requested = "cuda"
            else:
                requested = "cpu"
        if requested == "mps" and (mps is None or not mps.is_available()):
            raise RuntimeError("requested torch MPS, but torch.backends.mps is not available")
        if requested == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("requested CUDA, but torch.cuda is not available")
        return torch.device(requested)

    def score(self, case: Case, *, threshold: float, max_length: int) -> MaskResult:
        from swe_pruner.prune_wrapper import (
            aggregate_token_scores_to_lines,
            estimate_token_count,
            format_instruction,
            merge_token_scores_from_chunks,
            prune_code_lines,
            split_code_into_chunks,
        )

        started = time.perf_counter()
        max_length = max_length if max_length > 0 else 8192
        tokenizer = self.model.tokenizer

        # Upstream prune() currently hardcodes 8192 internally. Reproduce its
        # chunk-aware body here so --max-length can stress chunk coverage.
        formatted_query = format_instruction(None, case.query)
        query_tokens = estimate_token_count(formatted_query, tokenizer)
        code_tokens = estimate_token_count(case.text, tokenizer)
        prefix = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        prefix_tokens = tokenizer.encode(prefix, add_special_tokens=False)
        suffix_tokens = tokenizer.encode(suffix, add_special_tokens=False)
        available_length = max_length - len(prefix_tokens) - len(suffix_tokens)
        code_max_tokens = available_length - query_tokens

        if code_max_tokens < 100:
            result = _all_lines(case, "torch", "query-too-long")
            result.stats = {
                "origin_token_cnt": code_tokens,
                "left_token_cnt": code_tokens,
                "model_input_token_cnt": 0,
                "device": str(self.model._device),
                "code_max_tokens": code_max_tokens,
                "error_msg": "Query too long, insufficient space for code processing.",
            }
            return result

        if code_tokens > code_max_tokens:
            chunks = split_code_into_chunks(
                case.text,
                tokenizer,
                chunk_max_tokens=code_max_tokens,
                overlap_tokens=50,
            )
            chunk_scores = []
            chunk_results = []
            for chunk_text, start_char, end_char in chunks:
                chunk_score, token_scores, offsets = self.model._process_single_chunk(
                    case.query,
                    chunk_text,
                    tokenizer,
                    max_length=max_length,
                    instruction=self.model.instruction,
                )
                chunk_scores.append(chunk_score)
                chunk_results.append((token_scores, offsets, start_char, end_char))
            score = max(chunk_scores) if chunk_scores else 0.0
            code_token_scores, code_token_offsets = merge_token_scores_from_chunks(
                case.text, chunk_results
            )
        else:
            chunks = [(case.text, 0, len(case.text))]
            score, code_token_scores, code_token_offsets = self.model._process_single_chunk(
                case.query,
                case.text,
                tokenizer,
                max_length=max_length,
                instruction=self.model.instruction,
            )

        line_scores = aggregate_token_scores_to_lines(
            case.text,
            code_token_scores,
            code_token_offsets,
        )
        pruned, kept_lines = prune_code_lines(case.text, line_scores, threshold, False)
        left_tokens = estimate_token_count(pruned, tokenizer)
        return MaskResult(
            backend="torch",
            kept_lines=list(kept_lines),
            line_count=_line_count(case.text),
            pruned_sha256=_sha(pruned),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            stats={
                "origin_token_cnt": code_tokens,
                "left_token_cnt": left_tokens,
                "model_input_token_cnt": query_tokens
                + code_tokens
                + len(prefix_tokens)
                + len(suffix_tokens),
                "score": score,
                "device": str(self.model._device),
                "chunks": len(chunks),
                "code_max_tokens": code_max_tokens,
                **_integrity_stats(case, pruned),
            },
        )


def _required_model_dir() -> str:
    value = os.environ.get("NEEDLE_MODEL_DIR")
    if not value:
        raise RuntimeError("pass --model-dir or set NEEDLE_MODEL_DIR")
    return value


def compare_masks(case: Case, results: dict[str, MaskResult]) -> dict[str, Any]:
    torch_result = results.get("torch")
    mlx_result = results.get("mlx")
    if not torch_result or not mlx_result:
        return {}
    line_count = max(torch_result.line_count, mlx_result.line_count)
    torch_set = set(torch_result.kept_lines)
    mlx_set = set(mlx_result.kept_lines)
    labels = range(1, line_count + 1)
    same = sum((line in torch_set) == (line in mlx_set) for line in labels)
    both_kept = len(torch_set & mlx_set)
    union = len(torch_set | mlx_set)
    return {
        "exact": torch_set == mlx_set,
        "line_count": line_count,
        "line_agreement": same / line_count if line_count else 1.0,
        "jaccard": both_kept / union if union else 1.0,
        "torch_kept": len(torch_set),
        "mlx_kept": len(mlx_set),
        "both_kept": both_kept,
        "torch_only": sorted(torch_set - mlx_set),
        "mlx_only": sorted(mlx_set - torch_set),
    }


def compare_result_masks(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    torch_result = results.get("torch")
    mlx_result = results.get("mlx")
    if not torch_result or not mlx_result:
        return {}
    line_count = max(
        int(torch_result.get("line_count") or 0),
        int(mlx_result.get("line_count") or 0),
    )
    torch_set = set(int(line) for line in torch_result.get("kept_lines", []))
    mlx_set = set(int(line) for line in mlx_result.get("kept_lines", []))
    labels = range(1, line_count + 1)
    same = sum((line in torch_set) == (line in mlx_set) for line in labels)
    both_kept = len(torch_set & mlx_set)
    union = len(torch_set | mlx_set)
    return {
        "exact": torch_set == mlx_set,
        "line_count": line_count,
        "line_agreement": same / line_count if line_count else 1.0,
        "jaccard": both_kept / union if union else 1.0,
        "torch_kept": len(torch_set),
        "mlx_kept": len(mlx_set),
        "both_kept": both_kept,
        "torch_only": sorted(torch_set - mlx_set),
        "mlx_only": sorted(mlx_set - torch_set),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    comparisons = [row["comparison"] for row in rows if row.get("comparison")]
    if not comparisons:
        return {"cases": len(rows), "comparisons": 0}
    return {
        "cases": len(rows),
        "comparisons": len(comparisons),
        "exact_mask_rate": sum(1 for item in comparisons if item["exact"]) / len(comparisons),
        "mean_line_agreement": sum(item["line_agreement"] for item in comparisons) / len(comparisons),
        "mean_jaccard": sum(item["jaccard"] for item in comparisons) / len(comparisons),
        "total_lines": sum(item["line_count"] for item in comparisons),
    }


def merge_reports(paths: list[Path]) -> dict[str, Any]:
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    model_dirs: list[str] = []
    backends: list[str] = []
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("model_dir"):
            model_dirs.append(str(report["model_dir"]))
        backends.extend(str(item) for item in report.get("backends", []))
        for row in report.get("rows", []):
            case = row.get("case", {})
            key = (
                str(case.get("id")),
                str(case.get("text_sha256")),
                str(case.get("query")),
            )
            merged = rows_by_key.setdefault(
                key,
                {
                    "case": case,
                    "results": {},
                    "comparison": {},
                },
            )
            merged["results"].update(row.get("results", {}))

    rows = list(rows_by_key.values())
    for row in rows:
        row["comparison"] = compare_result_masks(row.get("results", {}))
    return {
        "ok": True,
        "model_dir": sorted(set(model_dirs)),
        "backends": sorted(set(backends)),
        "summary": summarize(rows),
        "rows": rows,
        "merged_reports": [str(path) for path in paths],
    }


def make_runner(name: str, args: argparse.Namespace) -> Any:
    if name == "torch":
        return TorchRunner(
            upstream_repo=args.upstream_repo,
            model_dir=args.model_dir or None,
            device_name=args.torch_device,
        )
    if name == "mlx":
        return MlxRunner(
            model_dir=args.model_dir or None,
            max_batch_size=args.mlx_batch_size,
            max_batch_tokens=args.mlx_max_batch_tokens or None,
        )
    raise ValueError(f"unknown backend: {name}")


def release_runner(name: str, runner: Any) -> None:
    evict = getattr(runner, "evict", None)
    if callable(evict):
        evict()
    del runner
    gc.collect()
    if name == "torch":
        try:
            import torch

            mps = getattr(torch.backends, "mps", None)
            if mps is not None and mps.is_available():
                torch.mps.empty_cache()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    elif name == "mlx":
        try:
            from needle_worker.soft_lamr.model import _clear_mlx_cache

            _clear_mlx_cache()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", action="append", default=[], type=Path, help="JSON/JSONL case or trajectory file.")
    parser.add_argument("--synthetic", action="store_true", help="Include one synthetic fixture.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum cases after de-duplication.")
    parser.add_argument("--backends", default="torch,mlx", help="Comma list: torch,mlx,none.")
    parser.add_argument("--model-dir", default=os.environ.get("NEEDLE_MODEL_DIR", ""))
    parser.add_argument("--upstream-repo", type=Path, default=Path("/tmp/swe-pruner-upstream/swe-pruner"))
    parser.add_argument("--torch-device", default="auto", choices=["auto", "mps", "cuda", "cpu"])
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--max-length", type=int, default=8192)
    parser.add_argument("--mlx-batch-size", type=int, default=1)
    parser.add_argument("--mlx-max-batch-tokens", type=int, default=0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/needle-port-parity-report.json"),
    )
    parser.add_argument("--merge-reports", nargs="*", type=Path, help="Merge backend-specific reports and compare masks.")
    parser.add_argument("--strict", action="store_true", help="Fail when any compared mask differs.")
    args = parser.parse_args(argv)

    if args.merge_reports:
        report = merge_reports(args.merge_reports)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report["summary"], indent=2, sort_keys=True))
        print(f"wrote {args.output}")
        if args.strict and any(row.get("comparison", {}).get("exact") is False for row in report["rows"]):
            return 1
        return 0

    cases = load_cases(args.cases, synthetic=args.synthetic or not args.cases, limit=args.limit)
    if not cases:
        raise SystemExit("no cases found")

    backend_names = {item.strip() for item in args.backends.split(",") if item.strip()}
    backend_order = [
        name
        for name in ("torch", "mlx")
        if name in backend_names and "none" not in backend_names
    ]
    results_by_case: list[dict[str, MaskResult]] = [dict() for _case in cases]
    if "none" not in backend_names:
        unknown = backend_names - {"torch", "mlx"}
        if unknown:
            raise SystemExit(f"unknown backend(s): {', '.join(sorted(unknown))}")
        for name in backend_order:
            runner = make_runner(name, args)
            try:
                for index, case in enumerate(cases):
                    results_by_case[index][name] = runner.score(
                        case,
                        threshold=args.threshold,
                        max_length=args.max_length,
                    )
            finally:
                release_runner(name, runner)

    rows: list[dict[str, Any]] = []
    for case, results in zip(cases, results_by_case, strict=True):
        row = {
            "case": {
                "id": case.case_id,
                "source": case.source,
                "text_sha256": _sha(case.text),
                "query": case.query,
                "line_count": _line_count(case.text),
                "must_contain": list(case.must_contain),
                "must_not_contain": list(case.must_not_contain),
            },
            "results": {name: asdict(result) for name, result in results.items()},
            "comparison": compare_masks(case, results),
        }
        rows.append(row)

    report = {
        "ok": True,
        "model_dir": args.model_dir or os.environ.get("NEEDLE_MODEL_DIR"),
        "backends": backend_order,
        "summary": summarize(rows),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    print(f"wrote {args.output}")

    if args.strict and any(row.get("comparison", {}).get("exact") is False for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
