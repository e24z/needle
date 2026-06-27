# MLX Pi Reference

Package: `e24z/mlx-pi-reference`

Needle for Pi keeps Pi's native tools in place while pruning large `read` and
`bash` observations before they are returned to the model.

- Implements: `swe-pruner/reference`
- Uses backend: `e24z/code-pruner-mlx`
- Host binding: `pi/native-tools`
- Runtime profile: `local_mlx_adaptive`
- Privacy default: local-only
- Status metric: exact characters trimmed
- Evidence: `fixture_pack:mlx-pi-reference`

If `context_focus_question` is missing, the package passes the original tool
output through unchanged.

The runtime profile is local MLX tuning, not SWE-Pruner behavior. It keeps batch
size at 1 on constrained Macs, uses a 2048-token window for small and medium
observations, and switches to 1024-token windows for larger observations. Restart
the resident Needle runtime after changing packages or profile settings.
