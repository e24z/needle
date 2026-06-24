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

## Now

### 1. Deepen Registry Validation

Goal:
Make invalid package graphs fail before runtime, with errors useful enough for
`/needle doctor` and future GitHub issues.

Acceptance:

- Packages validate `focus_contract`, `compute`, `runtime`, `privacy`,
  `accounting`, and `evidence`.
- Host bindings validate tool mappings by artifact kind, not only by host tool
  name.
- Backends validate `compute`, `interface`, `runtime`, and `launcher`.
- Capabilities validate `conforms_to` / `extends`, behavior recipe steps, focus,
  gates, rendering, and claim scope enough to catch obvious drift.
- Claim cards validate package/capability identity, metrics, known limits,
  must-not-claim text, and privacy notes.
- Evidence references either resolve to a checked local file or use a clearly
  recognized placeholder form that is surfaced as a remaining gap.

Verification:

```bash
PYTHONPATH=. python3 tests/test_package_config.py
for f in tests/test_*.py; do PYTHONPATH=. python3 "$f"; done
node tests/test_pi_client.mjs
git diff --check
```

### 2. Re-home Runtime Under Needle Names

Goal:
Stop making `pruner` the conceptual center of the product.

Acceptance:

- New imports can use `needle.runtime.*`.
- The old `pruner` package remains as a compatibility shim.
- Runtime docs and status text prefer Needle language.
- The backend launcher can later move from `-m pruner manage` to a Needle-owned
  module without changing package manifests again.

### 3. Archive Claude From The Active 1.0 Path

Goal:
Make the active tree reflect that Pi is the 1.0 host.

Acceptance:

- Claude plugin files move under `archive/claude/` or equivalent.
- Tests that remain active do not imply Claude is shipping in 1.0.
- Useful Claude lessons are preserved in docs or archive notes.

## Next

- Convert the accepted work queue into GitHub issues.
- Convert the CLI to Typer once command ownership is stable enough.
- Add a demo fixture and evidence pack for one visible read prune, one bash
  prune, and one missing-focus pass-through.
- Refresh the PRD current-state sections after the structural slices land.

## Decisions Needed

These should be surfaced before they block code:

1. Should HTTP backend support be a 1.0 promise or a post-1.0 documented path?
2. Should runtime state migrate from `~/.hay` to `~/.needle` before testers?
3. Should Claude be archived immediately after registry validation, or after
   runtime re-home?
4. Should package policy own min chars and minimum savings ratio directly, or
   should those remain adapter-local overrides with package-visible defaults?
5. How strict should the first evidence validation be: checked local fixture
   files now, or recognized placeholder references until the demo slice?

