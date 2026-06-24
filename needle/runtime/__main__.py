"""Needle-owned module entrypoint for the resident runtime.

The implementation still delegates to the legacy pruner CLI while the runtime
is being re-homed. Keep this module as the active launcher so package manifests
and host adapters no longer need to name the compatibility package.
"""

from __future__ import annotations

from pruner.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
