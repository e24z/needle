# Needle

Needle is a local context-pruning layer for coding agents.

It sits between an agent tool and the model:

```text
tool output -> Needle -> original or pruned text
```

The 1.0 product path is [Pi](https://github.com/mariozechner/pi). Needle extends
Pi's native `read` and `bash` tools, so users keep the normal Pi workflow. The
portable path is a bash-minimal MCP server for Claude Code, Codex, and other MCP
hosts; it exposes one observation tool,
`needle_bash(command, context_focus_question?)`.

In both paths, Needle only shortens large textual observations when the tool call
includes a `context_focus_question`. Missing focus questions pass through
unchanged.

## Install

For code that has already landed on `main` and been copied into the public tap,
the Mac install path is Homebrew. Until the first stable tag is cut, the tap is
pre-release/head-only:

```bash
brew install --HEAD e24z/tap/needle
```

Homebrew starts `needle setup` during install when it can run interactively. It
will not change Pi or Claude Code until you confirm a host install. If setup is
deferred, resume it with:

```bash
needle setup
```

The Homebrew formula installs the Python runtime and base CLI/MCP dependencies
for setup, package inspection, canaries, and MCP dogfooding. Real local MLX
pruning still requires backend dependencies and model files; that path is
developer preview until the backend extra is packaged cleanly. A future stable
formula will drop the `--HEAD` once a real release tarball SHA exists.

When validating a feature branch before it reaches `main`, use the source/dev
install path instead of Homebrew. It requires Python 3.13 or newer and `uv`:

```bash
uv tool install --editable .
needle setup
```

Needle's Pi setup expects Pi's CLI to be available:

```bash
pi --help
```

Needle's Claude Code setup expects Claude's CLI to be available:

```bash
claude --help
```

Needle's Codex dogfood setup expects the Codex CLI to be available:

```bash
codex --help
```

### First 10 Minutes

Use this path to prove setup separately from real MLX pruning:

| Goal | Command | Success means | Still not proven |
| --- | --- | --- | --- |
| Install the base CLI | Main/tap: `brew install --HEAD e24z/tap/needle`; feature branch: `uv tool install --editable .` | `needle` is installed with setup, registry, package, and MCP support. | Local MLX model pruning. |
| Preview setup | `needle setup --dry-run` | Needle can show the host changes it would make. | The host has accepted those changes. |
| Install a host adapter | `needle setup pi`, `needle setup claude-code`, or `needle setup codex` | The selected host can see Needle after you confirm setup. | That any given turn was pruned. |
| Run the no-model canary from a source checkout | `npm run demo:pi-canary` | The Pi adapter/canary path works without backend model files. | Real MLX pruning readiness. |
| Try real local MLX pruning from a clone | `uv tool install --editable '.[backend-code-pruner-mlx]'` then `needle model download` | The developer-preview backend deps and model path are present locally. | A stable packaged MLX install path. |

Base install, setup, MCP visibility, and the Pi canary can all work while real
local MLX pruning is still unavailable. Treat real MLX pruning as developer
preview until the backend dependencies and model path are packaged cleanly.

Check the adapter from inside Pi:

```text
/needle doctor
```

The important doctor lines should show the active package, capability, backend,
and runtime profile:

```text
active package e24z/mlx-pi-soft-lamr
capability e24z/soft-lamr
backend e24z/code-pruner-mlx
runtime profile local_mlx_adaptive
compute local_mlx | privacy local_only
```

Run the no-model canary:

```bash
npm run demo:pi-canary
```

The Pi canary and setup flow work without the model. Real pruning with the local
MLX backend also needs backend dependencies and model files. From a clone,
install the backend extra and download the model:

```bash
uv tool install --editable '.[backend-code-pruner-mlx]'
needle model dir
needle model download
```

## Uninstall

Remove host adapters through their native setup flows:

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

## What Ships

The installable product is the `needle` CLI/runtime plus a built-in registry
snapshot, the packaged Pi adapter, and the MCP bash adapter. The source repo
also contains tests, internal planning notes, diagnostics, and compatibility
shims; users should not need to clone or understand that material to try Needle.

Needle-owned runtime state defaults to `~/.needle`.

## Which Package Should I Use?

| Situation | Package | Host binding | Use it when | Boundary |
| --- | --- | --- | --- | --- |
| Default native Pi test | `e24z/mlx-pi-soft-lamr` | `pi/native-tools` | You want the intended Pi product path with AST repair. | Real pruning needs the MLX backend deps and model files. |
| Pi reference comparison | `e24z/mlx-pi-reference` | `pi/native-tools` | You want the SWE-Pruner reference behavior without AST repair. | This is a comparison path, not the default product path. |
| Claude Code or Codex dogfooding | `e24z/mlx-mcp-bash-reference` | `mcp/bash` | You can explicitly ask the host to call `needle_bash`. | Native Bash/shell output is not transparently rewritten. |

## Claims

Needle reports exact characters removed locally. Token and dollar savings are
estimates unless backed by a paired benchmark or provider billing data.

The default package is `e24z/mlx-pi-soft-lamr`. It uses the MLX backend with
Pi's `read` and `bash` tools, then adds Python AST repair on top of the
SWE-Pruner scoring path. The comparison package `e24z/mlx-pi-reference`
implements `swe-pruner/reference` without AST repair and should be treated as
the no-AST reference path, not the default product path.

Both MLX Pi packages use the `local_mlx_adaptive` runtime profile. That profile
is local launch tuning, not a capability claim: it keeps batch size at 1 on
constrained Macs, uses a 2048-token window for small and medium observations,
and switches to 1024-token windows for larger observations. Explicit
`NEEDLE_MLX_MAX_LENGTH` still wins for experiments.

The portable MCP package is `e24z/mlx-mcp-bash-reference`. It also implements
`swe-pruner/reference`, but its host binding is `mcp/bash` and its only tool is
`needle_bash`.

For local dogfooding across Pi, Claude Code, and Codex, use
[docs/getting-started/DOGFOODING.md](docs/getting-started/DOGFOODING.md). A
Claude Code or Codex run only counts as pruned when the transcript shows a
`needle_bash` MCP tool call; MCP setup does not rewrite native shell output.
For the Claude-specific flow, see
[docs/getting-started/CLAUDE-CODE-MCP.md](docs/getting-started/CLAUDE-CODE-MCP.md).

## Source Layout

The public release surface is intentionally small:

- `needle/`: CLI, runtime, host adapters, and built-in registry snapshot.
- `needle/backends/`: backend contract, fake/debug backends, and the current
  MLX code-pruner backend.
- `pruner/`: compatibility facade for legacy imports and `python -m pruner`.
- `packaging/`: Homebrew formula source used by the tap.
- `docs/getting-started/`: tester and dogfood flows.
- `docs/reference/`: stable user/developer references.
- `tests/` and `tools/`: direct-script tests and local diagnostics.

Planning history, archived host experiments, and performance notes live under
`docs/internal/`. They are maintainer notes, not the public tester path. Ignored
local archaeology, old benchmark runs, and teaching scratch files should stay
out of the source root. If needed locally, keep them under `.local-archive/`.

## Developer Notes

Useful commands:

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

The built-in registry lives in `needle/registry_data`. External registries can
be tested with `NEEDLE_REGISTRY_ROOT=/path/to/registry`.
