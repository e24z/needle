# MLX MCP Bash Reference

Package: `e24z/mlx-mcp-bash-reference`

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
- Runtime profile: `local_mlx_adaptive`
- Privacy default: local-only
- Status metric: exact characters trimmed
- Evidence: `fixture_pack:mlx-mcp-bash-reference`

If `context_focus_question` is missing, the package passes the original command
observation through unchanged.

The runtime profile is local MLX tuning, not SWE-Pruner behavior. It keeps batch
size at 1 on constrained Macs, uses a 2048-token window for small and medium
observations, and switches to 1024-token windows for larger observations. Restart
the resident Needle runtime after changing packages or profile settings.
