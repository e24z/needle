# Needle

Needle is a local context-pruning layer for coding agents.

It sits between an agent tool and the model:

```text
tool output -> Needle -> original or pruned text
```

The 1.0 target host is [Pi](https://github.com/mariozechner/pi), where Needle
extends Pi's native `read` and `bash` tools. The model still uses normal Pi
tools; Needle only shortens large textual observations when the tool call
includes a `context_focus_question`.

## Install

The intended public Mac install is Homebrew:

```bash
brew install e24z/tap/needle
needle setup pi
```

The tap is not published yet. Until the first tagged release, use the developer
path:

```bash
uv tool install --editable .
needle setup pi
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

Remove the Pi adapter through Pi's native package flow:

```bash
needle setup pi --uninstall
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
snapshot and the packaged Pi adapter. The source repo also contains tests,
planning docs, archived Claude work, and compatibility shims; users should not
need to clone or understand that material to try Needle.

Needle-owned runtime state defaults to `~/.needle`.

## Claims

Needle reports exact characters removed locally. Token and dollar savings are
estimates unless backed by a paired benchmark or provider billing data.

The default package is `e24z/pi-local-mac`, which implements the
`swe-pruner/reference` capability without AST repair. The alternate
`e24z/pi-local-mac-soft-lamr` package adds Python AST repair and should be
treated as a separate capability, not paper-parity SWE-Pruner.

## Developer Notes

Useful commands:

```bash
needle package doctor --host-binding pi/native-tools
needle evidence check --host-binding pi/native-tools
needle setup pi --dry-run
```

The built-in registry lives in `needle/registry_data`. External registries can
be tested with `NEEDLE_REGISTRY_ROOT=/path/to/registry`.
