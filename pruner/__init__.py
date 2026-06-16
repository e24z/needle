"""Irreducible core of the context-pruning engine (codename: Hay).

Nothing in here knows about Claude, plugins, monitors, hooks, or any model.
It is a machine-wide manager socket protocol that takes (text, query) and
returns text, plus clients/session leases/status helpers around that protocol.
The model and each host adapter are bolted on behind their own walls.
"""

__version__ = "0.1.0"  # keep in step with pyproject.toml
