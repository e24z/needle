# Needle Registry Field Audit

This file explains what the built-in registry fields do today. A field may
belong to more than one category, but every listed field below is explicit about
whether it drives runtime behavior, is only validated, is shown to operators, is
evidence/claim metadata, or is future/declarative.

Categories used here:

- `runtime-driving`: changes the launched runtime, manager environment, backend,
  or text-transform behavior today.
- `selection/validation-enforced`: loaded and checked by the registry before a
  package is accepted.
- `host-adapter-enforced`: used by a host adapter or MCP server rather than the
  resident manager itself.
- `operator/display`: shown in CLI/status/package surfaces for humans.
- `claim/evidence`: supports package claims, fixtures, or proof level.
- `future/declarative`: documents an intended or external contract but does not
  make the current installable runtime perform that behavior.

## Package Fields

| Field | Categories | Current behavior |
| --- | --- | --- |
| `id` | `selection/validation-enforced`, `operator/display` | Must match the package path and is the stable package selection key. It is shown in package, setup, and status surfaces. |
| `display_name` | `selection/validation-enforced`, `operator/display` | Required for package summaries. It does not change runtime behavior. |
| `implements` | `selection/validation-enforced`, `claim/evidence`, `operator/display` | Loads capabilities, validates one protocol lineage, validates backend support, and scopes claim/evidence checks. |
| `uses.backend` | `runtime-driving`, `selection/validation-enforced`, `operator/display` | Selects the backend manifest used by `runtime_launch_plan()` and manager launch env. A missing or unsupported backend fails package loading clearly. |
| `host_binding` | `selection/validation-enforced`, `host-adapter-enforced`, `operator/display` | Constrains host-scoped package selection. Host adapters use it to choose compatible packages; wrong scoped loads fail. |
| `focus_contract.prompt_bundle` | `host-adapter-enforced`, `operator/display`, `future/declarative` | Names the prompt/tool contract a host adapter should follow. The resident manager does not inspect this string. |
| `focus_contract.missing_focus_behavior` | `host-adapter-enforced`, `selection/validation-enforced` | Documents and validates the host behavior for missing goal hints. Pi and MCP paths pass through without pruning when no focus question is supplied. |
| `compute.default` | `operator/display`, `selection/validation-enforced` | Required metadata describing the default compute mode. Runtime launch is driven by `uses.backend`, not this field. |
| `compute.alternatives` | `future/declarative`, `operator/display` | Optional metadata only. Active built-in packages do not advertise `http_pruner` as a usable alternative because no HTTP runtime resolver exists in this pass. |
| `runtime` | `selection/validation-enforced`, `operator/display` | Must be a known runtime family, currently `local_manager`. It does not by itself launch anything. |
| `runtime_profile.id` | `operator/display`, `selection/validation-enforced` | Names the package runtime profile and is reported by launch plans/status. It is metadata unless paired with `runtime_profile.env`. |
| `runtime_profile.env` | `runtime-driving`, `selection/validation-enforced`, `operator/display` | Validated `NEEDLE_*` environment values that are applied to the resident manager process. Invalid or non-finite numeric values fail loading. |
| `privacy.default` | `operator/display`, `claim/evidence` | Required public privacy claim. It does not alter runtime behavior. |
| `privacy.remote_requires_explicit_endpoint` | `claim/evidence`, `future/declarative` | Required boolean contract for remote compute. It does not enable remote compute today. |
| `accounting.status` | `operator/display`, `claim/evidence` | Required metadata for what the package can report synchronously. Today the runtime reports exact character counts. |
| `accounting.async` | `claim/evidence`, `future/declarative` | Lists possible later/background estimates. It does not run a token counter today. |
| `package_card` | `selection/validation-enforced`, `operator/display`, `claim/evidence` | Must resolve to a package card file. Used for documentation/proof surface, not runtime behavior. |
| `claim_card` | `selection/validation-enforced`, `claim/evidence` | Must resolve to a claim object matching the package and capability. It does not change pruning. |
| `evidence` | `selection/validation-enforced`, `claim/evidence` | Validates fixture/evidence references and fixture coverage. It does not load models or run benchmarks. |

## Backend Fields

