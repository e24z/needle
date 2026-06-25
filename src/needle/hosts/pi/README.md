# Needle Pi Package

This directory is the packaged Pi adapter used by:

```bash
needle setup pi
```

It is intentionally small:

- `package.json` tells Pi to load `extension.js`.
- `extension.js` wraps Pi's native `read` and `bash` tools.
- `client.mjs` talks to the local Needle runtime socket.
- `demo-canary.mjs` replays local fixture cases without MLX or paid APIs.

Needle owns this adapter. Pi still owns the package install/uninstall mechanism.
