# Archived Claude Adapter

This directory preserves the pre-1.0 Claude Code plugin work: hooks, monitors,
skills, statusline code, marketplace metadata, and Claude-only tests.

Needle 1.0 targets Pi. Claude is intentionally outside the active adapter tree
until a future package/binding revives it. Keep useful lessons here, especially:

- session leases should be host-specific tents around one machine-wide runtime;
- status presentation needs honest states for down, cold, loading, degraded,
  ready, and active;
- host adapters should fail open when the runtime is unavailable;
- output replacement belongs in the host binding, not the runtime core.

Do not install this archive as a shipping package. If Claude support returns,
promote it through a new host binding, package card, claim card, and active test
suite instead of wiring these files back in place wholesale.
