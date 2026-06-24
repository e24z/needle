"""Needle-owned module entrypoint for the resident runtime.

Package manifests and host adapters launch this module instead of the legacy
`pruner` compatibility package.
"""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
