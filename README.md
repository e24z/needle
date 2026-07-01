# Needle

Needle is a local pruning layer for coding agents. It gives MCP-capable agents
one observation tool:

```text
needle_bash(command, context_focus_question?)
```

The product path is intentionally narrow:

- MCP exposes `needle_bash`.
- The runtime uses local MLX Soft-LaMR pruning.
- The model stays in Python/MLX, where the AI hot loop already lives.
- A machine-wide manager is shared by active sessions and unloads the model when
  nobody is around.

Needle does not transparently rewrite an agent's built-in Bash output. A
transcript only passed through Needle if it shows a `needle_bash` MCP call.

## Install

For code that has landed on `main` and been copied into the public tap, install
Needle with Homebrew:

```bash
brew install --HEAD e24z/tap/needle
```

This is still a head-only install. The stable formula comes after the first real
release tag and tarball SHA.

Homebrew runs a setup check after install. If your shell can answer prompts,
Needle may open setup immediately; if Homebrew defers it, run setup yourself:

```bash
needle setup
```

Feature branches should use a source install:

```bash
uv tool install --editable .
needle setup
```

Real local MLX pruning is a developer preview while the backend extra and model
download path settle. From a clone, use:

```bash
uv tool install --editable '.[backend-code-pruner-mlx]'
needle model dir
needle model download
```

## Try It Quickly

Start with the dry run:

```bash
needle setup --dry-run
```

Then connect the agent you actually want to test:

```bash
needle setup claude-code
# or
needle setup codex
```

Needle uses each host's own MCP command. It will not change Claude Code or Codex
until you confirm.

```bash
claude --help
codex --help
```

Start the runtime before expecting real pruning:

```bash
needle runtime manage
```

In Claude Code or Codex CLI, use the MCP tool explicitly. Native Bash output is
not rewritten behind the host's back.

Status helpers are available for hosts that can call an external status command:

```bash
needle statusline claude-code
needle statusline codex
needle status
```

## Runtime Model

Needle uses a campfire-shaped runtime: sessions come and go, but they lease a
shared local manager instead of owning the model directly. The manager lazy-loads
the pruning backend on the first prune, keeps it resident while sessions are
active, and evicts it after the last lease drops and the idle timer expires.

The built-in runtime identity is:

```text
runtime       mlx-soft-lamr
surface       mcp/bash
backend-id    code-pruner-mlx
profile       local_mlx_adaptive
```

The `local_mlx_adaptive` profile is local Mac tuning, not a product claim. It
keeps batch size at 1 on constrained machines, uses a 2048-token window for
small and medium observations, and switches to 1024-token windows for larger
observations. Hardware-specific batch-size tuning is later performance work, not
a release blocker. `NEEDLE_MLX_MAX_LENGTH` wins if you set it for an experiment.

Needle only prunes large textual observations when the tool call includes a
`context_focus_question`. If the focus question is missing, Needle passes the
observation through unchanged.

## Uninstall

Remove agent connections through Needle's setup command:

```bash
needle setup claude-code --uninstall
needle setup codex --uninstall
```

Remove Needle-owned local state:

```bash
needle uninstall --yes
```

Remove the CLI with the package manager you used:

```bash
brew uninstall needle
# or
uv tool uninstall needle
```

## Source Layout

The public tree is meant to stay small:

- `src/needle/` has the CLI, runtime, MCP host, and pruning backends.
- `src/needle/runtime/` owns the resident manager, socket wire format, event log,
  and session lease code.
- `src/needle/hosts/mcp/` has the MCP bash server.
- `src/needle/backends/code_pruner/` has the Python/MLX pruning path and repair
  logic.
- `packaging/homebrew/` has the formula source for the public tap.
- `tests/` has direct script tests.

Local implementation notes live next to the surface they explain, for example
`src/needle/README.md`, `src/needle/hosts/mcp/README.md`, and
`packaging/homebrew/README.md`. Root-level `docs/`, `tools/`, and `reference/`
are intentionally source-external and ignored here; use them for private notes
or local spikes, not release documentation. Tracked probes belong under
`tests/probes/`.

The installable product is `needle`. Legacy `pruner` imports or entrypoints are
not promised on this branch. `HAY_*` environment variables remain compatibility
aliases for early installs, while new docs should use `NEEDLE_*`.

## Developer Commands

```bash
needle setup --dry-run
needle setup claude-code --dry-run
needle setup codex --dry-run
needle runtime manage --help
needle statusline claude-code --plain
python3 tests/smoke_installed_artifact.py
```
