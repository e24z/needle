# Needle MCP Bash

Needle MCP Bash is the portable MCP package for agents that can use local stdio
MCP servers. It exposes one observation tool:

```text
needle_bash(command, context_focus_question?)
```

Use it for shell-shaped observation. Keep mutation on the host's native edit,
write, or apply-patch tools.

This package does not intercept host-native tools. Claude Code's native Bash and
Codex's built-in Bash are not pruned by Needle. The host transcript must show a
`needle_bash` MCP call for the observation to have passed through Needle.

The installed server entrypoint is:

```bash
needle mcp serve
```

The MCP server exposes the tool. The pruning model still lives in Needle's
machine-wide runtime manager, so start it separately before expecting real
pruning:

```bash
needle runtime manage --host-binding mcp/bash
```

If `needle_bash` returns raw output, check:

```bash
needle status --events 20
```

Recent events distinguish missing focus questions, manager-unavailable
pass-throughs, manager timeouts, and no-savings pass-throughs.

For Claude Code, start with:

```bash
needle setup claude-code --dry-run
```

For Codex dogfooding, start with:

```bash
needle setup codex --dry-run
```

## Focus Contract

`context_focus_question` is optional so small or raw observations can pass
through unchanged. For pruning, it should be a complete question describing what
the agent is trying to learn from the shell output.

Missing focus questions pass through unchanged.
