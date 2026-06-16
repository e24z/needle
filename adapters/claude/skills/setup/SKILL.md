---
description: Set up Hay — wire the status line into settings, check prerequisites, and explain what the status line glyph means
---

# Hay Setup

One-time setup for Hay. The plugin ships its own hook and monitor, but the status
line is a Claude **settings** feature that a plugin cannot install for you — so
this skill wires it up, checks the prerequisites, and explains what you'll see.

Work through these steps and report what you did to the user.

## 1. Find the Hay repo

The status line needs an **absolute** path (Claude's `statusLine` does not expand
variables). Find where Hay is installed:

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/plugins/known_marketplaces.json")
data = json.load(open(p))
for name, m in (data.items() if isinstance(data, dict) else []):
    src = m.get("source", {})
    root = m.get("installLocation") or src.get("path")
    if root and os.path.exists(os.path.join(root, "adapters/claude/statusline.py")):
        print(root)
        break
PY
```

If that prints nothing, ask the user for the path to their `hay` clone and verify
`adapters/claude/statusline.py` exists under it. Call the result `<HAY>`.

## 2. Wire the status line

Read `~/.claude/settings.json` and set (or replace) the `statusLine` key, using the
absolute `<HAY>` path. Show the user the change before writing it:

```json
"statusLine": {
  "type": "command",
  "command": "python3 <HAY>/adapters/claude/statusline.py",
  "refreshInterval": 1
}
```

`refreshInterval: 1` is required — the glyph animates every second, so without it
the line looks frozen. If a `statusLine` already points at a `hay` path, just
update the path (idempotent); don't clobber an unrelated status line without
asking.

## 3. Check prerequisites

```bash
uv --version || echo "MISSING: install uv (https://docs.astral.sh/uv/) — the model manager runs under it"
```

Note for the user: the **first** prune triggers a one-time model download (several
hundred MB) and env build under `uv`, so the first session is slow to go green.
After that it's warm.

## 4. Verify

```bash
echo '{"session_id":"setup-check"}' | COLUMNS=100 python3 <HAY>/adapters/claude/statusline.py
```

A line like `· hay · 0 tokens saved · 0 prunes` means it works. The status line
appears in their UI on the next render (may need a new session).

## 5. Explain the glyph (always do this)

The leading glyph is Hay's real state, and it always animates. Tell the user what
theirs currently means:

- **`-` gray** — down: no manager running (hook is failing open; nothing is pruned).
- **`·` blue** — cold: manager up, model **not loaded**. Normal — it loads lazily
  on the first prune, or was evicted when idle / under memory pressure. *(This is
  the blue dot you'll usually see at rest.)*
- **`⠋` amber (spinning)** — loading: the manager is busy cold-loading the model or
  mid-prune.
- **`✗` red** — degraded: the manager is up but the real model couldn't load, so
  Hay is passing text through unchanged. The one to investigate (run `/hay status`).
- **`⠿` green (pulsing)** — ready: model resident and idle. Healthy.
- **`⠋` cyan (spinning)** — active: a prune landed in the last few seconds.

For a deeper snapshot (residency, memory pressure, recent events), point them at
`/hay status`.
