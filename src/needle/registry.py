"""Static Needle package registry loading and validation.

This module is intentionally runtime-free: it must be safe in Linux cloud tests,
without MLX, Docker, model files, or host-agent processes. Registry files are
JSON-compatible YAML so they remain hand-readable while the loader can use only
the Python standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from .runtime import naming


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
BUILTIN_REGISTRY_ROOT = PACKAGE_ROOT / "registry_data"
DEFAULT_PACKAGE_ID = naming.DEFAULT_PACKAGE_ID
REGISTRY_ROOT_ENVS = ("NEEDLE_REGISTRY_ROOT", "HAY_REGISTRY_ROOT")
PACKAGE_ID_ENVS = ("NEEDLE_PACKAGE", "HAY_PACKAGE")
CONFIG_PATH_ENVS = ("NEEDLE_CONFIG", "HAY_CONFIG")

_KIND_DIRS = {
    "protocol": "protocols",
    "capability": "capabilities",
    "backend": "backends",
    "binding": "bindings",
    "package": "packages",
    "claim": "claims",
}

_SCHEMAS = {
    "protocol": "needle.protocol.v1",
    "capability": "needle.capability.v1",
    "backend": "needle.backend.v1",
    "binding": "needle.host_binding.v1",
    "package": "needle.package.v1",
    "claim": "needle.claim_card.v1",
}

_KNOWN_ARTIFACT_KINDS = {"file_text", "process_output"}
_KNOWN_EVIDENCE_PREFIXES = ("fixture_pack:",)
_KNOWN_RUNTIMES = {"local_manager"}
_BOOLEAN_RUNTIME_ENV_KEYS = {
    "NEEDLE_REPAIR",
    "NEEDLE_MLX_LIGHT",
    "NEEDLE_PROFILE_MLX",
    "NEEDLE_MLX_CLEAR_CACHE_AFTER_PRUNE",
}
_BOOLEAN_RUNTIME_ENV_VALUES = {"0", "1", "false", "true", "no", "yes", "off", "on"}
_POSITIVE_INT_RUNTIME_ENV_KEYS = {
    "NEEDLE_MLX_MAX_LENGTH",
    "NEEDLE_MAX_LENGTH",
    "NEEDLE_MLX_MAX_BATCH_SIZE",
    "NEEDLE_MLX_MAX_BATCH_TOKENS",
    "NEEDLE_MLX_CACHE_LIMIT_MB",
    "NEEDLE_MLX_WIRED_LIMIT_MB",
    "NEEDLE_MLX_ADAPTIVE_SINGLE_CHUNK_UNTIL_TOKENS",
    "NEEDLE_MLX_ADAPTIVE_SMALL_MAX_LENGTH",
    "NEEDLE_MLX_ADAPTIVE_LARGE_MAX_LENGTH",
}
_NON_NEGATIVE_INT_RUNTIME_ENV_KEYS = {
    "NEEDLE_CHUNK_OVERLAP_TOKENS",
}
_FLOAT_0_TO_1_RUNTIME_ENV_KEYS = {
    "NEEDLE_THRESHOLD",
}
_MIN_FLOAT_RUNTIME_ENV_KEYS = {
    "NEEDLE_MLX_MAX_LENGTH_RATIO": 1.0,
}
_ENUM_RUNTIME_ENV_VALUES = {
    "NEEDLE_MLX_PROFILE": {"local_adaptive", "local-mlx-adaptive", "local_mlx_adaptive"},
}
_KNOWN_RUNTIME_PROFILE_ENV_KEYS = (
    _BOOLEAN_RUNTIME_ENV_KEYS
    | _POSITIVE_INT_RUNTIME_ENV_KEYS
    | _NON_NEGATIVE_INT_RUNTIME_ENV_KEYS
    | _FLOAT_0_TO_1_RUNTIME_ENV_KEYS
    | set(_MIN_FLOAT_RUNTIME_ENV_KEYS)
    | set(_ENUM_RUNTIME_ENV_VALUES)
)


class PackageConfigError(ValueError):
    """Raised when registry objects are missing or internally inconsistent."""


@dataclass(frozen=True)
class LoadedPackage:
    package: dict[str, Any]
    protocol: dict[str, Any]
    capabilities: dict[str, dict[str, Any]]
    backend: dict[str, Any]
    binding: dict[str, Any]
    claim_card: dict[str, Any]
    package_card_path: Path
    evidence_paths: dict[str, Path]

    @property
    def package_id(self) -> str:
        return str(self.package["id"])

    @property
    def capability_ids(self) -> list[str]:
        return list(self.capabilities)

    @property
    def backend_id(self) -> str:
        return str(self.backend["id"])

    @property
    def binding_id(self) -> str:
        return str(self.binding["id"])

    @property
    def evidence_refs(self) -> list[str]:
        return list(self.evidence_paths)


@dataclass(frozen=True)
class RuntimeLaunchPlan:
    package_id: str
    backend_id: str
    kind: str
    command: list[str]
    env: dict[str, str]
    runtime_profile: str = ""
    extra: str = ""
    module: str = ""
    args: list[str] = field(default_factory=list)


def load_active_package(
    root: Path | None = None,
    package_id: str | None = None,
    *,
    host_binding: str | None = None,
) -> LoadedPackage:
    """Load and validate the active package graph."""
    registry_root = root or default_registry_root()
    active_package_id = canonical_package_id(
        package_id or default_package_id(registry_root, host_binding=host_binding)
    )
    package = _load_object(registry_root, "package", active_package_id)
    if host_binding and package.get("host_binding") != host_binding:
        raise PackageConfigError(
            f"package {active_package_id!r} is bound to {package.get('host_binding')!r}, "
            f"not requested host binding {host_binding!r}"
        )
    loaded = _validate_package_graph(registry_root, package)
    if host_binding and loaded.binding_id != host_binding:
        raise PackageConfigError(
            f"package {loaded.package_id!r} is bound to {loaded.binding_id!r}, "
            f"not requested host binding {host_binding!r}"
        )
    return loaded


def runtime_launch_plan(
    root: Path | None = None,
    package_id: str | None = None,
    *,
    host_binding: str | None = None,
) -> RuntimeLaunchPlan:
    """Resolve the active package into a concrete local runtime launch plan."""
    loaded = load_active_package(root, package_id, host_binding=host_binding)
    launcher = _validate_backend_launcher(loaded.backend)
    runtime_profile = _validate_runtime_profile(loaded.package)
    return RuntimeLaunchPlan(
        package_id=loaded.package_id,
        backend_id=loaded.backend_id,
        kind=launcher["kind"],
        command=launcher["command"],
        env={**launcher["env"], **runtime_profile["env"]},
        runtime_profile=runtime_profile["id"],
        extra=launcher["extra"],
        module=launcher["module"],
        args=launcher["args"],
    )


def load_backend(root: Path | None = None, backend_id: str = "") -> dict[str, Any]:
    """Load and validate a backend manifest without requiring a package."""
    if not backend_id:
        raise PackageConfigError("backend_id must be a non-empty string")
    registry_root = root or default_registry_root()
    backend = _load_object(registry_root, "backend", backend_id)
    _validate_backend(backend)
    return backend


def package_summaries(
    root: Path | None = None,
    *,
    host_binding: str | None = None,
) -> list[dict[str, Any]]:
    """List package registry entries with enough metadata for CLIs/adapters."""
    registry_root = root or default_registry_root()
    active_id = default_package_id(registry_root, host_binding=host_binding)
    summaries: list[dict[str, Any]] = []
    for package_id in list_package_ids(registry_root):
        try:
            loaded = load_active_package(registry_root, package_id)
        except PackageConfigError as exc:
            if host_binding:
                continue
            summaries.append(
                {
                    "id": package_id,
                    "active": package_id == active_id,
                    "valid": False,
                    "error": str(exc),
                }
            )
            continue
        if host_binding and loaded.binding_id != host_binding:
            continue
        summaries.append(
            {
                "id": loaded.package_id,
                "display_name": loaded.package.get("display_name"),
                "active": loaded.package_id == active_id,
                "valid": True,
                "capabilities": loaded.capability_ids,
                "protocol": loaded.protocol["id"],
                "backend": loaded.backend_id,
                "host_binding": loaded.binding_id,
                "runtime_profile": _validate_runtime_profile(loaded.package)["id"],
                "claim_card": loaded.claim_card["id"],
                "package_card": str(loaded.package_card_path),
            }
        )
    return sorted(summaries, key=lambda item: str(item["id"]))


def default_registry_root() -> Path:
    """Registry root for built-ins or an installed package registry checkout."""
    env = _first_env(REGISTRY_ROOT_ENVS)
    if env:
        return Path(env).expanduser()
    if BUILTIN_REGISTRY_ROOT.exists():
        return BUILTIN_REGISTRY_ROOT
    return REPO_ROOT


def default_package_id(
    root: Path | None = None,
    *,
    host_binding: str | None = None,
) -> str:
    package_id, _source = active_package_selection(root, host_binding=host_binding)
    return package_id


def active_package_selection(
    root: Path | None = None,
    *,
    host_binding: str | None = None,
) -> tuple[str, str]:
    for name in PACKAGE_ID_ENVS:
        value = os.environ.get(name)
        if value:
            return canonical_package_id(value), f"env:{name}"
    configured = configured_package_id(host_binding=host_binding)
    if configured:
        return canonical_package_id(configured), f"config:{package_config_path()}"
    if host_binding:
        return _default_package_for_host_binding(root or default_registry_root(), host_binding), "default"
    return DEFAULT_PACKAGE_ID, "default"


def package_config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    env = _first_env(CONFIG_PATH_ENVS)
    return Path(env).expanduser() if env else naming.app_home() / "config.json"


def canonical_package_id(package_id: str) -> str:
    return naming.canonical_package_id(package_id)


def configured_package_id(
    path: Path | None = None,
    *,
    host_binding: str | None = None,
) -> str | None:
    config = _read_user_config(package_config_path(path))
    if host_binding:
        packages = config.get("packages")
        if isinstance(packages, dict):
            value = packages.get(host_binding)
            if isinstance(value, str) and value:
                return canonical_package_id(value)
    value = config.get("package")
    if isinstance(value, str) and value:
        return canonical_package_id(value)
    packages = config.get("packages")
    if isinstance(packages, dict):
        values = sorted({value for value in packages.values() if isinstance(value, str) and value})
        if len(values) == 1:
            return canonical_package_id(values[0])
    return None


def set_configured_package_id(
    package_id: str,
    *,
    host_binding: str | None = None,
    root: Path | None = None,
    path: Path | None = None,
) -> LoadedPackage:
    loaded = load_active_package(root, package_id, host_binding=host_binding)
    config_path = package_config_path(path)
    config = _read_user_config(config_path)
    binding = host_binding or loaded.binding_id
    packages = config.get("packages")
    if not isinstance(packages, dict):
        packages = {}
    packages[binding] = loaded.package_id
    config["packages"] = packages
    if not host_binding:
        config["package"] = loaded.package_id
    _write_user_config(config_path, config)
    return loaded


def list_package_ids(root: Path | None = None) -> list[str]:
    registry_root = root or default_registry_root()
    packages_root = registry_root / _KIND_DIRS["package"]
    ids: list[str] = []
    if not packages_root.exists():
        return ids
    for path in packages_root.rglob("*.yaml"):
        ids.append(path.relative_to(packages_root).with_suffix("").as_posix())
    return sorted(ids)


def _default_package_for_host_binding(root: Path, host_binding: str) -> str:
    try:
        loaded = load_active_package(root, DEFAULT_PACKAGE_ID)
        if loaded.binding_id == host_binding:
            return loaded.package_id
    except PackageConfigError:
        pass
    for package_id in list_package_ids(root):
        try:
            loaded = load_active_package(root, package_id)
        except PackageConfigError:
            continue
        if loaded.binding_id == host_binding:
            return loaded.package_id
    raise PackageConfigError(f"no package found for host binding {host_binding!r}")


def object_path(root: Path, kind: str, object_id: str) -> Path:
    if kind not in _KIND_DIRS:
        raise PackageConfigError(f"unknown registry kind: {kind}")
    _validate_id(object_id)
    kind_dir = _KIND_DIRS[kind]
    if object_id.startswith(f"{kind_dir}/"):
        return root / f"{object_id}.yaml"
    return root / kind_dir / f"{object_id}.yaml"


def package_card_path(root: Path, card_id: str) -> Path:
    _validate_prefixed_path_id(card_id, "package-cards")
    return root / f"{card_id}.md"


def _load_object(root: Path, kind: str, object_id: str) -> dict[str, Any]:
    path = object_path(root, kind, object_id)
    try:
        with path.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError as exc:
        raise PackageConfigError(f"missing {kind} object {object_id!r} at {path}") from exc
    except json.JSONDecodeError as exc:
        raise PackageConfigError(f"invalid JSON-compatible YAML in {path}: {exc.msg}") from exc

    if not isinstance(obj, dict):
        raise PackageConfigError(f"{kind} object {object_id!r} must be a mapping")
    expected_schema = _SCHEMAS[kind]
    if obj.get("schema") != expected_schema:
        raise PackageConfigError(
            f"{kind} object {object_id!r} has schema {obj.get('schema')!r}, expected {expected_schema!r}"
        )
    if obj.get("id") != object_id:
        raise PackageConfigError(f"{kind} object at {path} has id {obj.get('id')!r}, expected {object_id!r}")
    return obj


def _validate_package_graph(root: Path, package: dict[str, Any]) -> LoadedPackage:
    _validate_package_manifest(package)

    implemented_ids = _string_list(package, "implements")
    if not implemented_ids:
        raise PackageConfigError(f"package {package['id']!r} must implement at least one capability")

    capabilities = {cap_id: _load_object(root, "capability", cap_id) for cap_id in implemented_ids}
    for capability in capabilities.values():
        _validate_capability(capability)
    protocols = {_capability_protocol(root, cap) for cap in capabilities.values()}
    if len(protocols) != 1:
        raise PackageConfigError(f"package {package['id']!r} mixes protocols: {sorted(protocols)}")
    protocol = _load_object(root, "protocol", protocols.pop())
    _validate_protocol(protocol)

    backend_id = _nested_string(package, ("uses", "backend"))
    backend = _load_object(root, "backend", backend_id)
    _validate_backend(backend)
    supported = set(_string_list(backend, "supports"))
    missing_support = sorted(set(implemented_ids) - supported)
    if missing_support:
        raise PackageConfigError(
            f"backend {backend_id!r} does not support package capabilities: {', '.join(missing_support)}"
        )

    binding = _load_object(root, "binding", _required_string(package, "host_binding"))
    _validate_binding(binding)

    card_id = _required_string(package, "package_card")
    card_path = package_card_path(root, card_id)
    if not card_path.exists():
        raise PackageConfigError(f"missing package card {card_id!r} at {card_path}")

    evidence_paths = _validate_package_evidence(root, package, implemented_ids, binding)

    claim_card = _load_object(root, "claim", _claim_object_id(package))
    _validate_claim_card(claim_card)
    if claim_card.get("package") != package["id"]:
        raise PackageConfigError(
            f"claim card {claim_card['id']!r} points to package {claim_card.get('package')!r}, "
            f"expected {package['id']!r}"
        )
    claimed_cap = claim_card.get("capability")
    if claimed_cap not in implemented_ids:
        raise PackageConfigError(
            f"claim card {claim_card['id']!r} claims capability {claimed_cap!r}, "
            f"not one of package capabilities {implemented_ids!r}"
        )

    return LoadedPackage(
        package=package,
        protocol=protocol,
        capabilities=capabilities,
        backend=backend,
        binding=binding,
        claim_card=claim_card,
        package_card_path=card_path,
        evidence_paths=evidence_paths,
    )


def _validate_protocol(protocol: dict[str, Any]) -> None:
    protocol_id = _required_string(protocol, "id")
    input_obj = _required_mapping(protocol, "input")
    if input_obj.get("text") != "string":
        raise PackageConfigError(f"protocol {protocol_id!r} input.text must be 'string'")
    output_obj = _required_mapping(protocol, "output")
    if output_obj.get("text") != "string":
        raise PackageConfigError(f"protocol {protocol_id!r} output.text must be 'string'")
    failure = _required_mapping(protocol, "failure")
    if not isinstance(failure.get("default"), str) or not failure["default"]:
        raise PackageConfigError(f"protocol {protocol_id!r} failure.default must be a string")
    accounting = _required_mapping(protocol, "accounting")
    _string_list(accounting, "minimum")


def _validate_capability(capability: dict[str, Any]) -> None:
    cap_id = _required_string(capability, "id")
    has_parent = "extends" in capability
    has_protocol = "conforms_to" in capability
    if has_parent == has_protocol:
        raise PackageConfigError(
            f"capability {cap_id!r} must declare exactly one of 'extends' or 'conforms_to'"
        )
    _required_string(capability, "extends" if has_parent else "conforms_to")
    _required_string(capability, "version")
    _required_string(capability, "description")

    focus = capability.get("focus")
    if focus is not None:
        if not isinstance(focus, dict):
            raise PackageConfigError(f"capability {cap_id!r} focus must be a mapping")
        _required_string(focus, "field")
        _required_string(focus, "missing")

    gates = capability.get("gates")
    if gates is not None:
        if not isinstance(gates, dict):
            raise PackageConfigError(f"capability {cap_id!r} gates must be a mapping")
        min_chars = gates.get("min_chars")
        if min_chars is not None and not isinstance(min_chars, int):
            raise PackageConfigError(f"capability {cap_id!r} gates.min_chars must be an integer")

    rendering = capability.get("rendering")
    if rendering is not None:
        if not isinstance(rendering, dict):
            raise PackageConfigError(f"capability {cap_id!r} rendering must be a mapping")
        for key in ("omitted_spans", "marker_format"):
            _required_string(rendering, key)

    claim_scope = capability.get("claim_scope")
    if claim_scope is not None and not isinstance(claim_scope, dict):
        raise PackageConfigError(f"capability {cap_id!r} claim_scope must be a mapping")

    impl = _required_mapping(capability, "implementation")
    recipe = _required_mapping(impl, "behavior_recipe")
    _validate_recipe_steps(cap_id, recipe)


def _validate_recipe_steps(owner_id: str, recipe: dict[str, Any]) -> None:
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PackageConfigError(f"{owner_id!r} behavior_recipe.steps must be a non-empty list")
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise PackageConfigError(f"{owner_id!r} behavior_recipe.steps entries must be mappings")
        step_id = _required_string(step, "id")
        if step_id in seen:
            raise PackageConfigError(f"{owner_id!r} behavior_recipe step {step_id!r} is duplicated")
        seen.add(step_id)
        _required_string(step, "kind")
        params = step.get("params")
        if params is not None and not isinstance(params, dict):
            raise PackageConfigError(f"{owner_id!r} behavior_recipe step {step_id!r} params must be a mapping")


def _validate_backend(backend: dict[str, Any]) -> None:
    backend_id = _required_string(backend, "id")
    _string_list(backend, "supports")
    compute = _required_mapping(backend, "compute")
    _required_string(compute, "default")
    _string_list(compute, "requires")
    interface = _required_mapping(backend, "interface")
    accepts = _string_list(interface, "accepts")
    returns = _string_list(interface, "returns")
    if "text" not in accepts or "text" not in returns:
        raise PackageConfigError(f"backend {backend_id!r} interface must accept and return text")
    runtime = _required_string(backend, "runtime")
    if runtime not in _KNOWN_RUNTIMES:
        raise PackageConfigError(f"backend {backend_id!r} runtime {runtime!r} is unknown")
    transport = backend.get("transport")
    if transport is not None:
        if not isinstance(transport, dict):
            raise PackageConfigError(f"backend {backend_id!r} transport must be a mapping")
        _validate_backend_transport(backend_id, transport)
    _validate_backend_launcher(backend)


def _validate_backend_transport(backend_id: str, transport: dict[str, Any]) -> None:
    kind = _required_string(transport, "kind")
    if kind != "http_json":
        raise PackageConfigError(f"backend {backend_id!r} transport.kind {kind!r} is unknown")
    endpoint_env = _required_string(transport, "endpoint_env")
    if not endpoint_env.startswith("NEEDLE_"):
        raise PackageConfigError(f"backend {backend_id!r} transport.endpoint_env must start with 'NEEDLE_'")
    if transport.get("endpoint_required") is not True:
        raise PackageConfigError(f"backend {backend_id!r} transport.endpoint_required must be true")
    if _required_string(transport, "failure_behavior") != "passthrough_original":
        raise PackageConfigError(
            f"backend {backend_id!r} transport.failure_behavior must be 'passthrough_original'"
        )
    _required_string(transport, "privacy_warning")

    request = _required_mapping(transport, "request")
    if _required_string(request, "method") != "POST":
        raise PackageConfigError(f"backend {backend_id!r} transport.request.method must be 'POST'")
    path = _required_string(request, "path")
    if not path.startswith("/"):
        raise PackageConfigError(f"backend {backend_id!r} transport.request.path must start with '/'")
    if _required_string(request, "content_type") != "application/json":
        raise PackageConfigError(
            f"backend {backend_id!r} transport.request.content_type must be 'application/json'"
        )
    request_body = _required_mapping(request, "body")
    if request_body.get("text") != "string":
        raise PackageConfigError(f"backend {backend_id!r} transport.request.body.text must be 'string'")
    if request_body.get("context_focus_question") != "optional string":
        raise PackageConfigError(
            f"backend {backend_id!r} transport.request.body.context_focus_question "
            "must be 'optional string'"
        )

    response = _required_mapping(transport, "response")
    statuses = response.get("status_success")
    if not isinstance(statuses, list) or 200 not in statuses:
        raise PackageConfigError(f"backend {backend_id!r} transport.response.status_success must include 200")
    if _required_string(response, "content_type") != "application/json":
        raise PackageConfigError(
            f"backend {backend_id!r} transport.response.content_type must be 'application/json'"
        )
    response_body = _required_mapping(response, "body")
    if response_body.get("text") != "string":
        raise PackageConfigError(f"backend {backend_id!r} transport.response.body.text must be 'string'")


def _validate_binding(binding: dict[str, Any]) -> None:
    binding_id = _required_string(binding, "id")
    _required_string(binding, "host")
    tools = _required_mapping(binding, "tools")
    if not tools:
        raise PackageConfigError(f"binding {binding_id!r} tools must not be empty")
    for tool_name, tool in tools.items():
        if not isinstance(tool_name, str) or not tool_name:
            raise PackageConfigError(f"binding {binding_id!r} tool names must be non-empty strings")
        if not isinstance(tool, dict):
            raise PackageConfigError(f"binding {binding_id!r} tool {tool_name!r} must be a mapping")
        artifact_kind = _required_string(tool, "artifact_kind")
        if artifact_kind not in _KNOWN_ARTIFACT_KINDS:
            raise PackageConfigError(
                f"binding {binding_id!r} tool {tool_name!r} artifact_kind {artifact_kind!r} is unknown"
            )
        for key in ("focus_param", "text_extract", "text_patch"):
            _required_string(tool, key)

    fallbacks = _required_mapping(binding, "fallbacks")
    for key in ("missing_focus", "unsupported_result_shape"):
        _required_string(fallbacks, key)


def _validate_package_manifest(package: dict[str, Any]) -> None:
    package_id = _required_string(package, "id")
    _required_string(package, "display_name")
    _nested_string(package, ("uses", "backend"))
    focus = _required_mapping(package, "focus_contract")
    _required_string(focus, "prompt_bundle")
    _required_string(focus, "missing_focus_behavior")
    compute = _required_mapping(package, "compute")
    _required_string(compute, "default")
    if "alternatives" in compute:
        _string_list(compute, "alternatives")
    runtime = _required_string(package, "runtime")
    if runtime not in _KNOWN_RUNTIMES:
        raise PackageConfigError(f"package {package_id!r} runtime {runtime!r} is unknown")
    privacy = _required_mapping(package, "privacy")
    _required_string(privacy, "default")
    if not isinstance(privacy.get("remote_requires_explicit_endpoint"), bool):
        raise PackageConfigError(
            f"package {package_id!r} privacy.remote_requires_explicit_endpoint must be a boolean"
        )
    accounting = _required_mapping(package, "accounting")
    _required_string(accounting, "status")
    if "async" in accounting:
        _string_list(accounting, "async")
    _validate_runtime_profile(package)
    for ref in _string_list(package, "evidence"):
        _validate_evidence_ref(package_id, ref)
    _required_string(package, "package_card")
    _required_string(package, "claim_card")


def _validate_runtime_profile(package: dict[str, Any]) -> dict[str, Any]:
    package_id = str(package.get("id", "<package>"))
    profile = package.get("runtime_profile")
    if profile is None:
        return {"id": "", "env": {}}
    if not isinstance(profile, dict):
        raise PackageConfigError(f"package {package_id!r} runtime_profile must be a mapping")
    profile_id = _required_string(profile, "id")
    env = profile.get("env", {})
    if not isinstance(env, dict):
        raise PackageConfigError(
            f"package {package_id!r} runtime_profile.env must map NEEDLE_* keys to strings"
        )
    clean_env: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key.startswith("NEEDLE_") or not isinstance(value, str):
            raise PackageConfigError(
                f"package {package_id!r} runtime_profile.env must map NEEDLE_* keys to strings"
            )
        _validate_runtime_profile_env_value(package_id, key, value)
        clean_env[key] = value
    return {"id": profile_id, "env": clean_env}


def _validate_runtime_profile_env_value(package_id: str, key: str, value: str) -> None:
    if key not in _KNOWN_RUNTIME_PROFILE_ENV_KEYS:
        raise PackageConfigError(f"package {package_id!r} runtime_profile.env key {key!r} is unknown")
    if key in _BOOLEAN_RUNTIME_ENV_KEYS:
        if value.strip().lower() not in _BOOLEAN_RUNTIME_ENV_VALUES:
            raise PackageConfigError(
                f"package {package_id!r} runtime_profile.env {key!r} must be a boolean "
                "(0/1, true/false, yes/no, or on/off)"
            )
        return
    if key in _POSITIVE_INT_RUNTIME_ENV_KEYS:
        _validate_int_env(package_id, key, value, minimum=1)
        return
    if key in _NON_NEGATIVE_INT_RUNTIME_ENV_KEYS:
        _validate_int_env(package_id, key, value, minimum=0)
        return
    if key in _FLOAT_0_TO_1_RUNTIME_ENV_KEYS:
        numeric = _parse_float_env(package_id, key, value)
        if numeric < 0.0 or numeric > 1.0:
            raise PackageConfigError(
                f"package {package_id!r} runtime_profile.env {key!r} must be between 0 and 1"
            )
        return
    if key in _MIN_FLOAT_RUNTIME_ENV_KEYS:
        numeric = _parse_float_env(package_id, key, value)
        minimum = _MIN_FLOAT_RUNTIME_ENV_KEYS[key]
        if numeric < minimum:
            raise PackageConfigError(
                f"package {package_id!r} runtime_profile.env {key!r} must be at least {minimum:g}"
            )
        return
    allowed = _ENUM_RUNTIME_ENV_VALUES.get(key)
    if allowed is not None and value not in allowed:
        raise PackageConfigError(
            f"package {package_id!r} runtime_profile.env {key!r} must be one of {sorted(allowed)!r}"
        )


def _validate_int_env(package_id: str, key: str, value: str, *, minimum: int) -> None:
    try:
        numeric = int(value, 10)
    except ValueError as exc:
        raise PackageConfigError(
            f"package {package_id!r} runtime_profile.env {key!r} must be an integer"
        ) from exc
    if str(numeric) != value.strip():
        raise PackageConfigError(
            f"package {package_id!r} runtime_profile.env {key!r} must be an integer"
        )
    if numeric < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise PackageConfigError(
            f"package {package_id!r} runtime_profile.env {key!r} must be a {qualifier} integer"
        )


def _parse_float_env(package_id: str, key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise PackageConfigError(
            f"package {package_id!r} runtime_profile.env {key!r} must be a number"
        ) from exc


def _validate_evidence_ref(package_id: str, ref: str) -> None:
    if any(ref.startswith(prefix) and ref != prefix for prefix in _KNOWN_EVIDENCE_PREFIXES):
        return
    if ref.startswith("evidence/"):
        _validate_id(ref)
        return
    raise PackageConfigError(
        f"package {package_id!r} evidence reference {ref!r} must start with "
        f"{', '.join(_KNOWN_EVIDENCE_PREFIXES)} or 'evidence/'"
    )


def evidence_ref_path(root: Path, ref: str) -> Path:
    """Resolve a package evidence reference to the checked local artifact."""
    if ref.startswith("fixture_pack:"):
        pack_id = ref.split(":", 1)[1]
        _validate_id(pack_id)
        return root / "evidence" / "fixture-packs" / pack_id / "manifest.json"
    if ref.startswith("evidence/"):
        _validate_id(ref)
        return root / ref
    raise PackageConfigError(f"unknown evidence reference {ref!r}")


def _validate_package_evidence(
    root: Path,
    package: dict[str, Any],
    implemented_ids: list[str],
    binding: dict[str, Any],
) -> dict[str, Path]:
    package_id = str(package["id"])
    paths: dict[str, Path] = {}
    seen: set[str] = set()
    for ref in _string_list(package, "evidence"):
        if ref in seen:
            raise PackageConfigError(f"package {package_id!r} evidence reference {ref!r} is duplicated")
        seen.add(ref)
        path = evidence_ref_path(root, ref)
        if not path.exists():
            raise PackageConfigError(f"missing evidence reference {ref!r} at {path}")
        if ref.startswith("fixture_pack:"):
            _validate_fixture_pack(path, package_id, implemented_ids, binding)
        paths[ref] = path
    return paths


def _validate_fixture_pack(
    path: Path,
    package_id: str,
    implemented_ids: list[str],
    binding: dict[str, Any],
) -> None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            pack = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PackageConfigError(f"invalid fixture pack manifest at {path}: {exc.msg}") from exc
    if not isinstance(pack, dict):
        raise PackageConfigError(f"fixture pack manifest at {path} must be a mapping")
    if pack.get("schema") != "needle.fixture_pack.v1":
        raise PackageConfigError(f"fixture pack {path} must use schema 'needle.fixture_pack.v1'")
    pack_id = _required_string(pack, "id")
    if path.parent.name != pack_id:
        raise PackageConfigError(f"fixture pack {pack_id!r} path must end with its id")
    if pack.get("package") != package_id:
        raise PackageConfigError(
            f"fixture pack {pack_id!r} points to package {pack.get('package')!r}, "
            f"expected {package_id!r}"
        )
    capability = _required_string(pack, "capability")
    if capability not in implemented_ids:
        raise PackageConfigError(
            f"fixture pack {pack_id!r} capability {capability!r} is not implemented by package {package_id!r}"
        )
    binding_id = _required_string(binding, "id")
    if pack.get("host_binding") != binding_id:
        raise PackageConfigError(f"fixture pack {pack_id!r} host_binding must be {binding_id!r}")
    binding_tools = _required_mapping(binding, "tools")

    cases = pack.get("cases")
    if not isinstance(cases, list) or not cases:
        raise PackageConfigError(f"fixture pack {pack_id!r} cases must be a non-empty list")
    coverage: set[tuple[str, str]] = set()
    seen: set[str] = set()
    for case_ref in cases:
        if not isinstance(case_ref, dict):
            raise PackageConfigError(f"fixture pack {pack_id!r} case refs must be mappings")
        case_id = _required_string(case_ref, "id")
        if case_id in seen:
            raise PackageConfigError(f"fixture pack {pack_id!r} case {case_id!r} is duplicated")
        seen.add(case_id)
        case_file = _required_string(case_ref, "file")
        _validate_id(case_file)
        case_path = path.parent / case_file
        if not case_path.exists():
            raise PackageConfigError(f"fixture pack {pack_id!r} missing case {case_id!r} at {case_path}")
        tool = _required_string(case_ref, "tool")
        if tool not in binding_tools:
            raise PackageConfigError(
                f"fixture pack {pack_id!r} case {case_id!r} uses tool {tool!r}, "
                f"not one of binding tools {sorted(binding_tools)!r}"
            )
        behavior = _required_string(case_ref, "expected_behavior")
        coverage.add((tool, behavior))
        _validate_fixture_case(case_path, case_id, tool, behavior)

    first_tool = next(iter(binding_tools))
    required = {(tool, "visible_prune") for tool in binding_tools}
    required.add((first_tool, "passthrough_original"))
    missing = sorted(f"{tool}:{behavior}" for tool, behavior in required - coverage)
    if missing:
        raise PackageConfigError(f"fixture pack {pack_id!r} missing required cases: {', '.join(missing)}")


def _validate_fixture_case(path: Path, case_id: str, tool: str, behavior: str) -> None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            case = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PackageConfigError(f"invalid fixture case at {path}: {exc.msg}") from exc
    if not isinstance(case, dict):
        raise PackageConfigError(f"fixture case at {path} must be a mapping")
    if case.get("schema") != "needle.fixture_case.v1":
        raise PackageConfigError(f"fixture case {case_id!r} must use schema 'needle.fixture_case.v1'")
    if case.get("id") != case_id:
        raise PackageConfigError(f"fixture case at {path} has id {case.get('id')!r}, expected {case_id!r}")
    if case.get("tool") != tool:
        raise PackageConfigError(f"fixture case {case_id!r} tool mismatch")
    if case.get("expected_behavior") != behavior:
        raise PackageConfigError(f"fixture case {case_id!r} expected_behavior mismatch")
    _required_string(case, "artifact_kind")
    input_obj = _required_mapping(case, "input")
    _required_string(input_obj, "text")
    assertions = _required_mapping(case, "assertions")
    if behavior == "visible_prune":
        _required_string(case, "context_focus_question")
        if not isinstance(assertions.get("chars_removed_gt"), int):
            raise PackageConfigError(f"fixture case {case_id!r} assertions.chars_removed_gt must be an integer")
    elif behavior == "passthrough_original":
        if case.get("context_focus_question") is not None:
            raise PackageConfigError(f"fixture case {case_id!r} passthrough case must omit focus")
        if assertions.get("returned_equals_original") is not True:
            raise PackageConfigError(
                f"fixture case {case_id!r} assertions.returned_equals_original must be true"
            )
    else:
        raise PackageConfigError(f"fixture case {case_id!r} has unknown behavior {behavior!r}")


def _validate_claim_card(claim: dict[str, Any]) -> None:
    claim_id = _required_string(claim, "id")
    _required_string(claim, "package")
    _required_string(claim, "capability")
    _required_string(claim, "claim")
    _required_string(claim, "evidence_level")
    tested = _required_mapping(claim, "tested")
    _required_string(tested, "host")
    _string_list(tested, "tools")
    _required_string(tested, "compute")
    _required_string(tested, "capability")
    metrics = _required_mapping(claim, "metrics")
    _string_list(metrics, "exact")
    if "estimates" in metrics:
        _string_list(metrics, "estimates")
    _string_list(claim, "known_limits")
    _string_list(claim, "must_not_claim")
    privacy_notes = _required_mapping(claim, "privacy_notes")
    _required_string(privacy_notes, "default")
    _required_string(privacy_notes, "remote_compute")
    if claim["capability"] != tested["capability"]:
        raise PackageConfigError(
            f"claim card {claim_id!r} capability {claim['capability']!r} "
            f"does not match tested.capability {tested['capability']!r}"
        )


def _validate_backend_launcher(backend: dict[str, Any]) -> dict[str, Any]:
    backend_id = str(backend.get("id", "<backend>"))
    launcher = backend.get("launcher")
    if not isinstance(launcher, dict):
        raise PackageConfigError(f"backend {backend_id!r} requires mapping field 'launcher'")

    kind = launcher.get("kind")
    if kind == "uv-python-module":
        extra = launcher.get("extra")
        if not isinstance(extra, str):
            raise PackageConfigError(f"backend {backend_id!r} launcher.extra must be a string")
        module = launcher.get("module")
        if not isinstance(module, str) or not module:
            raise PackageConfigError(f"backend {backend_id!r} launcher.module must be a non-empty string")
        args = launcher.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) and arg for arg in args):
            raise PackageConfigError(f"backend {backend_id!r} launcher.args must be a string list")
        command = ["uv", "run"]
        if extra:
            command.extend(["--extra", extra])
        command.extend(["-m", module])
        command.extend(args)
    elif kind == "needle-cli":
        command = launcher.get("command")
        if not isinstance(command, list) or not all(isinstance(arg, str) and arg for arg in command):
            raise PackageConfigError(f"backend {backend_id!r} launcher.command must be a string list")
        extra = ""
        module = ""
        args = list(command[1:])
    else:
        raise PackageConfigError(
            f"backend {backend_id!r} launcher.kind must be 'needle-cli' or 'uv-python-module'"
        )
    env = launcher.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and key and isinstance(value, str)
        for key, value in env.items()
    ):
        raise PackageConfigError(f"backend {backend_id!r} launcher.env must map strings to strings")

    return {
        "kind": kind,
        "command": list(command),
        "extra": extra,
        "module": module,
        "args": list(args),
        "env": dict(env),
    }


def _capability_protocol(root: Path, capability: dict[str, Any]) -> str:
    if "conforms_to" in capability:
        return _required_string(capability, "conforms_to")

    parent_id = _required_string(capability, "extends")
    parent = _load_object(root, "capability", parent_id)
    return _capability_protocol(root, parent)


def _claim_object_id(package: dict[str, Any]) -> str:
    claim_ref = _required_string(package, "claim_card")
    prefix = "claims/"
    if not claim_ref.startswith(prefix):
        raise PackageConfigError(f"claim_card must start with {prefix!r}: {claim_ref!r}")
    return claim_ref


def _required_string(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise PackageConfigError(f"{obj.get('id', '<object>')!r} requires string field {key!r}")
    return value


def _required_mapping(obj: dict[str, Any], key: str) -> dict[str, Any]:
    value = obj.get(key)
    if not isinstance(value, dict):
        raise PackageConfigError(f"{obj.get('id', '<object>')!r} requires mapping field {key!r}")
    return value


def _nested_string(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            raise PackageConfigError(f"{obj.get('id', '<object>')!r} requires field {'.'.join(keys)!r}")
        cur = cur[key]
    if not isinstance(cur, str) or not cur:
        raise PackageConfigError(f"{obj.get('id', '<object>')!r} requires string field {'.'.join(keys)!r}")
    return cur


def _string_list(obj: dict[str, Any], key: str) -> list[str]:
    value = obj.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PackageConfigError(f"{obj.get('id', '<object>')!r} requires non-empty string list field {key!r}")
    return value


def _validate_id(object_id: str) -> None:
    if not isinstance(object_id, str) or not object_id:
        raise PackageConfigError("registry id must be a non-empty string")
    if object_id.startswith("/") or object_id.endswith("/") or "//" in object_id:
        raise PackageConfigError(f"invalid registry id: {object_id!r}")
    if any(part in {"", ".", ".."} for part in object_id.split("/")):
        raise PackageConfigError(f"invalid registry id path: {object_id!r}")


def _validate_prefixed_path_id(object_id: str, prefix: str) -> None:
    _validate_id(object_id)
    if not object_id.startswith(f"{prefix}/"):
        raise PackageConfigError(f"expected {prefix} id, got {object_id!r}")


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _read_user_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise PackageConfigError(f"invalid user config at {path}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise PackageConfigError(f"user config at {path} must be a mapping")
    return data


def _write_user_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
