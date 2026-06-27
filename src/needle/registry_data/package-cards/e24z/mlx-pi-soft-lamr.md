# MLX Pi Soft LAMR

Package: `e24z/mlx-pi-soft-lamr`

MLX Pi Soft LAMR keeps Pi's native tools in place while pruning large
`read` and `bash` observations before they are returned to the model. It uses
the same SWE-Pruner scoring path as the reference package, then applies Python
AST mask repair so pruned Python output is less likely to lose enclosing
definitions.

- Implements: `e24z/soft-lamr`
- Extends: `swe-pruner/reference`
- Uses backend: `e24z/code-pruner-mlx`
- Host binding: `pi/native-tools`
- Runtime profile: `local_mlx_adaptive`
- Privacy default: local-only
- Status metric: exact characters trimmed
- Repair: Python AST mask expansion
- Evidence: `fixture_pack:mlx-pi-soft-lamr`

This is the default Pi package. Treat it as Needle's product path, not strict
SWE-Pruner paper-reference behavior. If `context_focus_question` is missing,
the package passes the original tool output through unchanged.

The runtime profile is local MLX tuning, not a capability claim. It keeps batch
size at 1 on constrained Macs, uses a 2048-token window for small and medium
observations, and switches to 1024-token windows for larger observations. Restart
the resident Needle runtime after changing packages or profile settings.
