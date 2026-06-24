# Needle

Needle is a local context-pruning layer for coding agents.

It sits between an agent tool and the model:

```text
tool output -> Needle -> original or pruned text
```

The 1.0 target hosts are [Pi](https://github.com/mariozechner/pi) and Claude
Code. Pi uses a native package that extends Pi's `read` and `bash` tools. Claude
Code uses a bash-minimal MCP server that exposes one observation tool,
`needle_bash(command, context_focus_question?)`. In both paths, Needle only
shortens large textual observations when the tool call includes a
`context_focus_question`.

## Install For Current Testers

Until the Homebrew tap exists, use the developer path from a clone of this repo.
This path requires Python 3.13 or newer and `uv`.

```bash
uv tool install --editable .
needle setup
```

## Planned Homebrew Install

The intended public Mac install is Homebrew once `e24z/homebrew-tap` exists:

```bash
brew install e24z/tap/needle
```

Homebrew starts `needle setup` during install when it can run interactively. If
setup is deferred, resume it with:

```bash
needle setup
```

The future Homebrew formula will install the Python runtime for you.

Needle's Pi setup expects Pi's CLI to be available:

```bash
pi --help
```

Needle's Claude Code setup expects Claude's CLI to be available:

```bash
claude --help
```

Check the adapter from inside Pi:

```text
/needle doctor
```

Run the no-model canary:

```bash
npm run demo:pi-canary
```

## Uninstall

Remove host adapters through their native setup flows:

```bash
needle setup pi --uninstall
needle setup claude-code --uninstall
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
also contains tests, planning docs, archived Claude hook work, and compatibility
shims; users should not need to clone or understand that material to try Needle.

Needle-owned runtime state defaults to `~/.needle`.

## Claims

Needle reports exact characters removed locally. Token and dollar savings are
estimates unless backed by a paired benchmark or provider billing data.

The default package is `e24z/mlx-pi-soft-lamr`. It uses the MLX backend with
Pi's `read` and `bash` tools, then adds Python AST repair on top of the
SWE-Pruner scoring path. The comparison package `e24z/mlx-pi-reference`
implements `swe-pruner/reference` without AST repair and should be treated as
the no-AST reference path, not the default product path.

The portable MCP package is `e24z/mlx-mcp-bash-reference`. It also implements
`swe-pruner/reference`, but its host binding is `mcp/bash` and its only tool is
`needle_bash`.

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
needle statusline claude-code --plain
```

The built-in registry lives in `needle/registry_data`. External registries can
be tested with `NEEDLE_REGISTRY_ROOT=/path/to/registry`.
