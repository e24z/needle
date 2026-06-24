"""Compatibility shim for older imports.

Needle owns the registry/package ontology. The pruner runtime still imports
these helpers in a few places while the backend/config boundary settles.
"""

from needle.registry import *  # noqa: F401,F403
