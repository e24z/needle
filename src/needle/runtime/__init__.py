"""Needle runtime namespace.

The resident manager, client, session lease, event log, memory guard, and wire
protocol live here.
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
