#!/usr/bin/env python3
"""Run Mini-SWE on SWE-bench, locally validate predictions, optionally submit."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, Template
from minisweagent.config import get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import (
    DATASET_MAPPING,
    filter_instances,
    get_swebench_docker_image_name,
    remove_from_preds_file,
    update_preds_file,
)
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.run.benchmarks.utils.common import ProgressTrackingAgent
from minisweagent.utils.log import add_file_handler, logger
from minisweagent.utils.serialize import UNSET, recursive_merge
from rich.live import Live

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_ROOT = REPO_ROOT / "benchmarks" / "runs"
DEFAULT_SLICES_ROOT = REPO_ROOT / "benchmarks" / "slices"
DEFAULT_RESULTS_ROOT = REPO_ROOT / "benchmarks" / "results"
VIBEPROXY_CONFIG = REPO_ROOT / "benchmarks" / "swebench" / "vibeproxy.yaml"
sys.path.insert(0, str(REPO_ROOT))

_TOOL_DIRS = [
    Path.home() / ".local" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
]

from pruner import client  # noqa: E402
from benchmarks.swebench.hay_environment import BenchmarkAbortError  # noqa: E402

SB_SUBSETS = {
    "lite": "swe-bench_lite",
    "verified": "swe-bench_verified",
    "full": "swe-bench-m",
}

HAY_POLICY_ENV_KEYS = (
    "HAY_MAX_LENGTH",
    "HAY_CHUNK_OVERLAP_TOKENS",
    "HAY_PRUNE_FLOOR",
    "HAY_SILENT_PRUNE",
    "HAY_REPAIR",
    "HAY_THRESHOLD",
    "HAY_MIN_FREE_MB",
    "HAY_BENCH_MIN_CHARS",
    "HAY_BENCH_MIN_SAVINGS_RATIO",
    "HAY_BENCH_PRUNE_TIMEOUT",
    "HAY_BENCH_ABORT_ON_LOW_MEMORY",
)

KNOWN_POLICIES: dict[str, dict[str, str]] = {
    "baseline": {},
    "hay-8192-floorless": {
        "HAY_MAX_LENGTH": "8192",
        "HAY_CHUNK_OVERLAP_TOKENS": "50",
        "HAY_PRUNE_FLOOR": "0",
    },
    "hay-2048-chunked": {
        "HAY_MAX_LENGTH": "2048",
        "HAY_CHUNK_OVERLAP_TOKENS": "50",
        "HAY_PRUNE_FLOOR": "0",
    },
}


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _has_query_hint(action: dict) -> bool:
    return any(
        isinstance(action.get(key), str) and action[key].strip()
        for key in ("context_focus_question", "hay_query", "query")
    )


class CheckpointingAgent(ProgressTrackingAgent):
    """Save the trajectory atomically so the live dashboard never reads a torn file.

    Mini-SWE's run loop already calls ``save(config.output_path)`` after every step;
    pointing ``output_path`` at the instance's ``.traj.json`` (see ``_process_instance``)
    turns that into a live per-turn checkpoint. We only override the write to be atomic
    (temp file + ``os.replace``) so a concurrent dashboard read can't catch a half-written
    file. This is pure observability — it does not touch the model, env, or pruning, so
    baseline/Hay parity holds as long as both modes use it.
    """

    def save(self, path: Path | None, *extra_dicts: dict) -> dict:
        if path is None:
            return super().save(path, *extra_dicts)
        # Bulletproof temp name — with_suffix() rejects multi-dot suffixes on some
        # Python versions, and this runs in run()'s finally every turn, so a raise
        # here would crash the instance. Append, don't reparse the suffix.
        tmp = path.parent / (path.name + ".tmp")
        data = super().save(tmp, *extra_dicts)
        os.replace(tmp, path)
        return data


class HayBenchmarkAgent(CheckpointingAgent):
    """Forward Mini-SWE's latest assistant narration as the pruning goal hint."""

    def execute_actions(self, message: dict) -> list[dict]:
        query = _message_text(message)
        actions = []
        for action in message.get("extra", {}).get("actions", []):
            if isinstance(action, dict) and query and not _has_query_hint(action):
                action = {**action, "context_focus_question": query}
            actions.append(action)
        if actions:
            message.setdefault("extra", {})["actions"] = actions
        outputs = [self.env.execute(action) for action in actions]
        return self.add_messages(
            *self.model.format_observation_messages(
                message, outputs, self.get_template_vars()
            )
        )


def _model_name(model: str) -> str:
    return model if "/" in model else f"openai/{model}"


def _slug(value: str, *, fallback: str = "run") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _ids_filter(instance_ids: list[str]) -> str:
    return "^(?:" + "|".join(re.escape(instance_id) for instance_id in instance_ids) + ")$"


