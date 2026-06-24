# Needle for Pi - Local Mac Soft-LaMR

Package: `e24z/pi-local-mac-soft-lamr`

Needle for Pi Soft-LaMR keeps Pi's native tools in place while pruning large
`read` and `bash` observations before they are returned to the model. It uses
the same SWE-Pruner scoring path as the reference package, then applies Python
AST mask repair so pruned Python output is less likely to lose enclosing
definitions.

- Implements: `e24z/soft-lamr`
- Extends: `swe-pruner/reference`
- Uses backend: `e24z/code-pruner-mlx`
- Host binding: `pi/native-tools`
- Privacy default: local-only
- Status metric: exact characters trimmed
- Repair: Python AST mask expansion
- Evidence: `fixture_pack:needle-soft-lamr`

Use this package when syntactic readability matters more than strict
SWE-Pruner paper-reference behavior. If `context_focus_question` is missing, the
package passes the original tool output through unchanged.
