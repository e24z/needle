import {
	ANIMATION_MS,
	INTENSITY_CODES,
	PULSE_FRAMES,
	SEP,
	SPIN_FRAMES,
	STATE_CODES,
} from "./config.js";
import { estimatedSavedTokens, formatCount, formatStoredCostRange } from "./state.js";
import { parseStatusMode } from "./status-modes.js";

export function renderStatus(ctx, state) {
	ctx.ui?.setStatus?.("needle", formatStatus(state));
}

export function decideStatusState(state) {
	if (!state.needleOn) return "off";
	if (state.backendStatus === "failed") return "failed";
	if (state.busyPrunes > 0) return "busy";
	if (state.backendStatus === "resident") return "resident";
	return "loading";
}

export function formatStatus(state, options = {}) {
	const visual = decideStatusState(state);
	const now = options.nowMs ?? Date.now();
	const tick = Math.floor(now / ANIMATION_MS);
	const indicator = formatIndicator(visual, tick);
	const calls = Number(state.counters.calls || 0);
	const saved = Number(state.counters.savedChars || 0);
	const plural = calls === 1 ? "" : "s";
	const mode = parseStatusMode(options.statusMode) || parseStatusMode(state.statusMode) || "chars";
	const forms = statusFormsForMode({
		mode,
		savedChars: saved,
		savedTokens: estimatedSavedTokens(state.counters, saved),
		costLowEstimate: state.counters.costLowEstimate,
		costHighEstimate: state.counters.costHighEstimate,
		calls,
		plural,
	});
	const cols = Number(options.columns || process.env.COLUMNS || 80);
	for (const line of forms) {
		if (2 + line.length <= cols - 1) return `${indicator} ${line}`;
	}
	return `${indicator} needle`;
}

function statusFormsForMode({
	mode,
	savedChars,
	savedTokens,
	costLowEstimate,
	costHighEstimate,
	calls,
	plural,
}) {
	if (mode === "compact") {
		return [
			`needle${SEP}${formatCount(savedChars)}c${SEP}${calls}p`,
			"needle",
		];
	}
	if (mode === "tokens") {
		return [
			`needle${SEP}~${formatCount(savedTokens)} input tokens avoided${SEP}${calls} prune${plural}`,
			`needle${SEP}~${formatCount(savedTokens)}t${SEP}${calls}p`,
			"needle",
		];
	}
	if (mode === "cost") {
		const range = formatStoredCostRange(costLowEstimate, costHighEstimate);
		if (range) {
			return [
				`needle${SEP}${range} est input avoided${SEP}${calls} prune${plural}`,
				`needle${SEP}${range} est${SEP}${calls}p`,
				"needle",
			];
		}
		return [
			`needle${SEP}pricing unavailable${SEP}${calls} prune${plural}`,
			`needle${SEP}no price${SEP}${calls}p`,
			"needle",
		];
	}
	return [
		`needle${SEP}${formatCount(savedChars)} chars trimmed${SEP}${calls} prune${plural}`,
		`needle${SEP}${formatCount(savedChars)}c${SEP}${calls}p`,
		"needle",
	];
}

function formatIndicator(visual, tick) {
	if (visual === "loading" || visual === "busy") {
		return ansi(STATE_CODES[visual], SPIN_FRAMES[tick % SPIN_FRAMES.length]);
	}
	if (visual === "resident") {
		return ansi(STATE_CODES.resident, PULSE_FRAMES[tick % PULSE_FRAMES.length]);
	}
	if (visual === "failed") return ansi(STATE_CODES.failed, "✗");
	return breathe(STATE_CODES.off, "·", tick);
}

function ansi(code, text) {
	return `\x1b[${code}m${text}\x1b[0m`;
}

function breathe(code, glyph, tick) {
	const intensity = INTENSITY_CODES[tick % INTENSITY_CODES.length];
	return ansi(intensity ? `${code};${intensity}` : code, glyph);
}
