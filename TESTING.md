# Testing Needle

A copy-paste test card for a clean machine. Total time: ~20 minutes plus one
model download (~1.5 GB). Everything below is safe to run on your own machine
and fully removable at the end.

**You need:** an Apple Silicon Mac, macOS 14+, ~4 GB free disk, python3, and
[Pi](https://github.com/mariozechner/pi) (`npm i -g @mariozechner/pi-coding-agent`).
8 GB RAM works; close heavy apps before the model-load steps.

Report anything that deviates from an **expect** line — that's the test.

## 1. Install

```bash
curl -fsSL https://e24z.github.io/needle/install.sh | bash
```

**Expect:** the script reports the installed binary path (`~/.local/bin/needle`)
and `needle --version` prints a version. If `~/.local/bin` is not on your PATH,
it tells you.

## 2. Setup wizard

```bash
needle
```

**Expect:** a five-step wizard (system check → pi check → worker environment →
model → pi integration). It asks before creating the venv, before the ~1.5 GB
model download, and before touching your Pi settings — each mutation is behind
its own confirmation. On completion it prints where everything went
(`~/Library/Application Support/Needle`).

Run `needle` again. **Expect:** a status summary, not the wizard — setup is
idempotent.

## 3. Pruning in a real Pi session

```bash
cd <some repo of yours> && pi
```

Ask Pi something that makes it read a large file, e.g.
*"Read the biggest source file in this repo and summarize what it does."*

**Expect:**
- The statusline shows a needle indicator: spinner while the model loads
  (first call after cold start blocks — this is by design), then
  `needle · Nk chars trimmed · N prunes`.
- The read observation in the transcript contains `[pruned]` markers and is
  visibly shorter than the file.
- The tool call includes a `context_focus_question` — Pi's model wrote it
  because the schema requires it.
- Pi's answer is still correct.

Then a failing command: ask Pi to run something that exits non-zero (e.g.
*"run `ls /nonexistent` and tell me what happened"*).

**Expect:** the exit status / error is intact in the observation — pruning
never eats exit codes.

## 4. Controls

Inside the same Pi session:

| command | expect |
| --- | --- |
| `/needle status` | mode, backend status, session count, chars trimmed |
| `/needle original` | the unpruned text of the last pruned observation |
| `/needle off` | notice; subsequent tool output arrives untouched, statusline shows off |
| `/needle on` | statusline returns (may spin while the model reloads) |

## 5. Campfire

Quit Pi, then:

```bash
needle status
```

**Expect:** no daemon running. The daemon lives only while sessions hold it —
last session out unloads the model and exits. `ps aux | grep needle` should
show nothing.

## 6. Uninstall

```bash
needle uninstall          # keeps the venv/model under NEEDLE_HOME
needle uninstall --purge  # removes the whole NEEDLE_HOME tree
```

**Expect:** the Pi integration is gone (`pi list` no longer shows needle), no
daemon, and after `--purge` no `~/Library/Application Support/Needle`.

## What to report

- Any observation that arrived unpruned **without** a visible
  `[needle ...]` banner explaining why — silent pass-through is a bug, loud
  failure is not.
- Wall-clock feel: model load time, per-prune stall, whether the machine
  swapped.
- Any wizard step that surprised you or asked for something unclear.
- The answer quality: did Pi ever miss something that was clearly in the
  original output? `/needle original` shows what was cut.
