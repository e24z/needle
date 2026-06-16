"""Irreducible core of the context-pruning server (codename: Hay).

Nothing in here knows about Claude, plugins, monitors, hooks, or any model.
It is just a socket server that takes (text, query) and returns text, plus a
client to talk to it. The model and the lifecycle owner are bolted on later,
each behind its own wall.
"""

__version__ = "0.1.0"  # keep in step with pyproject.toml