def _load_slice(args: argparse.Namespace) -> dict[str, Any]:
    slice_id = args.slice_id.strip()
    if not slice_id:
        return {}
    path = DEFAULT_SLICES_ROOT / f"{slice_id}.json"
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise SystemExit(f"error: slice id {slice_id!r} not found at {path}") from exc
    except ValueError as exc:
        raise SystemExit(f"error: slice file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"error: slice file must contain an object: {path}")
    instance_ids = data.get("instance_ids")
    if not isinstance(instance_ids, list) or not all(
        isinstance(item, str) and item for item in instance_ids
    ):
        raise SystemExit(f"error: slice {slice_id!r} must define string instance_ids")
    subset = str(data.get("subset", args.subset))
    split = str(data.get("split", args.split))
    if subset != args.subset or split != args.split:
        raise SystemExit(
            "error: slice "
            f"{slice_id!r} is for {subset}/{split}, but args request "
            f"{args.subset}/{args.split}"
        )
    expected_filter = _ids_filter(instance_ids)
    if args.filter and args.filter != expected_filter:
        raise SystemExit(
            f"error: --slice-id {slice_id} already defines a filter; "
            "omit --filter or use the exact slice filter"
        )
    if args.slice not in {"", ":", "0:1"}:
        raise SystemExit(
            f"error: --slice-id {slice_id} is already a frozen set; omit --slice"
        )
    args.filter = expected_filter
    args.slice = ":"
    return {**data, "id": slice_id, "path": str(path)}


def _env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _apply_known_policy(env: dict[str, str], policy_id: str) -> None:
    for key, value in KNOWN_POLICIES.get(policy_id, {}).items():
        env[key] = value


def _policy_env(env: dict[str, str]) -> dict[str, str]:
    return {key: env[key] for key in HAY_POLICY_ENV_KEYS if key in env}


def _policy_id_for_mode(args: argparse.Namespace, env: dict[str, str], mode: str) -> str:
    if mode == "baseline":
        return "baseline"
    requested = args.policy_id.strip()
    if requested:
        return requested
    max_length = str(env.get("HAY_MAX_LENGTH", "8192"))
    floor = "floor" if _env_truthy(env.get("HAY_PRUNE_FLOOR")) else "floorless"
    overlap = int(str(env.get("HAY_CHUNK_OVERLAP_TOKENS", "50") or "0"))
    chunk_part = "chunked" if overlap > 0 else "nochunk"
    if max_length == "8192":
        return f"hay-8192-{floor}"
    return f"hay-{_slug(max_length)}-{chunk_part}-{floor}"


def _default_run_id(args: argparse.Namespace, modes: list[str], output: Path) -> str:
    mode_part = "paired" if set(modes) == {"baseline", "hay"} else "-".join(modes)
    if args.slice_id or args.policy_id:
        scope = args.slice_id or (
            f"filter-{_slug(args.filter)}" if args.filter else f"slice-{_slug(args.slice)}"
        )
        policy = args.policy_id or "custom"
        policy_part = f"paired-{policy}" if set(modes) == {"baseline", "hay"} else (
            "baseline" if modes == ["baseline"] else policy
        )
        base = "__".join(
            [
                _slug(scope),
                _slug(policy_part, fallback="policy"),
                _slug(args.backend),
                _slug(args.model.split("/", 1)[-1]),
                time.strftime("%Y%m%d-%H%M%S"),
            ]
        )
        candidate = base
        suffix = 2
        while (output / candidate).exists():
            candidate = f"{base}-{suffix:02d}"
            suffix += 1
        return candidate
    if args.filter:
        scope = f"filter-{_slug(args.filter)}"
    elif args.slice:
        scope = f"slice-{_slug(args.slice)}"
    else:
        scope = "all"
    base = "-".join(
        [
            _slug(args.backend),
            _slug(mode_part, fallback="modes"),
            _slug(args.subset),
            _slug(args.split),
            scope,
            time.strftime("%Y%m%d-%H%M%S"),
        ]
    )
    candidate = base
    suffix = 2
    while (output / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _runtime_hay_home(run_id: str) -> str:
    digest = hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:8]
    suffix = _slug(run_id)[-24:] or "run"
    return str(Path("/tmp") / f"hay-bench-{suffix}-{digest}")


def _run_logged(cmd: list[str], *, env: dict[str, str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        return proc.wait()


def _tool_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for directory in _TOOL_DIRS:
        candidate = directory / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    searched = ", ".join(str(path) for path in _TOOL_DIRS)
    raise SystemExit(
        f"error: required command not found: {name}. Searched PATH and {searched}"
    )


def _require_tool(name: str) -> None:
    _tool_path(name)


def _check_modal_available() -> None:
    try:
        import modal  # noqa: F401
        import swerex  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "error: Modal support is missing; run `uv sync --extra bench` first"
        ) from exc


def _start_hay_session(env: dict[str, str], session_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "pruner", "session", "--session", session_id],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@contextmanager
def _temporary_environ(values: dict[str, str]):
    old_values = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _environment_class(backend: str, mode: str) -> str:
    if backend == "modal":
        if mode == "hay":
            return "benchmarks.swebench.hay_environment.HayModalEnvironment"
        return "benchmarks.swebench.hay_environment.BenchmarkModalEnvironment"
    if mode == "hay":
        return "benchmarks.swebench.hay_environment.HayDockerEnvironment"
    return "docker"


def _config_specs(args: argparse.Namespace) -> list[str]:
    specs = ["swebench.yaml"]
    if args.backend == "modal":
        specs.append("swebench_modal.yaml")
    specs.extend(
        [
            str(VIBEPROXY_CONFIG),
            f"model.model_kwargs.api_base={args.vibeproxy_url}",
        ]
    )
    return specs


def _build_config(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    configs = [get_config_from_spec(spec) for spec in _config_specs(args)]
    configs.append(
        {
            "environment": {
                "environment_class": _environment_class(args.backend, mode)
            },
            "model": {"model_name": _model_name(args.model), "model_class": "litellm"},
        }
    )
    return recursive_merge(*configs)


def _sets_swebench_image(environment_class: str) -> bool:
    return environment_class in {
        "docker",
        "swerex_modal",
    } or environment_class.endswith(
        ("HayDockerEnvironment", "BenchmarkModalEnvironment", "HayModalEnvironment")
    )


def _get_sb_environment(config: dict[str, Any], instance: dict[str, Any]):
    config = copy.deepcopy(config)
    env_config = config.setdefault("environment", {})
    env_config["environment_class"] = env_config.get("environment_class", "docker")
    image_name = get_swebench_docker_image_name(instance)
    if _sets_swebench_image(env_config["environment_class"]):
        env_config["image"] = image_name
    elif env_config["environment_class"] in {"singularity", "contree"}:
        env_config["image"] = "docker://" + image_name

    env = get_environment(env_config)
    if startup_command := config.get("run", {}).get("env_startup_command"):
        startup_command = Template(startup_command, undefined=StrictUndefined).render(
            **instance
        )
        out = env.execute({"command": startup_command})
        if out["returncode"] != 0:
            raise RuntimeError(f"Error executing startup command: {out}")
    return env


def _stop_environment(env: Any) -> None:
    if env is None:
        return
    try:
        if hasattr(env, "stop"):
            env.stop()
        elif hasattr(env, "cleanup"):
            env.cleanup()
    except Exception:
        logger.debug("Failed to stop environment", exc_info=True)


def _process_instance(
    instance: dict[str, Any],
    output_dir: Path,
    config: dict[str, Any],
    progress_manager: RunBatchProgressManager,
) -> None:
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    traj_path = instance_dir / f"{instance_id}.traj.json"
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    traj_path.unlink(missing_ok=True)
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Starting environment")

    agent = None
    env = None
    model = None
    exit_status = None
    result = None
    extra_info: dict[str, Any] = {}

    try:
        model = get_model(config=config.get("model", {}))
        env = _get_sb_environment(config, instance)
        environment_class = str(
            config.get("environment", {}).get("environment_class", "")
        )
        agent_class = (
            HayBenchmarkAgent if "Hay" in environment_class else CheckpointingAgent
        )
        # output_path drives Mini-SWE's per-step save loop; pointing it at the traj
        # file makes every turn land on disk live for the dashboard. Override here
        # (not in the shared config) so concurrent workers don't clobber each other.
        agent_kwargs = {**config.get("agent", {}), "output_path": traj_path}
        agent = agent_class(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **agent_kwargs,
        )
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")
    except BenchmarkAbortError as exc:
        logger.error(
            "Benchmark abort while processing instance %s: %s",
            instance_id,
            exc,
            exc_info=True,
        )
        exit_status, result = type(exc).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(exc)}
        raise
    except Exception as exc:
        logger.error(
            "Error processing instance %s: %s", instance_id, exc, exc_info=True
        )
        exit_status, result = type(exc).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(exc)}
    finally:
        if agent is not None:
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            logger.info("Saved trajectory to '%s'", traj_path)
        _stop_environment(env)
        model_name = (
            model.config.model_name
            if model is not None
            else config.get("model", {}).get("model_name", "")
        )
        update_preds_file(
            output_dir / "preds.json", instance_id, model_name, result or ""
        )
        progress_manager.on_instance_end(instance_id, exit_status)


def _load_instances(args: argparse.Namespace) -> list[dict[str, Any]]:
    from datasets import load_dataset

    dataset_path = DATASET_MAPPING.get(args.subset, args.subset)
    logger.info("Loading dataset %s, split %s...", dataset_path, args.split)
    instances = list(load_dataset(dataset_path, split=args.split))
    return filter_instances(
        instances,
        filter_spec=args.filter,
        slice_spec=args.slice,
        shuffle=args.shuffle,
    )


def _run_miniswe(args: argparse.Namespace, out_dir: Path, mode: str) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    handler_index = len(logger.handlers)
    add_file_handler(out_dir / "minisweagent.log")
    try:
        (out_dir / "minisweagent.stdout.log").write_text(
            "Mini-SWE ran in-process; see minisweagent.log.\n"
        )

        instances = _load_instances(args)
        if not args.redo_existing and (out_dir / "preds.json").exists():
            existing_instances = list(
                json.loads((out_dir / "preds.json").read_text()).keys()
            )
            logger.info("Skipping %s existing instances", len(existing_instances))
            instances = [
                instance
                for instance in instances
                if instance["instance_id"] not in existing_instances
            ]
        logger.info("Running %s mode on %s instances...", mode, len(instances))

        if not instances and not (out_dir / "preds.json").exists():
            (out_dir / "preds.json").write_text("{}\n")

        config = _build_config(args, mode)
        environment_class = config.get("environment", {}).get("environment_class", "")
        progress_manager = RunBatchProgressManager(
            len(instances), out_dir / f"exit_statuses_{time.time()}.yaml"
        )

        def process_futures(futures: dict[concurrent.futures.Future, str]) -> None:
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except concurrent.futures.CancelledError:
                    pass
                except BenchmarkAbortError as exc:
                    instance_id = futures[future]
                    logger.error("Aborting benchmark from instance %s: %s", instance_id, exc)
                    progress_manager.on_uncaught_exception(instance_id, exc)
                    for pending in futures:
                        if pending is not future and not pending.done():
                            pending.cancel()
                    raise
                except Exception as exc:
                    instance_id = futures[future]
                    logger.error(
                        "Error in future for instance %s: %s",
                        instance_id,
                        exc,
                        exc_info=True,
                    )
                    progress_manager.on_uncaught_exception(instance_id, exc)

        with Live(progress_manager.render_group, refresh_per_second=4):
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=args.workers
            ) as executor:
                futures = {
                    executor.submit(
                        _process_instance, instance, out_dir, config, progress_manager
                    ): instance["instance_id"]
                    for instance in instances
                }
                try:
                    process_futures(futures)
                except KeyboardInterrupt:
                    logger.info(
                        "Cancelling all pending jobs. Press ^C again to exit immediately."
                    )
                    for future in futures:
                        if not future.running() and not future.done():
                            future.cancel()
                    process_futures(futures)

        return {
            "exit_code": 0,
            "environment_backend": args.backend,
            "environment_class": environment_class,
            "config_specs": _config_specs(args),
            "instances_selected": len(instances),
        }
    finally:
        for handler in logger.handlers[handler_index:]:
            logger.removeHandler(handler)
            handler.close()


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        loaded = json.loads(value)
    else:
        loaded = value
    if not isinstance(loaded, list):
        raise ValueError(f"expected JSON list, got {type(loaded).__name__}")
    return [str(item) for item in loaded]


