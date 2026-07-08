import { PRUNE_TIMEOUT_MS, STATUS_POLL_MS } from "./config.js";

/// Single-flight enablement: everyone (session start, tool calls, /needle on)
/// awaits the same attempt; a failed attempt clears so the next call retries.
export function ensureEnabled(state) {
	if (!state.enablePromise) {
		state.enablePromise = enableNeedle(state).then((ok) => {
			if (!ok) state.enablePromise = null;
			return ok;
		});
	}
	return state.enablePromise;
}

export async function pollStatus(state) {
	if (!state.needleOn) return;
	if (state.busyPrunes > 0) return;
	if (state.statusPollInFlight) return;
	const now = state.nowFn();
	if (now - state.lastStatusPollMs < STATUS_POLL_MS) return;
	state.lastStatusPollMs = now;
	state.statusPollInFlight = true;
	try {
		const response = await state.requestFn("status", {}, { timeoutMs: 1000 });
		if (response?.ok && response.backend_status) {
			state.backendStatus = response.backend_status;
			if (response.backend_status === "resident") state.lastError = null;
		}
	} catch {
		// Daemon gone or crashed: visible, not fatal. The next tool call
		// starts it again.
		if (state.backendStatus === "resident") state.backendStatus = "cold";
	} finally {
		state.statusPollInFlight = false;
	}
}

async function enableNeedle(state) {
	state.backendStatus = "loading";
	state.lastError = null;
	const up = await state.ensureDaemonFn();
	if (!up) {
		state.backendStatus = "failed";
		state.lastError = "needle daemon did not start";
		return false;
	}
	try {
		// Enable blocks until the model is resident; give it cold-load time.
		const response = await state.requestFn(
			"enable",
			{ session: state.sessionId },
			{ timeoutMs: PRUNE_TIMEOUT_MS },
		);
		state.backendStatus = response?.backend_status || (response?.ok ? "resident" : "failed");
		if (!response?.ok) state.lastError = response?.error || "enable failed";
		return Boolean(response?.ok);
	} catch (error) {
		state.backendStatus = "failed";
		state.lastError = String(error?.message || error);
		return false;
	}
}
