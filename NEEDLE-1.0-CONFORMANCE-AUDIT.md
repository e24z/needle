# Needle 1.0 Conformance Audit

Status: Draft audit
Date: 2026-06-24
Branch: `pi-native-pruning`
Audited against: `NEEDLE-1.0-PRD.md`, `NEEDLE-1.0-ISSUE-MAP.md`, current repo tree

## Purpose

This audit exists because the repo now sits between two shapes:

- the old Hay/pruner implementation, where `pruner` owns most runtime and
  backend behavior;
- the Needle 1.0 product shape, where a user installs a Package, the Package
  implements Capabilities and uses a Backend, and the CLI/status surfaces tell
  the truth about that graph.

Do not treat PRD section 5.2 as scripture. Treat it as one candidate object
model that must answer a larger launch question:

Can a blind Pi tester install Needle, use normal Pi tools, see what happened,
trust the package claims, and uninstall it cleanly?

If an implementation satisfies 5.2 but fails that user story, it is not 1.0. If
the user story exposes a better ontology than 5.2, the PRD should change.

## Governing Sources

Use these sources in this order:

1. Product promise: `NEEDLE-1.0-PRD.md` sections 1-4.
2. User scenarios: `NEEDLE-1.0-PRD.md` section 6 and `TESTER-HANDOFF.md`.
3. Component boundaries: `NEEDLE-1.0-PRD.md` sections 5.1, 5.2, and 8.1.
4. Backend/transport/accounting/status constraints: PRD sections 10-12.
5. Public launch and acceptance gates: PRD sections 14-15.
6. Current implementation evidence from code and tests.

The issue map is useful history, but it is no longer a source of truth by
itself. Several early issues have already landed, and several deeper refactors
were not captured in the first map.

## Executive Summary

Needle has crossed from "idea" into a real Pi slice:

- Static registry objects exist for protocol, capabilities, backend, binding,
  packages, package cards, and claim cards.
- `needle package ...`, `needle status`, `needle stop`, `needle uninstall`, and
  `needle model ...` exist.
- Pi read and bash wrappers exist and accept `context_focus_question`.
- Status now reports exact characters trimmed instead of fake tokens.
- The default package can mean no AST repair, while Soft-LaMR can opt into AST
  repair.

But the repo has not yet fully become the Needle 1.0 architecture:

- Pi consumes backend launcher metadata and starts the resident runtime through
  `python -m needle.runtime manage`.
- The resident runtime implementation now lives under `needle.runtime`, while
  `pruner.*` remains as compatibility wrappers.
- Backend dependency ownership exists for the MLX backend, but only as the first
  backend manifest; HTTP/CUDA backends are still conceptual.
- Registry validation now checks the main package/capability/backend/binding,
  claim-card fields, and local fixture-pack evidence refs.
- Claude is archived under `archive/claude/` and is no longer in the active
  adapter or test tree.
- The demo/evidence path has local fixture packs for the Pi packages, but not
  yet a polished tester-facing demo script.
- The PRD and issue map contain stale "current branch" statements.

The next work should be a structural conformance pass, not more benchmark work
or more ontology prose.

## Current State By Area

