"""Needle runtime namespace.

The resident manager, client, session lease, built-in runtime config, event log,
memory guard, and wire protocol live here.
"""

from . import client, config, events, manager, naming, protocol, session, sysmem

__all__ = [
    "client",
    "config",
    "events",
    "manager",
    "naming",
    "protocol",
    "session",
    "sysmem",
]
