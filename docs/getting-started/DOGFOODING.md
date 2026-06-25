# Dogfooding Needle

Use this guide to test Needle as a local product, not as a SWE-bench run. The
goal is to answer three questions:

1. Can the host agent reach Needle?
2. Does the agent send a real `context_focus_question`?
3. Did Needle return a shorter observation without hiding the answer?

This is an experiment matrix. A run is not valid just because setup completed.

## Setup, Canary, Pruning

Keep these results separate when reporting a first run:

| Layer | What to run | Counts as success | Does not prove |
| --- | --- | --- | --- |
| Base install | `needle --help` and `needle setup --dry-run` | The CLI is present and can describe host setup. | Host installation or pruning. |
| Host setup | `needle setup pi`, `needle setup claude-code`, or `needle setup codex` | The host can see Needle after setup. | That the host invoked Needle on a real observation. |
| No-model canary | `npm run demo:pi-canary` from the source checkout | The Pi adapter/canary path works without model files. | Local MLX backend readiness. |
| Real MLX pruning | Backend extra installed, model downloaded, and transcript/status evidence shows a shorter result | A real observation passed through the local MLX backend. | A stable packaged backend/model install path. |

The base install and canary path can be healthy while real MLX pruning remains
developer preview. Do not describe a run as real pruning unless the backend and
model are available and the host transcript or Pi status proves Needle handled
that observation.

## Host Matrix

| Host | Setup | First check | Valid pruning signal | Invalidates the run |
| --- | --- | --- | --- | --- |
| Pi | `needle setup pi` | Open Pi and run `/needle doctor` | Pi uses wrapped `read` or `bash`; `/needle status` or the Pi UI shows accepted prunes / characters trimmed | Pi uses an unwrapped path, no focus question is present, output is below threshold, backend is unavailable |
| Claude Code | `needle setup claude-code` | Open Claude Code and run `/mcp` | The transcript shows the `needle_bash` MCP tool, with a real focus question and shorter returned text | Claude uses native Bash, no focus question is present, returned text is unchanged |
| Codex | `needle setup codex` | Start a fresh Codex thread and run `/mcp` | The transcript shows the `needle_bash` MCP tool, with a real focus question and shorter returned text | Codex uses built-in Bash, no focus question is present, returned text is unchanged |

## Important Boundary

Pi is the native 1.0 host. It wraps Pi's own `read` and `bash` observations.

Claude Code and Codex use Needle through MCP. MCP does not automatically replace
the host's built-in shell tools. The agent has to choose `needle_bash` for the
observation to pass through Needle.

Do not count a Claude Code or Codex run as pruned unless the transcript shows a
`needle_bash` tool call.

## Codex

Preview the Codex setup without changing anything:

```bash
needle setup codex --dry-run
```

Install the MCP server into Codex:

```bash
needle setup codex
```

Equivalent native command:

```bash
codex mcp add needle-bash -- needle mcp serve
```

Project-scoped config shape:

```toml
[mcp_servers.needle-bash]
command = "needle"
args = ["mcp", "serve"]
```

Start a fresh Codex thread after setup. Ask Codex to use `needle_bash` for large
read-only observations and to keep edits on native tools.

Example dogfood prompt:

```text
Use `needle_bash` for large read-only shell observations. Use native tools for
edits. Inspect the test output and focus on: which test failed and why?
```

## Claude Code

Preview setup:

```bash
needle setup claude-code --dry-run
```

Install:

```bash
needle setup claude-code
```

Open Claude Code and run `/mcp`. For dogfood prompts, explicitly ask Claude to
use `needle_bash` for large read-only observations.

## Pi

Preview setup:

```bash
needle setup pi --dry-run
```

Install:

```bash
needle setup pi
```

Open Pi and run:

```text
/needle doctor
```

The no-model canary is:

```bash
npm run demo:pi-canary
```

## What To Record

For each host, record:

- Host and version.
- Needle package id.
- Whether the model/backend was installed or the run used the canary/fake path.
- The focus question.
- Original characters and returned characters if visible.
- Whether the answer still contained the information the agent needed.
- Whether Needle was invoked through the intended tool surface.

## Stop Conditions

Stop and fix setup instead of continuing if:

- The host cannot see Needle.
- The transcript does not show `needle_bash` for MCP hosts.
- Pi does not show `/needle doctor`.
- The model/backend is missing but the run is being treated as real pruning.
- The run only proves setup, not pruning, but is being described as pruning.
