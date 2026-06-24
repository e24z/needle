"""Static Needle package registry loading and validation.

This module is intentionally runtime-free: it must be safe in Linux cloud tests,
without MLX, Docker, model files, or host-agent processes. Registry files are
JSON-compatible YAML so they remain hand-readable while the loader can use only
the Python standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from . import naming


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKAGE_ID = "e24z/pi-local-mac"
REGISTRY_ROOT_ENVS = ("HAY_REGISTRY_ROOT", "NEEDLE_REGISTRY_ROOT")
PACKAGE_ID_ENVS = ("HAY_PACKAGE", "NEEDLE_PACKAGE")
CONFIG_PATH_ENVS = ("HAY_CONFIG", "NEEDLE_CONFIG")

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


def load_active_package(root: Path | None = None, package_id: str | None = None) -> LoadedPackage:
    """Load and validate the active package graph."""
    registry_root = root or default_registry_root()
    active_package_id = package_id or default_package_id()
    package = _load_object(registry_root, "package", active_package_id)
    return _validate_package_graph(registry_root, package)


def package_summaries(
    root: Path | None = None,
    *,
    host_binding: str | None = None,
) -> list[dict[str, Any]]:
    """List package registry entries with enough metadata for CLIs/adapters."""
    registry_root = root or default_registry_root()
    active_id = default_package_id()
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
                "backend": loaded.backend_id,
                "host_binding": loaded.binding_id,
                "claim_card": loaded.claim_card["id"],
                "package_card": str(loaded.package_card_path),
            }
        )
    return sorted(summaries, key=lambda item: str(item["id"]))


def default_registry_root() -> Path:
    """Registry root for built-ins or an installed package registry checkout."""
    env = _first_env(REGISTRY_ROOT_ENVS)
    return Path(env).expanduser() if env else REPO_ROOT


def default_package_id() -> str:
    package_id, _source = active_package_selection()
    return package_id


def active_package_selection() -> tuple[str, str]:
    for name in PACKAGE_ID_ENVS:
        value = os.environ.get(name)
        if value:
            return value, f"env:{name}"
    configured = configured_package_id()
    if configured:
        return configured, f"config:{package_config_path()}"
    return DEFAULT_PACKAGE_ID, "default"


def package_config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    env = _first_env(CONFIG_PATH_ENVS)
    return Path(env).expanduser() if env else naming.app_home() / "config.json"


def configured_package_id(path: Path | None = None) -> str | None:
    config = _read_user_config(package_config_path(path))
    value = config.get("package")
    return value if isinstance(value, str) and value else None


def set_configured_package_id(
    package_id: str,
    *,
    root: Path | None = None,
    path: Path | None = None,
) -> LoadedPackage:
    loaded = load_active_package(root, package_id)
    config_path = package_config_path(path)
    config = _read_user_config(config_path)
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
    implemented_ids = _string_list(package, "implements")
    if not implemented_ids:
        raise PackageConfigError(f"package {package['id']!r} must implement at least one capability")

    capabilities = {cap_id: _load_object(root, "capability", cap_id) for cap_id in implemented_ids}
    protocols = {_capability_protocol(root, cap) for cap in capabilities.values()}
    if len(protocols) != 1:
        raise PackageConfigError(f"package {package['id']!r} mixes protocols: {sorted(protocols)}")
    protocol = _load_object(root, "protocol", protocols.pop())

    backend_id = _nested_string(package, ("uses", "backend"))
    backend = _load_object(root, "backend", backend_id)
    supported = set(_string_list(backend, "supports"))
    missing_support = sorted(set(implemented_ids) - supported)
    if missing_support:
        raise PackageConfigError(
            f"backend {backend_id!r} does not support package capabilities: {', '.join(missing_support)}"
        )

    binding = _load_object(root, "binding", _required_string(package, "host_binding"))

    card_id = _required_string(package, "package_card")
    card_path = package_card_path(root, card_id)
    if not card_path.exists():
        raise PackageConfigError(f"missing package card {card_id!r} at {card_path}")

    claim_card = _load_object(root, "claim", _claim_object_id(package))
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
    )


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
