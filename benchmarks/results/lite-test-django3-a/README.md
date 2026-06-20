# lite-test-django3-a

Three-instance Django smoke slice:

- `django__django-11039`
- `django__django-11049`
- `django__django-11099`

Latest clean paired smoke:

| policy | resolved | cost | api calls | note |
| --- | ---: | ---: | ---: | --- |
| `baseline` | 3/3 | 1.516067 | 41 | Mini-SWE baseline on Modal |
| `hay-2048-chunked` | 3/3 | 0.914004 | 33 | 2048-token chunk scoring; paired in the same run |

Against the latest baseline smoke, `hay-2048-chunked` saved `0.602063`
estimated dollars (`39.7%`) while preserving local validation acceptance on
this slice. Hay telemetry recorded `32` rows, `7` accepted prunes, `2` chunked
outputs, `5655` accepted saved tokens, and `0` low-memory passthroughs. This is
a smoke result, not a publishable benchmark claim.

Source run:
`benchmarks/runs/lite-test-django3-a__paired-hay-2048-chunked__modal__gpt-5-5__20260619-224333`

Validity caveat: older global `~/.hay` events contain low-memory passthroughs,
including runs from 2026-06-17 and 2026-06-18. Result summaries include
`telemetry.low_memory_passthrough`; treat any nonzero value as invalid for
benchmark claims.

Older retained smoke summaries:

| policy | resolved | cost | api calls | note |
| --- | ---: | ---: | ---: | --- |
| `baseline` | 3/3 | 1.306428 | 39 | Backfilled from a raw run before manifest Git stamping |
| `hay-8192-floorless` | 3/3 | 1.367375 | 51 | Backfilled 8192-token policy smoke |
| `hay-2048-chunked` | 3/3 | 1.074160 | 38 | Combined from two Hay-only raw runs |
