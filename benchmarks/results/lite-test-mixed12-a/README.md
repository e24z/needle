# lite-test-mixed12-a

Twelve-instance mixed SWE-bench Lite smoke slice, one early-index instance per
repository family:

- `astropy__astropy-12907`
- `django__django-10914`
- `matplotlib__matplotlib-18869`
- `mwaskom__seaborn-2848`
- `pallets__flask-4045`
- `psf__requests-1963`
- `pydata__xarray-3364`
- `pylint-dev__pylint-5859`
- `pytest-dev__pytest-11143`
- `scikit-learn__scikit-learn-10297`
- `sphinx-doc__sphinx-10325`
- `sympy__sympy-11400`

Latest paired mixed smoke:

| policy | resolved | validation errors | cost | api calls | note |
| --- | ---: | ---: | ---: | ---: | --- |
| `baseline` | 5/12 | 1 | 7.739553 | 175 | Mini-SWE baseline on Modal |
| `hay-2048-chunked` | 5/12 | 1 | 7.836748 | 208 | 2048-token chunk scoring; paired in the same run |

Against the paired baseline, `hay-2048-chunked` cost `0.097195` more
estimated dollars (`+1.3%`) and made `33` more API calls (`+18.9%`) while
preserving the exact same resolved set:

- `astropy__astropy-12907`
- `django__django-10914`
- `pylint-dev__pylint-5859`
- `scikit-learn__scikit-learn-10297`
- `sphinx-doc__sphinx-10325`

Both modes errored on `sympy__sympy-11400` during local validation because the
validation sandbox reported `pytest: command not found`. Treat the 5/12 result
as the acceptance truth for this paired run, but not as a clean publishable
benchmark claim for Sympy.

Hay telemetry recorded `212` rows, `48` accepted prunes, `15` chunked outputs,
`56730` accepted saved tokens, `86882` model input tokens, `max_chunks=4`, and
`0` low-memory passthroughs. The low-memory tripwire was enabled through
`HAY_BENCH_ABORT_ON_LOW_MEMORY=1`, so this run is not tainted by the earlier
low-memory pass-through problem.

Source run:
`benchmarks/runs/lite-test-mixed12-a__paired-hay-2048-chunked__modal__gpt-5-5__20260620-083343`

Provenance caveat: the result JSON records the worktree as dirty because
`benchmarks/slices/lite-test-mixed12-a.json` was new and untracked when the run
started. The run was generated from commit `e365040` plus that slice file; no
benchmark code changes occurred during the run.

Per-instance cost deltas:

| instance | baseline | hay | delta |
| --- | ---: | ---: | ---: |
| `astropy__astropy-12907` | 0.326466 | 0.289685 | -11.3% |
| `django__django-10914` | 0.566745 | 0.483572 | -14.7% |
| `matplotlib__matplotlib-18869` | 0.438757 | 0.745152 | +69.8% |
| `mwaskom__seaborn-2848` | 1.027130 | 0.981418 | -4.5% |
| `pallets__flask-4045` | 0.439917 | 0.763653 | +73.6% |
| `psf__requests-1963` | 0.468316 | 0.424404 | -9.4% |
| `pydata__xarray-3364` | 1.587946 | 1.506676 | -5.1% |
| `pylint-dev__pylint-5859` | 0.370758 | 0.440823 | +18.9% |
| `pytest-dev__pytest-11143` | 0.395964 | 0.241906 | -38.9% |
| `scikit-learn__scikit-learn-10297` | 0.380644 | 0.689281 | +81.1% |
| `sphinx-doc__sphinx-10325` | 0.995739 | 0.711698 | -28.5% |
| `sympy__sympy-11400` | 0.741171 | 0.558480 | -24.6% |

