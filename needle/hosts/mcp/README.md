# Needle MCP Bash

Needle MCP Bash is the portable MCP package for agents that can use local stdio
MCP servers. It exposes one observation tool:

```text
needle_bash(command, context_focus_question?)
```

Use it for shell-shaped observation. Keep mutation on the host's native edit,
write, or apply-patch tools.

The installed server entrypoint is:

```bash
needle mcp serve
```

For Claude Code, start with:

```bash
needle setup claude-code --dry-run
```

