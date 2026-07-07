import {
	PALETTE_CODES,
	normalizeStatuslineAppearance,
	parseStatusSpinnerName,
	statusAnimationMs,
	statusSpinnerForName,
} from "./appearance.js";
import { SEP } from "./config.js";
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
	const statusline = normalizeStatuslineAppearance(options.statusline || state.statusline);
	if (options.statusSpinnerName) {
		const parsed = parseStatusSpinnerName(options.statusSpinnerName);
		if (parsed) {
			statusline.loading.spinner = parsed;
			statusline.busy.spinner = parsed;
		}
	}
	const appearance = statusline[visual] || statusline.loading;
	const spinner = statusSpinnerForName(appearance.spinner);
	const tick = Math.floor(now / statusAnimationMs(appearance, spinner));
	const indicator = formatIndicator(appearance, tick, spinner);
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

function formatIndicator(appearance, tick, spinner) {
	const color = PALETTE_CODES[appearance.color] || PALETTE_CODES.gray;
	return ansi(color, spinner.frames[tick % spinner.frames.length]);
}

function ansi(code, text) {
	return `\x1b[${code}m${text}\x1b[0m`;
}
