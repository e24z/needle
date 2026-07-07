// Needle Pi extension: routes native read/bash observations through the
// local Needle daemon.
//
// Blocking semantics: when Needle is on, supported observations wait for the
// daemon and resident model. Cold starts, slow loads, and memory refusals
// return visible unpruned-output banners instead of silently passing through.
// `context_focus_question` is required by the tool schema; if it is missing,
// the extension returns a visible unpruned-output banner.

import { readConfiguredStatuslineAppearance, statusRepaintMs } from "./appearance.js";
import { ensureDaemon, request } from "./client.mjs";
import {
	BASH_TOOL,
	HEARTBEAT_MS,
	READ_TOOL,
	STATUS_POLL_MS,
} from "./config.js";
import { registerNeedleCommand, registerNeedleShortcuts } from "./commands.js";
import { ensureEnabled, pollStatus } from "./daemon.js";
import { loadPiTools } from "./pi-tools.js";
import {
	createNeedleState,
	restoreState,
	updatePricingFromContext,
} from "./state.js";
import { decideStatusState, formatStatus, renderStatus } from "./statusline.js";
import {
	buildToolResultPatch,
	extractText,
	registerOverride,
	splitEnvelope,
	withRequiredFocusQuestion,
} from "./tool-overrides.js";

export {
	buildToolResultPatch,
	decideStatusState,
	extractText,
	formatStatus,
	splitEnvelope,
	withRequiredFocusQuestion,
};

export default async function needlePiExtension(pi) {
	return installNeedlePiExtension(pi, await loadPiTools());
}

export function installNeedlePiExtension(pi, options = {}) {
	const state = createNeedleState(options, {
		requestFn: request,
		ensureDaemonFn: ensureDaemon,
	});
	let heartbeatTimer;
	let statusPollTimer;
	let animationTimer;

	registerNeedleCommand(pi, state);
	registerNeedleShortcuts(pi, state);
	registerOverride(pi, state, options.createReadTool, READ_TOOL);
	registerOverride(pi, state, options.createBashTool, BASH_TOOL);

	pi.on("session_start", async (_event, ctx) => {
		state.sessionId = ctx.sessionManager?.getSessionId?.() || `pi-${Date.now()}`;
		state.statusline = readConfiguredStatuslineAppearance();
		const restored = restoreState(ctx.sessionManager?.getEntries?.() || []);
		Object.assign(state.counters, restored.counters);
		if (restored.statusMode) state.statusMode = restored.statusMode;
		updatePricingFromContext(state, ctx);
		// Start enablement in the background so the session UI appears
		// immediately. Tool calls await the same promise, so the first
		// observation still waits for the daemon and resident model.
		ensureEnabled(state).catch(() => undefined);
		heartbeatTimer = setInterval(() => {
			if (state.sessionId && state.needleOn) {
				state.requestFn("heartbeat", { session: state.sessionId }).catch(() => undefined);
			}
		}, HEARTBEAT_MS);
		statusPollTimer = setInterval(() => {
			pollStatus(state).catch(() => undefined);
		}, STATUS_POLL_MS);
		animationTimer = setInterval(() => {
			renderStatus(ctx, state);
		}, statusRepaintMs(state.statusline));
		renderStatus(ctx, state);
	});

	pi.on("model_select", async (event, ctx) => {
		updatePricingFromContext(state, ctx, event);
		renderStatus(ctx, state);
	});

	pi.on("session_shutdown", async () => {
		if (heartbeatTimer) clearInterval(heartbeatTimer);
		if (statusPollTimer) clearInterval(statusPollTimer);
		if (animationTimer) clearInterval(animationTimer);
		if (state.sessionId && state.needleOn) {
			await state.requestFn("disable", { session: state.sessionId }).catch(() => undefined);
		}
	});
}
