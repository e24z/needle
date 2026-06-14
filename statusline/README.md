# statusline/ — NOT a plugin component (settings-level)

`statusline.py` (Ring 4) is referenced from `settings.json` `statusLine`, not
auto-loaded as a plugin component. It reads a small state file written by the
hook and renders cumulative tokens saved — the part that converts real savings
into the "I can see it working" reaction.
