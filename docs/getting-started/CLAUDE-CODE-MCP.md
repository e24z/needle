# Claude Code MCP Setup

Needle supports Claude Code through an explicit MCP tool named `needle_bash`.

This is not transparent interception. Claude Code's native Bash tool is not
pruned by Needle. A Claude Code run only counts as a Needle run when the
transcript shows a `needle_bash` MCP tool call.

## Setup

Preview setup without changing Claude Code:

```bash
needle setup claude-code --dry-run
```

Install the MCP server:

```bash
needle setup claude-code
```

Equivalent Claude command:

```bash
claude mcp add --transport stdio --scope local needle-bash -- needle mcp serve
```

Open Claude Code and run:

```text
/mcp
```

You should see the `needle-bash` MCP server.

## How To Prompt

Ask Claude Code to use `needle_bash` for large read-only observations and to
keep edits on native tools.

Example:

```text
Use `needle_bash` for large read-only shell observations. Use native tools for
edits. Inspect the failing test output and focus on: which test failed and why?
```

The focus question should be self-contained. Good focus questions name what the
agent is trying to learn:

```text
Which test failed and what error caused it?
```

Avoid vague focus questions:

```text
this file
```

## What Counts As Success

A valid Claude Code dogfood run has all of these:

- `/mcp` shows `needle-bash`.
- The transcript shows Claude Code calling `needle_bash`.
- The call includes a meaningful `context_focus_question`.
- The returned text is shorter than the raw observation.
- The returned text still contains the information Claude needed.

## What Does Not Count

Do not count the run as pruned if:

- Claude Code used native Bash instead of `needle_bash`.
- The MCP server was installed but never called.
- The focus question was missing.
- The output was too small to prune.
- The backend/model was missing and the run was only proving setup.

## Status

`needle statusline claude-code --plain` reports Needle runtime health. It does
not prove that a particular Claude Code turn was pruned.

Use transcript evidence for per-turn proof.