def _load_predictions(preds: Path) -> list[dict[str, str]]:
    data = json.loads(preds.read_text())
    if isinstance(data, list):
        return [
            {
                "instance_id": str(item["instance_id"]),
                "model_name_or_path": str(item.get("model_name_or_path", "")),
                "model_patch": str(item.get("model_patch", "")),
            }
            for item in data
        ]
    if isinstance(data, dict):
        return [
            {
                "instance_id": str(instance_id),
                "model_name_or_path": str(item.get("model_name_or_path", "")),
                "model_patch": str(item.get("model_patch", "")),
            }
            for instance_id, item in data.items()
        ]
    raise ValueError(f"unsupported predictions format in {preds}: {type(data).__name__}")


def _validation_test_ids(instance: dict[str, Any]) -> list[str]:
    return _json_list(instance.get("FAIL_TO_PASS", [])) + _json_list(
        instance.get("PASS_TO_PASS", [])
    )


_DJANGO_UNITTEST_LABEL_RE = re.compile(
    r"^(?P<method>[A-Za-z_][A-Za-z0-9_]*) \((?P<class>[A-Za-z_][A-Za-z0-9_.]*)\)$"
)


def _django_runtest_labels(test_ids: list[str]) -> tuple[list[str], list[str]]:
    runnable: list[str] = []
    skipped: list[str] = []
    for test_id in test_ids:
        match = _DJANGO_UNITTEST_LABEL_RE.match(test_id)
        if match:
            runnable.append(f"{match.group('class')}.{match.group('method')}")
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", test_id):
            runnable.append(test_id)
        else:
            skipped.append(test_id)
    return runnable, skipped


