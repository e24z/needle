# Needle

Needle is a local pruning layer for agent coding tools. It sits between a tool
call and the model:

```text
tool output -> Needle -> original text or shorter text
```

The default 1.0 path is [Pi](https://github.com/mariozechner/pi). Needle extends
Pi's native `read` and `bash` tools, so the workflow still feels like Pi. The
portable path is an MCP server for Claude Code, Codex, and other MCP hosts. That
server exposes one observation tool:

```text
needle_bash(command, context_focus_question?)
```

Needle only prunes large textual observations when the tool call includes a
`context_focus_question`. If the focus question is missing, Needle passes the
observation through unchanged.

## Install

For code that has landed on `main` and been copied into the public tap, install
Needle with Homebrew:

```bash
brew install --HEAD e24z/tap/needle
```

This is still a head-only install. The stable formula comes after the first real
release tag and tarball SHA.

Homebrew starts `needle setup` during install when it can run interactively. It
will not change Pi, Claude Code, or Codex until you confirm the host setup. If
Homebrew defers setup, run it yourself:

```bash
needle setup
```

Feature branches should use a source install instead:

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

## Try it quickly

Start with the dry run:

```bash
needle setup --dry-run
```

Then install the host you actually want to test:

```bash
needle setup pi
# or
needle setup claude-code
# or
needle setup codex
```

Needle checks for the host CLI before it tries to install anything:

```bash
pi --help
claude --help
codex --help
```

Inside Pi, check the active package:

```text
/needle doctor
```

The useful lines look like this:

```text
active package e24z/mlx-pi-soft-lamr
capability e24z/soft-lamr
backend e24z/code-pruner-mlx
runtime profile local_mlx_adaptive
compute local_mlx | privacy local_only
```

From a source checkout, the no-model canary exercises the Pi adapter without MLX
or paid API calls:

```bash
npm run demo:pi-canary
```

For Claude Code or Codex, use the MCP tool explicitly. A run only passed through
Needle if the transcript shows a `needle_bash` call. Native Bash output is not
rewritten behind the host's back.

## Packages in plain English

Needle's registry has a few layers. Most users only choose a package, but the
layers matter when you are comparing behavior:

- Protocol: the smallest contract, `text -> text' | text`.
- Capability: the pruning policy, such as `swe-pruner/reference` or
  `e24z/soft-lamr`.
- Backend: the implementation that does the scoring, such as
  `e24z/code-pruner-mlx`.
- Host binding: where Needle plugs into an agent, such as Pi native tools or MCP
  bash.
- Package: the bundle a user installs or selects.
- Evidence: local fixtures or benchmark records for that package.

Most people should start with `e24z/mlx-pi-soft-lamr`. It is the intended Pi
product path: Pi native `read` and `bash`, the local MLX backend, SWE-Pruner
scoring, and Python AST repair.

Use `e24z/mlx-pi-reference` when you want the Pi comparison path without AST
repair. Use `e24z/mlx-mcp-bash-reference` for Claude Code, Codex, or another MCP
host that can call `needle_bash`.

Needle reports exact characters removed locally. Token and dollar savings are
estimates unless you pair them with a benchmark run or provider billing data.
The `local_mlx_adaptive` runtime profile is local Mac tuning, not a product
claim. It keeps batch size at 1 on constrained machines, uses a 2048-token window
for small and medium observations, and switches to 1024-token windows for larger
observations. `NEEDLE_MLX_MAX_LENGTH` wins if you set it for an experiment.

## Uninstall

Remove host adapters through Needle's setup command:

```bash
needle setup pi --uninstall
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

## Source layout

The public tree is meant to stay small:

- `needle/` has the CLI, runtime, adapters, backends, and built-in registry.
- `needle/hosts/pi/` has the Pi package.
- `needle/hosts/mcp/` has the portable MCP bash server.
- `packaging/homebrew/` has the formula source for the public tap.
- `tests/` has direct script tests.

Local implementation notes live next to the surface they explain, for example
`needle/hosts/pi/README.md`, `needle/hosts/mcp/README.md`, and
`packaging/homebrew/README.md`.

## Developer commands

```bash
needle package doctor --host-binding pi/native-tools
needle package doctor --host-binding mcp/bash
needle evidence check --host-binding pi/native-tools
needle evidence check --host-binding mcp/bash
needle setup --dry-run
needle setup pi --dry-run
needle setup claude-code --dry-run
needle setup codex --dry-run
needle statusline claude-code --plain
```

The built-in registry lives in `needle/registry_data`. External registries can be
tested with `NEEDLE_REGISTRY_ROOT=/path/to/registry`.
