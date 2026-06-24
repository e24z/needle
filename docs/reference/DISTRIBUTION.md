# Distribution

Needle's user-facing product is an installed CLI/runtime, not a source checkout.

## Ownership Model

- Needle owns the CLI, runtime lifecycle, built-in registry snapshot, local
  model directory, and host setup commands.
- Pi owns Pi's package/extension mechanism.
- Claude Code owns Claude's MCP configuration mechanism.
- `needle setup pi` is an orchestrator: it calls Pi's native package command
  with Needle's packaged Pi adapter directory.
- `needle setup claude-code` is an orchestrator: it calls Claude Code's native
  MCP command and points it at `needle mcp serve`.
- The source repo is for contributors. The release artifact is for users.

## Release Surfaces

### Public Mac Path

```bash
brew install --HEAD e24z/tap/needle
```

The formula's `post_install` hook calls `needle setup --from-homebrew`. In an
interactive install this starts the guided setup flow. In a non-interactive
install it writes a pending setup marker and the formula caveats show how to
resume with `needle setup`.

The tap lives in the separate `e24z/homebrew-tap` repository. Before the first
stable release, it is a head-only formula. When cutting a stable release, add the
release tarball URL and SHA256 to the tap formula.

### Early Tester Path

```bash
uv tool install --editable .
needle setup
```

This still exposes the user to a source checkout, so it is not the preferred
public 1.0 path.

### Python App Path

Once a wheel is published, `pipx` is the fallback for users who do not want
Homebrew:

```bash
pipx install needle
needle setup
```

## Registry

1.0 ships a built-in registry snapshot under `needle/registry_data`.

An external registry can be selected with:

```bash
NEEDLE_REGISTRY_ROOT=/path/to/registry needle package list
```

A separate public registry repository should wait until third-party packages
exist. Before that, splitting it out creates an extra trust surface without a
user benefit.

## Release Checklist

1. Update `pyproject.toml`, `package.json`, and `needle/hosts/pi/package.json`
   to the same version.
2. Run the direct-script Python tests and the Node Pi tests.
3. Build the wheel/sdist and install it into a clean environment.
4. Run `needle setup pi --dry-run` from the installed artifact.
5. Run `needle setup claude-code --dry-run` from the installed artifact.
6. Run `needle mcp serve` through a stdio MCP client smoke.
7. Run `npm run demo:pi-canary` from the source checkout.
8. For pre-release taps, install with `brew install --HEAD e24z/tap/needle`.
9. For stable releases, tag the release and update the Homebrew formula URL and
   SHA256 in the tap.
10. Install with `brew install --HEAD e24z/tap/needle`; confirm the setup hook runs or
    prints a pending setup path.
11. Run `needle setup pi`, open Pi, and verify `/needle doctor`.
12. Run `needle setup claude-code`, open Claude Code, and verify `/mcp`.
13. Run `needle statusline claude-code --plain`.

For local pre-release Homebrew smoke tests, copy `packaging/homebrew/Formula/needle.rb`
into a throwaway tap and install `--HEAD` from that tap. Homebrew 6 rejects
direct formula-file installs outside a tap.
