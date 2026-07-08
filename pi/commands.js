import { needleHome, socketPath } from "./client.mjs";
import { SEP, STATUSLINE_SHORTCUTS, TOGGLE_SHORTCUTS } from "./config.js";
import { ensureEnabled } from "./daemon.js";
import { formatCount, persistState, updatePricingFromContext } from "./state.js";
import { renderStatus } from "./statusline.js";
import {
	STATUS_MODES,
	cycleStatusMode,
	parseStatusMode,
	statusModeLabel,
} from "./status-modes.js";

export function registerNeedleCommand(pi, state) {
	pi.registerCommand?.("needle", {
		description: "Needle status, recovery, and on/off",
		getArgumentCompletions: (prefix) => {
			const items = [
				"status",
				"original",
				"on",
				"off",
				"statusline",
				...STATUS_MODES.map((mode) => `statusline ${mode}`),
			].filter((item) => item.startsWith(prefix));
			return items.length ? items.map((value) => ({ value, label: value })) : null;
		},
		handler: async (args, ctx) => {
			const parts = String(args || "").trim().split(/\s+/).filter(Boolean);
			const sub = parts[0] || "status";
			if (sub === "off") {
				await setNeedleEnabled(state, ctx, false);
				return;
			}
			if (sub === "on") {
				await setNeedleEnabled(state, ctx, true, pi);
				return;
			}
			if (sub === "statusline") {
				const mode = parseStatusMode(parts[1]) || cycleStatusMode(state);
				state.statusMode = mode;
				updatePricingFromContext(state, ctx);
				persistState(pi, state);
				renderStatus(ctx, state);
				ctx.ui?.notify?.(`needle statusline: ${statusModeLabel(mode)}`, "info");
				return;
			}
			if (sub === "original") {
				const content = await buildOriginalMessage(state);
				if (typeof pi.sendMessage === "function") {
					pi.sendMessage({ customType: "needle-original", content, display: true });
				} else {
					ctx.ui?.notify?.(content, "info");
				}
				return;
			}
			const content = await buildStatusMessage(state);
			if (typeof pi.sendMessage === "function") {
				pi.sendMessage({ customType: "needle-status", content, display: true });
			} else {
				ctx.ui?.notify?.(content, "info");
			}
		},
	});
}

export function registerNeedleShortcuts(pi, state) {
	for (const shortcut of TOGGLE_SHORTCUTS) {
		pi.registerShortcut?.(shortcut, {
			description: "Toggle Needle pruning",
			handler: async (ctx) => {
				await setNeedleEnabled(state, ctx, !state.needleOn, pi);
			},
		});
	}
	for (const shortcut of STATUSLINE_SHORTCUTS) {
		pi.registerShortcut?.(shortcut, {
			description: "Cycle Needle statusline",
			handler: async (ctx) => {
				const mode = cycleStatusMode(state);
				updatePricingFromContext(state, ctx);
				persistState(pi, state);
				renderStatus(ctx, state);
				ctx.ui?.notify?.(`needle statusline: ${statusModeLabel(mode)}`, "info");
			},
		});
	}
}

async function setNeedleEnabled(state, ctx, enabled, pi) {
	if (!enabled) {
		state.needleOn = false;
		if (state.sessionId) {
			await state.requestFn("disable", { session: state.sessionId }).catch(() => undefined);
		}
		state.backendStatus = "cold";
		renderStatus(ctx, state);
		ctx.ui?.notify?.("needle off: tool output passes through untouched", "info");
		return true;
	}
	state.needleOn = true;
	state.enablePromise = null;
	const ok = await ensureEnabled(state);
	const content = await buildOnMessage(state, ok);
	renderStatus(ctx, state);
	if (typeof pi?.sendMessage === "function") {
		pi.sendMessage({ customType: "needle-status", content, display: true });
	} else {
		ctx.ui?.notify?.(content, ok ? "info" : "error");
	}
	return ok;
}

async function buildOnMessage(state, ok) {
	const status = await buildStatusMessage(state);
	return ok ? status : `needle: on failed\n${status}`;
}

async function buildStatusMessage(state) {
	const lines = [];
	let daemon = null;
	try {
		daemon = await state.requestFn("status", {}, { timeoutMs: 1000 });
	} catch {
		daemon = null;
	}
	if (!state.needleOn) {
		lines.push("needle: off (this session); /needle on to re-enable");
	} else if (!daemon?.ok) {
		lines.push("needle: daemon not running (starts on the next tool call)");
	} else {
		lines.push(
			`needle: ${daemon.mode}${SEP}backend ${daemon.backend_status}${SEP}${daemon.sessions} session${daemon.sessions === 1 ? "" : "s"}`,
		);
	}
	if (state.lastError) lines.push(`last error: ${state.lastError}`);
	lines.push(
		`this session: ${formatCount(state.counters.savedChars || 0)} chars trimmed${SEP}${state.counters.calls || 0} prunes`,
	);
	lines.push(`socket: ${resolvedPathLine(socketPath)}`);
	lines.push(`home: ${resolvedPathLine(needleHome)}`);
	return lines.join("\n");
}

async function buildOriginalMessage(state) {
	if (!state.sessionId) return "needle: no active session";
	try {
		const response = await state.requestFn(
			"original",
			{ session: state.sessionId },
			{ timeoutMs: 1000 },
		);
		if (response?.ok) return String(response.text ?? "");
		return `needle: ${response?.error || "no original cached for session"}`;
	} catch (error) {
		return `needle: original unavailable: ${String(error?.message || error)}`;
	}
}

function resolvedPathLine(resolvePath) {
	try {
		return resolvePath();
	} catch (error) {
		return `unavailable (${String(error?.message || error)})`;
	}
}
