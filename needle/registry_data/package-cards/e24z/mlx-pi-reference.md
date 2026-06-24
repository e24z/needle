# MLX Pi Reference

Package: `e24z/mlx-pi-reference`

Needle for Pi keeps Pi's native tools in place while pruning large `read` and
`bash` observations before they are returned to the model.

- Implements: `swe-pruner/reference`
- Uses backend: `e24z/code-pruner-mlx`
- Host binding: `pi/native-tools`
- Privacy default: local-only
- Status metric: exact characters trimmed
- Evidence: `fixture_pack:mlx-pi-reference`

If `context_focus_question` is missing, the package passes the original tool
output through unchanged.