def _validation_test_plan(repo: str, test_ids: list[str]) -> dict[str, Any]:
    if repo == "django/django":
        runnable, skipped = _django_runtest_labels(test_ids)
        tests = " ".join(shlex.quote(test_id) for test_id in runnable)
        return {
            "runner": "django-runtests",
            "command": f"python tests/runtests.py --verbosity 1 {tests}",
            "runnable_test_ids": runnable,
            "skipped_test_ids": skipped,
        }

    tests = " ".join(shlex.quote(test_id) for test_id in test_ids)
    return {
        "runner": "pytest",
        "command": f"pytest -q {tests} --tb=short",
        "runnable_test_ids": test_ids,
        "skipped_test_ids": [],
    }


def _validation_command(
    model_patch: str, test_patch: str, test_ids: list[str], *, repo: str = ""
) -> str:
    model_patch_b64 = base64.b64encode(model_patch.encode("utf-8")).decode("ascii")
    test_patch_b64 = base64.b64encode(test_patch.encode("utf-8")).decode("ascii")
    plan = _validation_test_plan(repo, test_ids)
    skipped_b64 = base64.b64encode(
        "\n".join(plan["skipped_test_ids"]).encode("utf-8")
    ).decode("ascii")
    return f"""set -euo pipefail
python - <<'PY'
import base64
from pathlib import Path
Path('/tmp/model.patch').write_bytes(base64.b64decode({model_patch_b64!r}))
Path('/tmp/test.patch').write_bytes(base64.b64decode({test_patch_b64!r}))
PY
echo '::hay-validation::python'
which python
python - <<'PY'
import os
import sys
print(sys.executable)
print(os.environ.get('CONDA_DEFAULT_ENV'))
PY
echo '::hay-validation::apply-model'
git apply /tmp/model.patch
echo '::hay-validation::apply-tests'
git apply /tmp/test.patch
echo '::hay-validation::runner::{plan["runner"]}'
python - <<'PY'
import base64
skipped = base64.b64decode({skipped_b64!r}).decode('utf-8').splitlines()
for test_id in skipped:
    print(f"::hay-validation::skipped-unrunnable-test::{{test_id}}")
PY
if [ {len(plan["runnable_test_ids"])} -eq 0 ]; then
    echo '::hay-validation::no-runnable-tests' >&2
    exit 2
fi
{plan["command"]}
"""


def _validation_job_status(
    returncode: int, exception_info: str, raw_output: str
) -> str:
    if returncode == -1 and exception_info:
        return "error"
    lower_output = raw_output.lower()
    if returncode in {126, 127}:
        return "error"
    if "command not found" in lower_output:
        return "error"
    if "::hay-validation::no-runnable-tests" in raw_output:
        return "error"
    return "completed"


def _validation_config(args: argparse.Namespace) -> dict[str, Any]:
    specs = ["swebench.yaml"]
    if args.backend == "modal":
        specs.append("swebench_modal.yaml")
    configs = [get_config_from_spec(spec) for spec in specs]
    configs.append(
        {
            "environment": {
                "environment_class": (
                    "benchmarks.swebench.hay_environment.BenchmarkModalEnvironment"
                    if args.backend == "modal"
                    else "docker"
                ),
                "timeout": args.validation_timeout,
            }
        }
    )
    return recursive_merge(*configs)


