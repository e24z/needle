"""Local SWE-bench validation report helpers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from benchmarks.swebench import run as run_mod
from benchmarks.swebench.run import (
    _django_runtest_labels,
    _load_predictions,
    _policy_id_for_mode,
    _validation_job_status,
    _summarize_validation,
    _validation_command,
    _validation_test_ids,
    _write_result_summary,
)


def test_load_predictions_accepts_keyed_preds_json() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "preds.json"
        path.write_text(
            json.dumps(
                {
                    "repo__repo-1": {
                        "instance_id": "repo__repo-1",
                        "model_name_or_path": "openai/test",
                        "model_patch": "diff --git a/x b/x\n",
                    }
                }
            )
        )

        preds = _load_predictions(path)

    assert preds == [
        {
            "instance_id": "repo__repo-1",
            "model_name_or_path": "openai/test",
            "model_patch": "diff --git a/x b/x\n",
        }
    ]


def test_validation_test_ids_uses_fail_to_pass_and_pass_to_pass() -> None:
    assert _validation_test_ids(
        {
            "FAIL_TO_PASS": json.dumps(["tests/test_fix.py::test_new[case]"]),
            "PASS_TO_PASS": json.dumps(["tests/test_old.py::test_old"]),
        }
    ) == ["tests/test_fix.py::test_new[case]", "tests/test_old.py::test_old"]


def test_validation_command_applies_model_and_test_patches() -> None:
    cmd = _validation_command(
        "diff --git a/x b/x\n",
        "diff --git a/tests/test_x.py b/tests/test_x.py\n",
        ["tests/test_x.py::test_new[case]"],
    )

    assert "git apply /tmp/model.patch" in cmd
    assert "git apply /tmp/test.patch" in cmd
    assert "pytest -q 'tests/test_x.py::test_new[case]' --tb=short" in cmd


def test_django_runtest_labels_convert_unittest_names() -> None:
    runnable, skipped = _django_runtest_labels(
        [
            "test_callable_path (model_fields.test_filepathfield.FilePathFieldTests)",
            "assertRaisesMessage shouldn't interpret RE special chars.",
        ]
    )

    assert runnable == [
        "model_fields.test_filepathfield.FilePathFieldTests.test_callable_path"
    ]
    assert skipped == ["assertRaisesMessage shouldn't interpret RE special chars."]


def test_validation_command_uses_django_runtests() -> None:
    cmd = _validation_command(
        "diff --git a/x b/x\n",
        "diff --git a/tests/model_fields/test_filepathfield.py b/tests/model_fields/test_filepathfield.py\n",
        [
            "test_callable_path (model_fields.test_filepathfield.FilePathFieldTests)",
            "assertRaisesMessage shouldn't interpret RE special chars.",
        ],
        repo="django/django",
    )

    assert "pytest -q" not in cmd
    assert (
        "python tests/runtests.py --verbosity 1 "
        "model_fields.test_filepathfield.FilePathFieldTests.test_callable_path"
    ) in cmd
    assert "::hay-validation::skipped-unrunnable-test::" in cmd


def test_validation_status_marks_missing_runner_as_error() -> None:
    assert _validation_job_status(127, "", "bash: pytest: command not found") == "error"
    assert (
        _validation_job_status(1, "", "AssertionError: patch failed test")
        == "completed"
    )


def test_summarize_validation_separates_unresolved_from_errors() -> None:
    summary = _summarize_validation(
        [
            {"instance_id": "a", "job_status": "completed", "resolved": True},
            {"instance_id": "b", "job_status": "completed", "resolved": False},
            {"instance_id": "c", "job_status": "error", "resolved": False},
        ]
    )

    assert summary["submitted_instances"] == 3
    assert summary["completed_instances"] == 2
    assert summary["resolved_instances"] == 1
    assert summary["unresolved_instances"] == 1
    assert summary["error_instances"] == 1
    assert summary["resolved_ids"] == ["a"]
    assert summary["unresolved_ids"] == ["b"]
    assert summary["error_ids"] == ["c"]


def test_slice_id_loads_frozen_instance_filter() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        slices = Path(tmpdir)
        (slices / "lite-test-demo.json").write_text(
            json.dumps(
                {
                    "id": "lite-test-demo",
                    "subset": "lite",
                    "split": "test",
                    "instance_ids": ["django__django-1", "astropy__astropy-2"],
                }
            )
        )
        old = run_mod.DEFAULT_SLICES_ROOT
        run_mod.DEFAULT_SLICES_ROOT = slices
        args = SimpleNamespace(
            slice_id="lite-test-demo",
            subset="lite",
            split="test",
            filter="",
            slice="0:1",
        )
        try:
            data = run_mod._load_slice(args)
        finally:
            run_mod.DEFAULT_SLICES_ROOT = old

    assert data["id"] == "lite-test-demo"
    assert args.slice == ":"
    assert args.filter == "^(?:django__django\\-1|astropy__astropy\\-2)$"


def test_policy_id_names_baseline_and_hay_knobs() -> None:
    args = SimpleNamespace(policy_id="")
    assert _policy_id_for_mode(args, {}, "baseline") == "baseline"
    assert _policy_id_for_mode(args, {"HAY_MAX_LENGTH": "8192"}, "hay") == (
        "hay-8192-floorless"
    )
    assert _policy_id_for_mode(
        args, {"HAY_MAX_LENGTH": "2048", "HAY_CHUNK_OVERLAP_TOKENS": "50"}, "hay"
    ) == "hay-2048-chunked-floorless"


def test_result_summary_exports_compact_run_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "runs" / "run-1"
        mode_dir = root / "hay" / "django__django-1"
        mode_dir.mkdir(parents=True)
        (root / "hay" / "preds.json").write_text(
            json.dumps(
                {
                    "django__django-1": {
                        "model_name_or_path": "openai/test",
                        "model_patch": "diff --git a/x b/x\n",
                    }
                }
            )
        )
        (mode_dir / "django__django-1.traj.json").write_text(
            json.dumps(
                {
                    "info": {
                        "exit_status": "Submitted",
                        "model_stats": {"instance_cost": 0.12, "api_calls": 3},
                    }
                }
            )
        )
        (root / "hay" / "local-validation.json").write_text(
            json.dumps(
                {
                    "summary": {
                        "submitted_instances": 1,
                        "resolved_instances": 1,
                    }
                }
            )
        )
        (root / "hay" / "hay_telemetry.jsonl").write_text(
            json.dumps(
                {
                    "accepted": True,
                    "chunked": True,
                    "chunks": 2,
                    "saved_tokens": 10,
                    "model_input_tokens": 30,
                }
            )
            + "\n"
        )
        args = SimpleNamespace(
            results_output=str(Path(tmpdir) / "results"),
            slice_id="lite-test-demo",
            backend="modal",
            model="gpt-5.5",
            subset="lite",
            split="test",
            slice=":",
            filter="^django",
            policy_id="hay-2048-chunked",
        )
        manifest = {"run_id": "run-1", "git": {"short_commit": "abc123"}}

        out = Path(
            _write_result_summary(
                args=args,
                root=root,
                manifest=manifest,
                mode="hay",
                mode_meta={
                    "policy": {
                        "id": "hay-2048-chunked",
                        "env": {"HAY_MAX_LENGTH": "2048"},
                    }
                },
                env={"HAY_MAX_LENGTH": "2048"},
            )
        )
        data = json.loads(out.read_text())

    assert data["slice_id"] == "lite-test-demo"
    assert data["policy"]["id"] == "hay-2048-chunked"
    assert data["totals"] == {"api_calls": 3, "cost": 0.12, "predictions": 1}
    assert data["local_validation"]["resolved_instances"] == 1
    assert data["telemetry"]["chunked"] == 1
    assert data["telemetry"]["low_memory_passthrough"] == 0


if __name__ == "__main__":
    test_load_predictions_accepts_keyed_preds_json()
    test_validation_test_ids_uses_fail_to_pass_and_pass_to_pass()
    test_validation_command_applies_model_and_test_patches()
    test_django_runtest_labels_convert_unittest_names()
    test_validation_command_uses_django_runtests()
    test_validation_status_marks_missing_runner_as_error()
    test_summarize_validation_separates_unresolved_from_errors()
    test_slice_id_loads_frozen_instance_filter()
    test_policy_id_names_baseline_and_hay_knobs()
    test_result_summary_exports_compact_run_evidence()
    print("test_benchmark_validation OK")
