"""Compatibility package for pre-Needle runtime imports.

New code should import `needle.runtime.*`. This package stays importable so
older local installs, tests, and scripts keep working during the 1.0 migration.
"""

__version__ = "0.1.0"  # keep in step with pyproject.toml