def _validate_prediction(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    instance: dict[str, Any],
    prediction: dict[str, str],
) -> dict[str, Any]:
    instance_id = prediction["instance_id"]
    test_ids = _validation_test_ids(instance)
    repo = str(instance.get("repo", ""))
    plan = _validation_test_plan(repo, test_ids)
    started = time.time()
    env = None
    try:
        env = _get_sb_environment(config, instance)
        output = env.execute(
            {
                "command": _validation_command(
                    prediction.get("model_patch", ""),
                    str(instance.get("test_patch", "")),
                    test_ids,
                    repo=repo,
                )
            },
            cwd="/testbed",
            timeout=args.validation_timeout,
        )
        returncode = int(output.get("returncode", -1))
        exception_info = str(output.get("exception_info", ""))
        raw_output = str(output.get("output", ""))
        job_status = _validation_job_status(returncode, exception_info, raw_output)
        resolved = job_status == "completed" and returncode == 0
        return {
            "instance_id": instance_id,
            "job_status": job_status,
            "resolved": resolved,
            "returncode": returncode,
            "exception_info": exception_info,
            "elapsed_s": round(time.time() - started, 1),
            "model_name_or_path": prediction.get("model_name_or_path", ""),
            "model_patch_chars": len(prediction.get("model_patch", "")),
            "fail_to_pass": _json_list(instance.get("FAIL_TO_PASS", [])),
            "pass_to_pass": _json_list(instance.get("PASS_TO_PASS", [])),
            "validation_runner": plan["runner"],
            "runnable_test_ids": plan["runnable_test_ids"],
            "skipped_test_ids": plan["skipped_test_ids"],
            "output_tail": raw_output[-args.validation_output_chars :],
        }
    except Exception as exc:
        return {
            "instance_id": instance_id,
            "job_status": "error",
            "resolved": False,
            "returncode": -1,
            "exception_info": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.time() - started, 1),
            "model_name_or_path": prediction.get("model_name_or_path", ""),
            "model_patch_chars": len(prediction.get("model_patch", "")),
            "fail_to_pass": _json_list(instance.get("FAIL_TO_PASS", [])),
            "pass_to_pass": _json_list(instance.get("PASS_TO_PASS", [])),
            "validation_runner": plan["runner"],
            "runnable_test_ids": plan["runnable_test_ids"],
            "skipped_test_ids": plan["skipped_test_ids"],
            "output_tail": traceback.format_exc()[-args.validation_output_chars :],
        }
    finally:
        _stop_environment(env)


def _summarize_validation(results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in results if item.get("job_status") == "completed"]
    resolved = [item for item in completed if item.get("resolved")]
    errors = [item for item in results if item.get("job_status") == "error"]
    unresolved = [item for item in completed if not item.get("resolved")]
    return {
        "submitted_instances": len(results),
        "completed_instances": len(completed),
        "resolved_instances": len(resolved),
        "unresolved_instances": len(unresolved),
        "error_instances": len(errors),
        "submitted_ids": [str(item.get("instance_id")) for item in results],
        "completed_ids": [str(item.get("instance_id")) for item in completed],
        "resolved_ids": [str(item.get("instance_id")) for item in resolved],
        "unresolved_ids": [str(item.get("instance_id")) for item in unresolved],
        "error_ids": [str(item.get("instance_id")) for item in errors],
    }


def _run_local_validation(
    args: argparse.Namespace,
    *,
    preds: Path,
    report_path: Path,
) -> dict[str, Any]:
    predictions = _load_predictions(preds)
    instance_by_id = {instance["instance_id"]: instance for instance in _load_instances(args)}
    config = _validation_config(args)
    results: list[dict[str, Any] | None] = [None] * len(predictions)

    def validate_at(index: int, prediction: dict[str, str]) -> dict[str, Any]:
        instance_id = prediction["instance_id"]
        instance = instance_by_id.get(instance_id)
        if instance is None:
            return {
                "instance_id": instance_id,
                "job_status": "error",
                "resolved": False,
                "returncode": -1,
                "exception_info": "prediction instance_id is not in the selected dataset slice/filter",
                "elapsed_s": 0.0,
                "model_name_or_path": prediction.get("model_name_or_path", ""),
                "model_patch_chars": len(prediction.get("model_patch", "")),
                "fail_to_pass": [],
                "pass_to_pass": [],
                "output_tail": "",
            }
        return _validate_prediction(
            args=args, config=config, instance=instance, prediction=prediction
        )

    print(
        f"\n=== local validation: {preds} ({args.backend}, {len(predictions)} predictions) ===\n",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, args.validation_workers)
    ) as executor:
        future_to_index = {
            executor.submit(validate_at, index, prediction): index
            for index, prediction in enumerate(predictions)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            result = future.result()
            results[index] = result
            status = "resolved" if result.get("resolved") else result.get("job_status")
            print(f"validation {result['instance_id']}: {status}", flush=True)

    final_results = [item for item in results if item is not None]
    report = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": args.backend,
        "test_selection": "FAIL_TO_PASS+PASS_TO_PASS",
        "predictions_path": str(preds),
        "summary": _summarize_validation(final_results),
        "instances": final_results,
    }
    _write_json(report_path, report)
    summary = report["summary"]
    print(
        "local validation: "
        f"{summary['resolved_instances']}/{summary['submitted_instances']} resolved, "
        f"{summary['unresolved_instances']} unresolved, "
        f"{summary['error_instances']} errors",
        flush=True,
    )
    return report


