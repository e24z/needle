# Needle 1.0 Work Queue

Status: Active
Branch: `pi-native-pruning`
Updated: 2026-06-24

This is the live implementation queue. It exists so the branch can keep moving
without relying on chat memory.

## Working Rules

- Keep taking the next small structural slice.
- Commit every meaningful checkpoint with a Conventional Commit.
- Prefer implementation evidence over more ontology prose.
- Keep the active 1.0 path focused on Pi.
- Do not run benchmarks until package/runtime/demo trust gates are real.
- Use GitHub issues as the durable tracker once this local queue has been
  converted into issue-sized work.

## Done

- `docs(needle): add 1.0 conformance audit`
  - Added `NEEDLE-1.0-CONFORMANCE-AUDIT.md`.
  - Marked the old issue map as staging/history, not the canonical tracker.
- `feat(needle): launch runtime from backend metadata`
  - Added backend launcher metadata to `e24z/code-pruner-mlx`.
  - Renamed the MLX extra to `backend-code-pruner-mlx`.
  - Made Pi resolve runtime launch from active package/backend metadata.
  - Made `NEEDLE_BACKEND=e24z/code-pruner-mlx` a real backend selector.
- `feat(needle): deepen registry validation`
  - Validated package focus, compute, runtime, privacy, accounting, and
    evidence fields.
  - Validated host binding tool mappings by artifact kind.
  - Validated backend compute/interface/runtime/launcher shape.
  - Validated capability recipes and claim card trust fields.
- `refactor(needle): add runtime namespace`
  - Added `needle.runtime.*` wrappers around the existing manager/client/session
    runtime.
  - Moved Needle-owned CLI and registry imports onto `needle.runtime`.
  - Kept `pruner` as the compatibility entrypoint for now.
- `refactor(needle): launch manager through runtime namespace`
  - Added `python -m needle.runtime` as the active resident runtime launcher.
  - Moved the MLX backend manifest, Pi client expectations, and CLI doctor
    output off `python -m pruner manage`.
  - Kept the runtime implementation delegated to `pruner.cli` during migration.
- `chore(needle): archive claude adapter`
  - Moved Claude plugin metadata, adapter code, skills, monitors, and
    Claude-only tests under `archive/claude/`.
  - Removed Claude from active uninstall guidance.
  - Preserved the statusline/session lessons in an archive README.
- `feat(needle): add package evidence fixtures`
  - Added checked fixture packs for reference and Soft-LaMR Pi packages.
  - Validated fixture-pack manifests, case files, and read/bash/missing-focus
    coverage during package loading.
  - Surfaced evidence refs in `needle package doctor`.
- `feat(needle): add evidence check command`
  - Added `needle evidence check [package]`.
  - Listed fixture manifests and read/bash/pass-through cases for testers.
  - Documented the command in the Pi README and tester handoff.
- `docs(needle): migrate work queue to github issues`
  - Created GitHub issues #3-#8 for the remaining Needle 1.0 slices.
  - Kept issue #1 as the broad Pi-native parent and issue #2 as the MCP lane.
- `feat(needle): default runtime state to needle home`
  - Changed default runtime state from `~/.hay` to `~/.needle`.
  - Kept `HAY_*` env vars as compatibility aliases behind `NEEDLE_*`.
  - Added `NEEDLE_NO_EVENTS`/`NEEDLE_EVENTS*` aliases.
- `refactor(needle): physically re-home runtime modules`
  - Moved the resident runtime implementation into `needle/runtime`.
  - Left `pruner.*` as compatibility wrappers.
  - Updated active tests to import `needle.runtime` unless they are explicitly
    testing compatibility.
- `feat(needle): convert cli to typer`
  - Replaced the public `needle` argparse command tree with Typer.
  - Kept stable command names for package, evidence, status, stop, uninstall,
    and model commands.
  - Kept backend/MLX dependencies out of the base CLI dependency set.
  - Exercised the real `uv run needle ...` entrypoint in CLI tests.

## Now

### 1. Add Demo Fixture And Evidence Pack

Goal:
Prove the Pi package behavior with small, local, repeatable fixtures before
public tester docs or benchmark claims.

Acceptance:

- A checked fixture exercises one visible read prune. (Landed.)
- A checked fixture exercises one bash/process-output prune. (Landed.)
- A checked fixture exercises missing `context_focus_question` pass-through.
  (Landed.)
- Claim-card evidence refs resolve to local files instead of placeholders.
  (Landed.)
- `needle package doctor` or a dedicated validator reports evidence status.
  (Landed.)
- `needle evidence check` prints the fixture cases in a tester-facing form.
  (Landed.)

### 2. GitHub Issues Are The Durable Tracker

Goal:
Use GitHub issues for implementation tracking from this point forward.

Issue Set:

- #1 Build Pi-native Hay pruning contract from SWE-Pruner parity principles
- #2 Build bash-minimal Needle MCP package
- #3 Finish Needle runtime re-home under Needle namespaces (runtime landed on branch)
- #4 Migrate runtime state from `~/.hay` to `~/.needle` (landed on branch)
- #5 Convert needle CLI to Typer with stable command names (landed on branch)
- #6 Define HTTP backend contract and registry metadata
- #7 Add live Pi demo canary around evidence fixtures
- #8 Refresh Needle 1.0 PRD and tester docs after structural slices

Acceptance:

- GitHub issues exist for demo/evidence, Typer CLI, runtime physical re-home,
  state rename/migration, and HTTP/backend contract. (Landed.)
- The local queue points to those issues instead of becoming a second tracker.
  (Landed.)

## Next

- Pick one issue and branch/PR against it instead of expanding this local queue.
- Work issue #6 next: define the HTTP backend contract and registry metadata.
- Finish backend physical re-home under `needle.backends`.
- Refresh the PRD current-state sections after the structural slices land.

## Decisions Needed

These should be surfaced before they block code:

1. Should HTTP backend support be a 1.0 promise or a post-1.0 documented path?
2. Should package policy own min chars and minimum savings ratio directly, or
   should those remain adapter-local overrides with package-visible defaults?
3. How strict should the first evidence validation be: checked local fixture
   files now, or recognized placeholder references until the demo slice?