| Area | Current Evidence | Status | Gap |
| --- | --- | --- | --- |
| Pi install story | `package.json`, `adapters/pi/extension.js`, `TESTER-HANDOFF.md` | Partial | Local/git install is documented, but package distribution, fresh blind install, and exact uninstall rehearsal still need proof. |
| Pi read pruning | `adapters/pi/extension.js`, `tests/test_pi_client.mjs` | Mostly landed | Policy knobs still live in adapter env vars instead of resolved package/capability policy. |
| Pi bash pruning | `adapters/pi/extension.js`, `tests/test_pi_client.mjs` | Landed | PRD/issue map still contain stale text claiming bash is not implemented. |
| Explicit focus | Pi wrapper adds `context_focus_question` | Landed for Pi | Need verify package/binding owns this contract, not adapter-only code. |
| Status ontology | Pi footer/status functions and tests | Partial | Runtime stats do not expose enough active/load/backend/package detail; adapter still infers some state. |
| Exact chars | Pi counters and tester handoff use chars | Mostly landed | Token estimation/cost methods are not designed or exposed in detailed status yet. |
| Package registry | `protocols/`, `capabilities/`, `backends/`, `bindings/`, `packages/`, `claims/`, `package-cards/` | Landed as static graph | The graph is metadata more than execution spine. |
| Registry validation | `needle/registry.py`, `tests/test_package_config.py` | First slice landed | Main package graph fields are checked; full evidence-pack resolution and package-local step references remain. |
| Backend dependency ownership | `backends/e24z/code-pruner-mlx.yaml` declares `backend-code-pruner-mlx` | First slice landed | Need carry the same pattern to HTTP/CUDA backends and docs. |
| Backend launch | Pi can resolve a runtime launch plan from active package/backend metadata and launch `needle.runtime` | Landed | Backend implementation still lives under `pruner/backends` until backend re-home. |
| Runtime naming/state | `needle.runtime.*` owns runtime implementation and state defaults to `~/.needle` | Mostly landed | `pruner` and `HAY_*` remain compatibility shims; backend code still needs a Needle namespace. |
| Backend selection | `NEEDLE_BACKEND=e24z/code-pruner-mlx` is recognized; `HAY_BACKEND=code-pruner` remains an alias | Transitional | Legacy names should become compatibility shims, not primary docs. |
| Reference vs Soft-LaMR | Capability files and repair config tests exist | Mostly landed | Need ensure active package controls repair in every runtime path and CLI doctor reports it plainly. |
| HTTP/CUDA backend | PRD describes target | Missing | Need at least a documented backend contract, and maybe a minimal HTTP backend stub if "point Needle at HTTP" remains 1.0. |
| Evidence/claims | Claim cards, `evidence/fixture-packs/*`, and `needle evidence check` exist | Mostly landed | Fixture packs are validated and listable; a live Pi demo script can still wrap an actual session later. |
| Claude | `archive/claude/` | Archived | Not a 1.0 host. Revive only through a new host binding, package card, claim card, and active tests. |
| CLI shape | `needle/cli.py` uses argparse | Working but straining | Typer likely fits the nested product surface better, but it is secondary to ownership. |
| Issue tracking | GitHub issues #1-#8 and `NEEDLE-1.0-WORK-QUEUE.md` | Mostly landed | GitHub now owns durable work items; local docs still need refresh to avoid stale parallel tracking. |
| Docs | Tester handoff is current-ish; reading map and issue map are stale | Mixed | Need docs refresh after structural refactor. |

## Main Diagnosis

The project has two different layers of truth:

1. Registry truth: package/capability/backend files say what Needle should be.
2. Runtime truth: adapter/session/manager code still starts and selects the old
   `pruner` runtime mostly by environment variables and hardcoded module names.

1.0 requires those layers to meet. A Package should not merely display
`uses: e24z/code-pruner-mlx`; it should cause the runtime launcher to pick the
right backend dependency set, runtime module, interface, privacy mode, and
claim surface.

## Proposed Next Issue Set

### A. Backend-manifest driven runtime launch

Priority: P0
Status: first slice landed; keep this issue open only for follow-up polish.

Problem:
The Pi adapter previously launched `uv run --extra mlx -m pruner manage`
directly. That bypassed the package/backend graph.

Target:

- Backend object declares its Python extra, runtime module, backend alias, model
  requirements, and supported interface.
- Package resolution returns enough launch metadata for host adapters.
- Pi starts the manager from resolved metadata, not hardcoded `mlx`/`pruner`.
- Legacy env vars remain as compatibility aliases only.

Likely files:

- `backends/e24z/code-pruner-mlx.yaml`
- `needle/registry.py`
- `adapters/pi/client.mjs`
- `pyproject.toml`
- tests for package resolution and Pi spawn command.

### B. Runtime re-home and compatibility shim

Priority: P0
Status: namespace shim and Needle-owned runtime launcher landed; physical
re-home remains.

Problem:
`pruner` still means runtime, CLI, backend, old product identity, and manager
protocol. This keeps the code mentally anchored in the old architecture.

Target:

- Move runtime modules toward `needle.runtime`. (Landed for resident runtime.)
- Move backend code toward `needle.backends.code_pruner_mlx`.
- Keep `pruner` as a thin compatibility import/entrypoint while the branch is
  in transition.
- Update docs/tests to describe Needle runtime, not "the pruner app".

This can be staged. Do not mix it with large behavioral changes.

### C. Backend dependency contract

Priority: P0
Status: first slice landed for `e24z/code-pruner-mlx`.

Problem:
The MLX dependency must belong to the backend identity, not the CLI/app.

Target:

```toml
[project.optional-dependencies]
backend-code-pruner-mlx = [
  "mlx",
  "mlx-lm",
  "numpy",
  "huggingface-hub",
  "transformers",
]
```

Then `e24z/code-pruner-mlx` declares that extra. The CLI stays lightweight.

### D. Registry validation deepening

Priority: P0
Status: first slice landed; keep open for evidence-pack resolution.

Problem:
The loader validates graph references but not enough of the product contract.

Target:

- Validate package `accounting`, `privacy`, `runtime`, `compute`, `evidence`,
  `claim_card`, `package_card`, and host binding shape.