def _submit_command(
    args: argparse.Namespace, preds: Path, report_dir: Path, run_id: str
) -> list[str]:
    subset = SB_SUBSETS.get(args.subset)
    if subset is None:
        raise SystemExit(f"error: sb-cli subset mapping missing for {args.subset!r}")
    return [
        "sb-cli",
        "submit",
        subset,
        args.split,
        "--predictions_path",
        str(preds),
        "--run_id",
        run_id,
        "--output_dir",
        str(report_dir),
        "--overwrite",
        "1" if args.overwrite_reports else "0",
    ]


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _git_cmd(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()


def _git_metadata() -> dict[str, Any]:
    status = _git_cmd("status", "--porcelain", "--untracked-files=all").splitlines()
    return {
        "commit": _git_cmd("rev-parse", "HEAD"),
        "short_commit": _git_cmd("rev-parse", "--short", "HEAD"),
        "branch": _git_cmd("branch", "--show-current"),
        "dirty": bool(status),
        "status": status[:200],
    }


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mode_result_summary(mode_dir: Path) -> dict[str, Any]:
    preds = _read_json_dict(mode_dir / "preds.json")
    instances: list[dict[str, Any]] = []
    total_cost = 0.0
    total_calls = 0
    for instance_id, prediction in sorted(preds.items()):
        pred = prediction if isinstance(prediction, dict) else {}
        traj = _read_json_dict(mode_dir / instance_id / f"{instance_id}.traj.json")
        info = traj.get("info") if isinstance(traj.get("info"), dict) else {}
        stats = (
            info.get("model_stats")
            if isinstance(info.get("model_stats"), dict)
            else {}
        )
        cost = float(stats.get("instance_cost") or 0.0)
        calls = int(stats.get("api_calls") or 0)
        total_cost += cost
        total_calls += calls
        instances.append(
            {
                "instance_id": instance_id,
                "cost": cost,
                "api_calls": calls,
                "exit_status": info.get("exit_status"),
                "patch_chars": len(str(pred.get("model_patch", ""))),
            }
        )

    validation = _read_json_dict(mode_dir / "local-validation.json").get("summary", {})
    rows = []
    telemetry_path = mode_dir / "hay_telemetry.jsonl"
    try:
        rows = [
            json.loads(line)
            for line in telemetry_path.read_text().splitlines()
            if line.strip()
        ]
    except (OSError, ValueError):
        rows = []
    telemetry = {
        "rows": len(rows),
        "accepted": sum(1 for row in rows if row.get("accepted")),
        "passthrough": sum(
            1
            for row in rows
            if str(row.get("backend", "")).startswith("passthrough")
            or str(row.get("reason", "")).startswith("passthrough")
        ),
        "low_memory_passthrough": sum(
            1
            for row in rows
            if row.get("reason") == "low-memory"
            or row.get("passthrough_reason") == "low-memory"
            or row.get("backend") == "passthrough:low-memory"
        ),
        "chunked": sum(1 for row in rows if row.get("chunked")),
        "saved_tokens": sum(int(row.get("saved_tokens") or 0) for row in rows),
        "accepted_saved_tokens": sum(
            int(row.get("saved_tokens") or 0) for row in rows if row.get("accepted")
        ),
        "model_input_tokens": sum(
            int(row.get("model_input_tokens") or 0) for row in rows
        ),
        "max_chunks": max(
            [int(row.get("chunks") or row.get("chunk_count") or 0) for row in rows]
            or [0]
        ),
    }
    return {
        "instances": instances,
        "totals": {
            "predictions": len(preds),
            "cost": round(total_cost, 6),
            "api_calls": total_calls,
        },
        "local_validation": validation if isinstance(validation, dict) else {},
        "telemetry": telemetry,
    }


def _write_result_summary(
    *,
    args: argparse.Namespace,
    root: Path,
    manifest: dict[str, Any],
    mode: str,
    mode_meta: dict[str, Any],
    env: dict[str, str],
) -> str:
    policy = mode_meta.get("policy") if isinstance(mode_meta.get("policy"), dict) else {}
    policy_id = str(policy.get("id") or _policy_id_for_mode(args, env, mode))
    slice_id = args.slice_id.strip() or "ad-hoc"
    result_dir = Path(args.results_output).resolve() / slice_id
    result_id = "__".join(
        [
            _slug(policy_id),
            _slug(args.backend),
            _slug(args.model.split("/", 1)[-1]),
            _slug(manifest["run_id"]),
        ]
    )
    result = {
        "version": 1,
        "result_id": result_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": manifest["run_id"],
        "run_dir": str(root),
        "slice_id": args.slice_id.strip(),
        "slice": manifest.get("slice_definition") or {
            "subset": args.subset,
            "split": args.split,
            "slice": args.slice,
            "filter": args.filter,
        },
        "mode": mode,
        "policy": policy,
        "backend": args.backend,
        "model": _model_name(args.model),
        "git": manifest.get("git", {}),
        **_mode_result_summary(root / mode),
    }
    path = result_dir / f"{result_id}.json"
    _write_json(path, result)
    return str(path)


def _initial_manifest(args: argparse.Namespace, run_id: str, root: Path) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fresh = {
        "run_id": run_id,
        "started_at": now,
        "slice_id": args.slice_id.strip(),
        "policy_id": args.policy_id.strip(),
        "subset": args.subset,
        "split": args.split,
        "slice": args.slice,
        "filter": args.filter,
        "shuffle": args.shuffle,
        "model": _model_name(args.model),
        "backend": args.backend,
        "modes": {},
    }
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return fresh
    try:
        existing = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return fresh
    if not isinstance(existing, dict):
        return fresh
    existing.update(
        {
            "run_id": run_id,
            "slice_id": args.slice_id.strip(),
            "policy_id": args.policy_id.strip(),
            "subset": args.subset,
            "split": args.split,
            "slice": args.slice,
            "filter": args.filter,
            "shuffle": args.shuffle,
            "model": _model_name(args.model),
            "backend": args.backend,
            "resumed_at": now,
        }
    )
    existing.setdefault("started_at", fresh["started_at"])
    existing.setdefault("modes", {})
    existing.pop("finished_at", None)
    return existing


def _check_vibeproxy(args: argparse.Namespace) -> None:
    if args.skip_vibeproxy_check:
        return
    req = urllib.request.Request(
        args.vibeproxy_url.rstrip("/") + "/models",
        headers={"Authorization": "Bearer dummy"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SystemExit(
            f"error: VibeProxy check failed at {args.vibeproxy_url}: {exc}"
        ) from exc
    models = {
        str(item.get("id")) for item in data.get("data", []) if isinstance(item, dict)
    }
    bare = args.model.split("/", 1)[-1]
    if bare not in models:
        raise SystemExit(
            f"error: VibeProxy is reachable but does not list model {bare!r}"
        )


def _wait_hay_manager(env: dict[str, str], timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    with _temporary_environ(
        {"HAY_HOME": env["HAY_HOME"], "HAY_MANAGER_SOCKET": ""}
    ):
        while time.monotonic() < deadline:
            try:
                if client.stats(timeout=0.5).get("ok"):
                    return True
            except OSError:
                pass
            time.sleep(0.25)
    return False


def run(args: argparse.Namespace) -> int:
    if args.backend == "modal":
        _check_modal_available()
    if args.submit:
        _require_tool("sb-cli")
        if not os.getenv("SWEBENCH_API_KEY"):
            raise SystemExit(
                "error: SWEBENCH_API_KEY is required when --submit is enabled"
            )
    _require_tool("uv")
    _check_vibeproxy(args)

    slice_definition = _load_slice(args)
    output_root = Path(args.output).resolve()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if "hay" in modes and args.policy_id == "baseline":
        raise SystemExit("error: policy-id 'baseline' cannot be used for hay mode")
    run_id = args.run_id.strip() or _default_run_id(args, modes, output_root)
    root = output_root / run_id
    manifest = _initial_manifest(args, run_id, root)
    manifest["git"] = _git_metadata()
    if slice_definition:
        manifest["slice_definition"] = slice_definition
    _write_json(root / "manifest.json", manifest)

    base_env = os.environ.copy()
    path_parts = [str(path) for path in _TOOL_DIRS]
    if base_env.get("PATH"):
        path_parts.append(base_env["PATH"])
    base_env["PATH"] = os.pathsep.join(path_parts)
    base_env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{base_env.get('PYTHONPATH', '')}"
    base_env.setdefault("OPENAI_API_KEY", "dummy")
    base_env.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
    # Benchmark runs should be isolated from a user's production/global manager:
    # no stale socket, no old memory floor, no leftover resident model.
    base_env.pop("HAY_MANAGER_SOCKET", None)
    base_env.setdefault("HAY_HOME", _runtime_hay_home(run_id))
    base_env.setdefault("HAY_MANAGER_DETACH", "0")
    # Benchmarks should measure the pruner, not the host's conservative cold-load
    # safety floor. Production adapters still use the manager default unless the
    # caller explicitly sets HAY_MIN_FREE_MB.
    base_env.setdefault("HAY_MIN_FREE_MB", "0")
    base_env.setdefault("HAY_BENCH_ABORT_ON_LOW_MEMORY", "1")
    if args.policy_id:
        _apply_known_policy(base_env, args.policy_id)
    manifest["hay_home"] = base_env["HAY_HOME"]
    _write_json(root / "manifest.json", manifest)

    for mode in modes:
        if mode not in {"baseline", "hay"}:
            raise SystemExit(f"error: unknown mode {mode!r}; expected baseline or hay")
        mode_dir = root / mode
        mode_dir.mkdir(parents=True, exist_ok=True)
        env = base_env.copy()
        env["HAY_BENCH_TELEMETRY"] = str(mode_dir / "hay_telemetry.jsonl")
        env["HAY_BENCH_QUERY"] = args.hay_query
        session = None
        if mode == "hay":
            session = _start_hay_session(env, f"{run_id}-{mode}")
            time.sleep(args.manager_warmup_s)
            if not _wait_hay_manager(env):
                _stop_process(session)
                raise SystemExit("error: Hay manager did not come up for hay mode")

        print(f"\n=== {mode}: Mini-SWE {args.backend} backend ===\n", flush=True)
        started = time.time()
        try:
            with _temporary_environ(
                {
                    "HAY_BENCH_TELEMETRY": env["HAY_BENCH_TELEMETRY"],
                    "HAY_BENCH_QUERY": env["HAY_BENCH_QUERY"],
                    "HAY_HOME": env["HAY_HOME"],
                    "HAY_MANAGER_SOCKET": "",
                }
            ):
                mode_meta = _run_miniswe(args, mode_dir, mode)
        except BenchmarkAbortError as exc:
            mode_meta = {
                "mode": mode,
                "policy": {
                    "id": _policy_id_for_mode(args, env, mode),
                    "known": _policy_id_for_mode(args, env, mode) in KNOWN_POLICIES,
                    "env": _policy_env(env) if mode == "hay" else {},
                },
                "exit_code": 75,
                "elapsed_s": round(time.time() - started, 1),
                "predictions_path": str(mode_dir / "preds.json"),
                "telemetry_path": str(mode_dir / "hay_telemetry.jsonl"),
                "error": str(exc),
            }
            manifest["modes"][mode] = mode_meta
            _write_json(root / "manifest.json", manifest)
            return 75
        finally:
            _stop_process(session)
        elapsed = round(time.time() - started, 1)
        code = int(mode_meta.pop("exit_code", 0))
        preds = mode_dir / "preds.json"
        mode_meta.update(
            {
                "mode": mode,
                "policy": {
                    "id": _policy_id_for_mode(args, env, mode),
                    "known": _policy_id_for_mode(args, env, mode) in KNOWN_POLICIES,
                    "env": _policy_env(env) if mode == "hay" else {},
                },
                "exit_code": code,
                "elapsed_s": elapsed,
                "predictions_path": str(preds),
                "telemetry_path": str(mode_dir / "hay_telemetry.jsonl"),
            }
        )
        if code != 0:
            mode_meta["error"] = f"Mini-SWE exited {code}"
            manifest["modes"][mode] = mode_meta
            _write_json(root / "manifest.json", manifest)
            return code
        if not preds.exists():
            mode_meta["error"] = "preds.json was not produced"
            manifest["modes"][mode] = mode_meta
            _write_json(root / "manifest.json", manifest)
            return 1
        if args.validate_local:
            validation_report_path = mode_dir / "local-validation.json"
            validation_report = _run_local_validation(
                args, preds=preds, report_path=validation_report_path
            )
            mode_meta["local_validation"] = {
                "enabled": True,
                "report_path": str(validation_report_path),
                **validation_report["summary"],
            }
            manifest["modes"][mode] = mode_meta
            _write_json(root / "manifest.json", manifest)
        else:
            mode_meta["local_validation"] = {"enabled": False}
        if args.submit:
            report_dir = mode_dir / "sb-cli-reports"
            submit_run_id = f"{run_id}-{mode}"
            sb_cmd = _submit_command(args, preds, report_dir, submit_run_id)
            print(f"\n=== sb-cli submit: {submit_run_id} ===\n", flush=True)
            sb_code = _run_logged(
                sb_cmd, env=env, log_path=mode_dir / "sb-cli.stdout.log"
            )
            mode_meta.update(
                {
                    "sbcli_command": sb_cmd,
                    "sbcli_exit_code": sb_code,
                    "sbcli_run_id": submit_run_id,
                    "sbcli_report_dir": str(report_dir),
                }
            )
            if sb_code != 0:
                mode_meta["error"] = f"sb-cli exited {sb_code}"
                manifest["modes"][mode] = mode_meta
                _write_json(root / "manifest.json", manifest)
                return sb_code
        should_export = (
            args.export_result is True
            or (args.export_result is None and bool(args.slice_id.strip()))
        )
        if should_export:
            mode_meta["result_summary_path"] = _write_result_summary(
                args=args,
                root=root,
                manifest=manifest,
                mode=mode,
                mode_meta=mode_meta,
                env=env,
            )
        manifest["modes"][mode] = mode_meta
        _write_json(root / "manifest.json", manifest)

    manifest["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_json(root / "manifest.json", manifest)
    print(f"\nResults written to {root}", flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--subset", default="lite", choices=sorted(SB_SUBSETS))
    p.add_argument("--split", default="test")
    p.add_argument(
        "--slice",
        default="0:1",
        help="dataset slice; empty means all selected instances",
    )
    p.add_argument("--filter", default="", help="regex instance filter")
    p.add_argument(
        "--slice-id",
        default="",
        help="load a frozen slice from benchmarks/slices/<slice-id>.json",
    )
    p.add_argument("--shuffle", action="store_true")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--model", default="gpt-5.5")
    p.add_argument(
        "--backend",
        default="modal",
        choices=["modal", "docker"],
        help="where Mini-SWE runs task sandboxes",
    )
    p.add_argument("--vibeproxy-url", default="http://localhost:8317/v1")
    p.add_argument("--skip-vibeproxy-check", action="store_true")
    p.add_argument(
        "--modes", default="baseline,hay", help="comma-separated: baseline,hay"
    )
    p.add_argument("--output", default=str(DEFAULT_RUN_ROOT))
    p.add_argument("--results-output", default=str(DEFAULT_RESULTS_ROOT))
    p.add_argument(
        "--policy-id",
        default="",
        help=(
            "named policy for hay mode, e.g. hay-2048-chunked; "
            "known policies set their env knobs"
        ),
    )
    p.add_argument("--run-id", default="")
    p.add_argument(
        "--hay-query",
        default="",
        help="force one relevance query for Hay; by default Hay uses the current assistant narration",
    )
    p.add_argument("--manager-warmup-s", type=float, default=2.0)
    p.add_argument("--redo-existing", action="store_true")
    p.add_argument(
        "--validate-local", dest="validate_local", action="store_true", default=True
    )
    p.add_argument("--no-validate-local", dest="validate_local", action="store_false")
    p.add_argument("--validation-workers", type=int, default=1)
    p.add_argument("--validation-timeout", type=int, default=1200)
    p.add_argument("--validation-output-chars", type=int, default=12000)
    p.add_argument("--submit", dest="submit", action="store_true", default=False)
    p.add_argument("--no-submit", dest="submit", action="store_false")
    p.add_argument(
        "--export-result",
        dest="export_result",
        action="store_true",
        default=None,
        help="write a compact tracked summary under benchmarks/results",
    )
    p.add_argument(
        "--no-export-result",
        dest="export_result",
        action="store_false",
        help="skip compact result export even when --slice-id is set",
    )
    p.add_argument("--overwrite-reports", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