| Field | Categories | Current behavior |
| --- | --- | --- |
| `id` | `selection/validation-enforced`, `operator/display` | Must match the backend path and is referenced by `uses.backend`. |
| `supports` | `selection/validation-enforced` | Must include every capability implemented by a package using the backend. |
| `compute.default` | `operator/display`, `selection/validation-enforced` | Required metadata describing backend compute mode. Runtime selection still comes from `uses.backend` and `launcher`. |
| `compute.requires` | `operator/display`, `claim/evidence` | Displayed by doctor as backend requirements. It does not install dependencies. |
| `interface` | `selection/validation-enforced`, `future/declarative` | Validates that the backend accepts and returns text. Extra values such as `scores` or `accounting` are descriptive today. |
| `runtime` | `selection/validation-enforced` | Must be a known runtime family. It does not launch the manager without a package launch path. |
| `launcher` | `runtime-driving`, `selection/validation-enforced` | Defines the command/env shape that `runtime_launch_plan()` uses. Changing command or env changes the launch plan or fails validation. |
| `transport` | `future/declarative`, `selection/validation-enforced` | Validates a future HTTP JSON contract when present. The current runtime does not implement this transport. |

## Binding Fields

| Field | Categories | Current behavior |
| --- | --- | --- |
| `id` | `selection/validation-enforced`, `operator/display` | Must match the binding path and package `host_binding`. |
| `host` | `operator/display`, `host-adapter-enforced` | Names the host family. The registry validates it as present but does not install the host. |
| `tools` | `selection/validation-enforced`, `host-adapter-enforced`, `claim/evidence` | Defines tool names and shapes expected by host adapters and fixture coverage. |
| `focus_param` | `host-adapter-enforced`, `selection/validation-enforced` | Names the parameter that should carry the context-focus question. The manager receives only the resolved query text. |
| `text_extract` | `host-adapter-enforced`, `selection/validation-enforced` | Names how the adapter extracts text from a host result. It is descriptive to the core runtime. |
| `text_patch` | `host-adapter-enforced`, `selection/validation-enforced` | Names how the adapter writes pruned text back into a host result. It is descriptive to the core runtime. |
| `fallbacks` | `host-adapter-enforced`, `selection/validation-enforced` | Documents adapter fallback behavior such as passthrough for missing focus or unsupported shapes. |

## Capability Fields

| Field | Categories | Current behavior |
| --- | --- | --- |
| `id` | `selection/validation-enforced`, `operator/display`, `claim/evidence` | Must match the capability path and is referenced by packages, backends, claims, and evidence. |
| `extends` | `selection/validation-enforced`, `claim/evidence` | Points to a parent capability. Used to resolve the protocol lineage. |
| `conforms_to` | `selection/validation-enforced` | Points to the protocol for root capabilities. Exactly one of `extends` or `conforms_to` must be present. |
| `focus` | `host-adapter-enforced`, `claim/evidence`, `future/declarative` | Describes the focus field and missing-focus behavior. Pi/MCP enforce this through their tool layer; the manager does not validate the capability object at prune time. |
| `gates` | `future/declarative`, `selection/validation-enforced` | Validated metadata for policy gates such as minimum characters. Current adapter thresholds are code/env driven, not dynamically loaded from this field. |
| `rendering` | `claim/evidence`, `future/declarative`, `selection/validation-enforced` | Documents the expected marker/rendering style. The current renderer lives in backend code rather than reading these strings dynamically. |
| `claim_scope` | `claim/evidence`, `operator/display` | Describes what a claim may or may not include, such as no AST repair for reference behavior. |
| `implementation.behavior_recipe` | `claim/evidence`, `future/declarative`, `selection/validation-enforced` | Validated recipe metadata for how the policy should be understood. It is not an executable pipeline interpreter today. |

## Current Runtime Constraints

- The resident manager records the package id, host binding, runtime profile id,
  and backend id it was launched with, then exposes those values in stats/status.
- A live manager does not yet force-restart when a later session wants a
  different package/profile but the code version is unchanged. This mismatch is
  deliberately visible in status rather than hidden behind risky restart logic.
- `HAY_*` environment variables remain accepted as legacy aliases for early
  installs. Public package manifests use `NEEDLE_*`.
- `pruner` imports and entrypoints are not part of the current installable
  product on this branch. The installable product is `needle`.
- `e24z/code-pruner-http` is retained as a future/declarative contract only. No
  built-in package advertises it as a usable runtime alternative in this pass.
