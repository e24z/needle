# Needle for Pi Tester Handoff

This is the 1.0 tester path for Needle running inside Pi.

Needle is installed as a Pi extension. Once Pi starts, the extension launches a
machine-wide local manager. Pi's native `read` and `bash` tools still run first;
Needle only stands between the tool output and the model. If the tool output is
large and the tool call includes `context_focus_question`, Needle prunes the
text before Pi returns it to the model.

## Scenario

Maya is testing Needle on a MacBook. She wants Pi to inspect a large repo without
spending model context on irrelevant file sections or noisy command output.

1. She installs the extension:

   ```bash
   cd /path/to/hay
   uv tool install --editable .
   pi install .
   ```

2. She starts Pi from a project:

   ```bash
   cd /path/to/project
   pi
   ```

3. She checks that Needle is really loaded:

   ```text
   /needle doctor
   ```

   The important lines are:

   ```text
   active package e24z/pi-local-mac
   capability swe-pruner/reference
   backend e24z/code-pruner-mlx
   compute local_mlx | privacy local_only
   ```

   `/hay` is a temporary alias for the same Pi command.

4. She asks Pi to inspect a large file or run a noisy command. Pi's tool call
   includes a `context_focus_question`, so Needle can score the output against
   what the model is looking for. Missing focus questions pass through
   unchanged.

5. She watches the footer or runs:

   ```text
   /needle status
   ```

   The status line reports exact characters trimmed in this Pi session. Token
   and dollar savings are estimates unless a separate billing-backed run proves
   them.

6. She lists available Pi-compatible packages:

   ```text
   /needle packages
   ```

   This is a Pi-local view. The durable package control plane is the
   host-neutral CLI installed above:

   ```bash
   needle package list --host-binding pi/native-tools
   needle package current --host-binding pi/native-tools
   needle package doctor --host-binding pi/native-tools
   needle evidence check --host-binding pi/native-tools
   ```

   The default package is `e24z/pi-local-mac`. It implements
   `swe-pruner/reference`, which means no AST repair. The alternative package is
   `e24z/pi-local-mac-soft-lamr`. It implements `e24z/soft-lamr`, which extends
   the reference behavior with Python AST repair.

   `needle evidence check` validates and lists the local fixture pack behind the
   package claim: one read prune case, one bash prune case, and one missing-focus
   pass-through case.

7. If she wants Soft-LaMR as her default package, she selects it with the CLI:

   ```bash
   needle package use e24z/pi-local-mac-soft-lamr
   ```

   If she has not installed the CLI yet, she can run the same command from the
   repo as `uv run needle package use e24z/pi-local-mac-soft-lamr`.

   If a manager is already resident, she stops it first so the new package
   policy and `/needle doctor` agree:

   ```bash
   needle stop
   ```

   For a one-off run, she can still use an environment override:

   ```bash
   NEEDLE_PACKAGE=e24z/pi-local-mac-soft-lamr pi
   ```

8. If she wants to disable Needle for one Pi run without uninstalling it:

   ```bash
   pi --no-extensions
   ```

9. If she wants to remove Needle completely:

   ```bash
   cd /path/to/hay
   pi uninstall .
   needle uninstall --yes
   uv tool uninstall needle
   ```

   `pi uninstall .` removes the Pi extension through Pi's native package flow.
   `needle uninstall --yes` removes Needle-owned local runtime/config/model
   files, which default to `~/.needle`. `uv tool uninstall needle` removes the
   CLI entrypoint installed in step 1.

10. If she wants to preview cleanup first:

   ```bash
   needle uninstall
   ```

## What Needle Claims

- It prunes large Pi `read` and `bash` observations before they reach the model.
- It requires an explicit `context_focus_question`.
- It reports exact character reduction locally.
- The default package keeps tool text on the local Mac.
- Runtime state defaults to `~/.needle`; old `HAY_*` env vars are compatibility
  aliases, not the preferred public surface.

## What Needle Does Not Claim

- Exact dollar savings for every user.
- SWE-Pruner paper parity when Soft-LaMR AST repair is enabled.
- Coverage for Pi tools other than `read` and `bash`.
- That pruning always helps; bad focus questions or tiny outputs pass through or
  may save little.
