"""Needle runtime namespace.

The implementation still lives in `pruner` during the migration. New Needle
code should import from `needle.runtime.*`; the old `pruner` package remains as
the compatibility entrypoint until the runtime can be physically re-homed.
"""

from . import client, events, manager, naming, protocol, session, sysmem

__all__ = [
    "client",
    "events",
    "manager",
    "naming",
    "protocol",
    "session",
    "sysmem",
]
