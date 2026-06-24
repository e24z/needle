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
   pi install .
   ```

2. She starts Pi from a project:

   ```bash
   cd /path/to/project
   pi
   ```

3. She checks that Needle is really loaded:

   ```text
   /hay doctor
   ```

   The important lines are:

   ```text
   active package e24z/pi-local-mac
   capability swe-pruner/reference
   backend e24z/code-pruner-mlx
   compute local_mlx | privacy local_only
   ```

4. She asks Pi to inspect a large file or run a noisy command. Pi's tool call
   includes a `context_focus_question`, so Needle can score the output against
   what the model is looking for. Missing focus questions pass through
   unchanged.

5. She watches the footer or runs:

   ```text
   /hay status
   ```

   The status line reports exact characters trimmed in this Pi session. Token
   and dollar savings are estimates unless a separate billing-backed run proves
   them.

6. She lists available Pi-compatible packages:

   ```text
   /hay packages
   ```

   This is a Pi-local view. The durable package control plane is the
   host-neutral CLI:

   ```bash
   uv run -m pruner package list
   uv run -m pruner package current
   uv run -m pruner package doctor
   ```

   The default package is `e24z/pi-local-mac`. It implements
   `swe-pruner/reference`, which means no AST repair. The alternative package is
   `e24z/pi-local-mac-soft-lamr`. It implements `e24z/soft-lamr`, which extends
   the reference behavior with Python AST repair.

7. If she wants Soft-LaMR as her default package, she selects it with the CLI:

   ```bash
   uv run -m pruner package use e24z/pi-local-mac-soft-lamr
   ```

   If a manager is already resident, she stops it first so the new package
   policy and `/hay doctor` agree:

   ```bash
   uv run -m pruner stop
   ```

   For a one-off run, she can still use an environment override:

   ```bash
   HAY_PACKAGE=e24z/pi-local-mac-soft-lamr pi
   ```

8. If she wants to disable Needle for one Pi run without uninstalling it:

   ```bash
   pi --no-extensions
   ```

9. If she wants to remove Needle from Pi:

   ```bash
   cd /path/to/hay
   uv run -m pruner stop
   pi uninstall .
   ```

10. If she also wants to remove downloaded local model files:

   ```bash
   rm -rf ~/.hay/models
   ```

## What Needle Claims

- It prunes large Pi `read` and `bash` observations before they reach the model.
- It requires an explicit `context_focus_question`.
- It reports exact character reduction locally.
- The default package keeps tool text on the local Mac.

## What Needle Does Not Claim

- Exact dollar savings for every user.
- SWE-Pruner paper parity when Soft-LaMR AST repair is enabled.
- Coverage for Pi tools other than `read` and `bash`.
- That pruning always helps; bad focus questions or tiny outputs pass through or
  may save little.
