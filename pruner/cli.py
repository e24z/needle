"""Compatibility wrapper for the old `python -m pruner` runtime CLI."""

from needle.runtime.cli import *  # noqa: F401,F403
from needle.runtime.cli import _render_status, main  # noqa: F401
