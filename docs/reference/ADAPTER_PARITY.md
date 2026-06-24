# Adapter Parity

This matrix is the 1.0 checklist for Needle host integrations. If a row is
marked "gap", it is either a planned follow-up or a reason not to call that host
path done.

## 1.0 Host Matrix

| Surface | Pi native | Claude Code via MCP |
| --- | --- | --- |
| Install entrypoint | `needle setup pi` | `needle setup claude-code` |
| First-run path | `brew install --HEAD e24z/tap/needle` triggers `needle setup --from-homebrew`, then the wizard can choose Pi | `brew install --HEAD e24z/tap/needle` triggers `needle setup --from-homebrew`, then the wizard can choose Claude Code |
| Uninstall entrypoint | `needle setup pi --uninstall` | `needle setup claude-code --uninstall` |
| Native owner | Pi owns package install/uninstall | Claude Code owns MCP config |
| Default package | `e24z/mlx-pi-soft-lamr` | `e24z/mlx-mcp-bash-reference` |
| Host binding | `pi/native-tools` | `mcp/bash` |
| Tool surface | Wraps Pi `read` and `bash` observations | Exposes one explicit tool, `needle_bash(command, context_focus_question?)` |
| Goal hint | `context_focus_question` on wrapped tool params | Optional `context_focus_question` argument on `needle_bash` |
| Missing hint behavior | Pass through original text | Pass through original text |
| Default backend | `e24z/code-pruner-mlx` | `e24z/code-pruner-mlx` through the same runtime protocol |
| Runtime model | One machine-wide manager, leased by sessions | Same manager, reached through `needle mcp serve` |
| Status surface | Pi status bar plus `/needle status` and `/needle doctor` | `needle statusline claude-code`, `/mcp`, and `needle status` |
| Session savings | Pi tracks per-session accepted prunes and characters trimmed | Gap: MCP statusline reports runtime health, not per-session savings |
| Canary | `node needle/hosts/pi/demo-canary.mjs` | MCP stdio smoke for `needle mcp serve` |
| Public docs | `README.md`, `docs/reference/PI-ADAPTER.md` | `needle/hosts/mcp/README.md`, `README.md` |
| Test coverage | Python CLI tests plus Node Pi client tests | Python CLI tests plus MCP server smoke |

## 1.0 Decisions

- Pi remains the richest 1.0 host because it can wrap native tools and render
  session counters in the status bar.
- MCP is the portable reference host surface. It stays bash-minimal so closed
  source agents can use a predictable observation tool instead of relying on
  interception.
- Claude Code statusline support is intentionally minimal: it shows Needle
  runtime health from generic manager stats. It must not pretend to have Pi's
  per-session savings counters until the MCP path can actually provide them.
- Homebrew install should trigger setup, but host mutation still requires user
  confirmation.

## Gaps To Track

- MCP/Claude Code does not yet expose per-session accepted-prune counters.
- The Homebrew formula in this source repo still needs a real release tarball
  SHA before the non-HEAD public tap path can be cut.
- Full Homebrew Python resource refresh should happen inside the real tap before
  public release.
