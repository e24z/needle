# MLX MCP Bash Reference

Package: `e24z/mlx-mcp-bash-reference`

Needle MCP Bash exposes one portable MCP tool for shell-shaped command output:

```text
needle_bash(command, context_focus_question?)
```

`needle_bash` executes unsandboxed local `bash -c` and captures bounded output
for optional pruning. Agents should use it only for commands the user would be
comfortable running in a normal shell, such as `rg`, `sed`, `git diff`, and test
commands. Planned file mutation should stay with the host's native edit, write,
or apply-patch tools so changes remain visible in the host workflow.

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