- Validate backend `supports`, runtime/dependency metadata, and interface.
- Validate capability extension/conformance chains.
- Make errors good enough for `/needle doctor`.

### E. Archive non-1.0 Claude surface

Priority: P0 or P1 depending on release hygiene.

Status:
Landed. Claude support is archived because 1.0 targets Pi.

What moved:

- Claude adapter/plugin/skills/docs moved into `archive/claude/`.
- Claude-only tests moved out of the active `tests/test_*.py` suite.
- Archive README preserves statusline and manager lifecycle lessons.

### F. Typer CLI pass

Priority: P1 unless CLI work blocks A-D.

Problem:
The CLI is now a real product surface with nested commands and doctor output.
`argparse` works, but the help UX will get worse as commands grow.

Target:

- Convert `needle` CLI to Typer.
- Keep command names stable.
- Add grouped help for `package`, `backend`, `runtime`, `model`, `doctor`,
  `uninstall`.
- Keep base CLI dependency light; do not pull MLX into base install.

### G. Evidence pack and demo fixture

Priority: P0 for public tester launch.

Problem:
Claim cards exist, but evidence refs are symbolic.

Target:

- Add a tiny fixture repo/output pair for read and bash.
- Record before/after, expected pass-through, expected prune, exact chars.
- Add a check script that proves package claims point at existing evidence.
- Make the demo independent of Docker, benchmarks, paid APIs, and live MLX when
  possible.

### H. HTTP backend contract

Priority: Decision-dependent.

Problem:
The PRD says users can run local MLX or point Needle at HTTP. If that remains a
1.0 promise, a contract needs to exist.

Target:

- Document request/response shape.
- Add backend metadata for `e24z/code-pruner-http` or equivalent.
- Decide whether implementation is a stub, fake test backend, or real client.

### I. GitHub issue migration plan

Priority: P1, but should happen before parallel external work.

Status:
Landed. GitHub issues #3-#8 now split the remaining Needle 1.0 work under the
broad Pi-native parent #1. Issue #2 remains the separate MCP lane.

Follow-up:

- Keep this local map as design history, not as the canonical tracker.
- Link PRs to issues once implementation starts moving in parallel.

Do not create GitHub issues merely to create activity. First convert this audit
into a small, sequenced issue set that is worth tracking.

## Decisions To Surface

These are product/architecture calls, not chores.

1. Is HTTP backend support a 1.0 requirement or a documented post-1.0 path?
   - Recommendation: make the HTTP contract a 1.0 doc/metadata requirement, but
     make a polished provider recipe post-1.0.

2. Do we rename runtime state from `~/.hay` to `~/.needle` before public testers?
   - Answer: yes. Runtime state now defaults to `~/.needle`; `HAY_*` remains a
     compatibility alias.

3. Was Claude archived for 1.0?
   - Answer: yes. The active repo now shows the Pi 1.0 product, while
     `archive/claude/` preserves the older adapter work.

4. Is `e24z/pi-local-mac` still the default public package?
   - Recommendation: yes, with `swe-pruner/reference` and no AST repair. Keep
     Soft-LaMR as explicit alternate until evidence says otherwise.

5. Should package policy own min chars / min savings threshold?
   - Recommendation: yes. Adapter env vars can override locally, but defaults
     should be visible in capability/package metadata or package-local policy.

6. Should Typer happen before or after the runtime re-home?
   - Recommendation: after backend-manifest launch, before public tester docs.
     Typer improves UX but will not fix ownership by itself.

7. How strict should claim/evidence validation be for first testers?
   - Recommendation: strict on existence and identity, modest on metrics. Exact
     chars are enough for the demo; token/dollar claims need explicit caveats.

8. Is MCP in scope for 1.0?
   - Recommendation: no, unless Pi stalls. Keep MCP as a parallel architecture
     note, not the release blocker.

9. When do GitHub issues become canonical?
   - Recommendation: after the conformance audit is accepted and before the
     structural refactor splits into parallel branches. The local issue map can
     stage wording, but GitHub should own durable issue numbers, labels, and PR
     links.

## Suggested Work Order

1. Add backend dependency/launch metadata and make Pi consume it.
2. Deepen registry validation so bad package graphs fail early.
3. Re-home runtime/backend modules under Needle names, keeping `pruner` shims.
4. Convert CLI to Typer and add `backend`/`runtime` doctor views.
5. Add a live Pi demo script around an actual session, if needed.
6. Refresh PRD/current-state sections, issue map, tester handoff, and reading
   map.

Do not start with benchmarks. Do not start with more ontology prose. The next
useful evidence is whether a package graph can drive the actual runtime.
