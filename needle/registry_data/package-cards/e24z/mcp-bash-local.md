# Needle MCP Bash - Local

Package: `e24z/mcp-bash-local`

Needle MCP Bash exposes one portable MCP tool for shell-shaped observation:

```text
needle_bash(command, context_focus_question?)
```

Agents should use this tool for `rg`, `sed`, `git diff`, test output, and other
read-only command observations. Deliberate file mutation stays with the host's
native edit, write, or apply-patch tools.

- Implements: `swe-pruner/reference`
- Uses backend: `e24z/code-pruner-mlx`
- Host binding: `mcp/bash`
- Privacy default: local-only
- Status metric: exact characters trimmed
- Evidence: `fixture_pack:mcp-bash-reference`

If `context_focus_question` is missing, the package passes the original command
observation through unchanged.

