# statusline/ — NOT a plugin component (settings-level)

`statusline.py` is referenced from `settings.json` `statusLine`, not auto-loaded
as a plugin component. It renders the manager's real residency state (queried
live) plus cumulative tokens saved — the part that converts real savings into the
"I can see it working" reaction.
