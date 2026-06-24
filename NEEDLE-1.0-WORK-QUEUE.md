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

## Now

### 1. Add Demo Fixture And Evidence Pack

Goal:
Prove the Pi package behavior with small, local, repeatable fixtures before
public tester docs or benchmark claims.

Acceptance:

- A checked fixture exercises one visible read prune.
- A checked fixture exercises one bash/process-output prune.
- A checked fixture exercises missing `context_focus_question` pass-through.
- Claim-card evidence refs resolve to local files instead of placeholders.
- `needle package doctor` or a dedicated validator reports evidence status.

### 2. Convert Work Queue Into GitHub Issues

Goal:
Move durable coordination out of local Markdown once the evidence-pack shape is
concrete enough to split into public issues.

Acceptance:

- GitHub issues exist for demo/evidence, Typer CLI, runtime physical re-home,
  state rename/migration, and HTTP/backend contract.
- The local queue points to those issues instead of becoming a second tracker.

## Next

- Convert the CLI to Typer once command ownership is stable enough.
- Finish physical runtime re-home under `needle.runtime`/`needle.backends`.
- Refresh the PRD current-state sections after the structural slices land.

## Decisions Needed

These should be surfaced before they block code:

1. Should HTTP backend support be a 1.0 promise or a post-1.0 documented path?
2. Should runtime state migrate from `~/.hay` to `~/.needle` before testers?
3. Should package policy own min chars and minimum savings ratio directly, or
   should those remain adapter-local overrides with package-visible defaults?
4. How strict should the first evidence validation be: checked local fixture
   files now, or recognized placeholder references until the demo slice?
