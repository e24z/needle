# Distribution

Needle's user-facing product is an installed CLI/runtime, not a source checkout.

## Ownership Model

- Needle owns the CLI, runtime lifecycle, built-in registry snapshot, local
  model directory, and host setup commands.
- Pi owns Pi's package/extension mechanism.
- `needle setup pi` is an orchestrator: it calls Pi's native package command
  with Needle's packaged Pi adapter directory.
- The source repo is for contributors. The release artifact is for users.

## Release Surfaces

### Public Mac Path

```bash
brew install e24z/tap/needle
needle setup pi
```

The tap is expected to live in a separate repository, for example
`e24z/homebrew-tap`, with a formula copied from `packaging/homebrew/Formula`.

### Early Tester Path

```bash
uv tool install --editable .
needle setup pi
```

This still exposes the user to a source checkout, so it is not the preferred
public 1.0 path.

### Python App Path

Once a wheel is published, `pipx` is the fallback for users who do not want
Homebrew:

```bash
pipx install needle
needle setup pi
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
5. Run `npm run demo:pi-canary` from the source checkout.
6. Tag the release.
7. Update the Homebrew formula URL and SHA256 in the tap.
8. Install with `brew install e24z/tap/needle`.
9. Run `needle setup pi`, open Pi, and verify `/needle doctor`.
