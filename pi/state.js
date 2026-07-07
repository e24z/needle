import { defaultStatuslineAppearance } from "./appearance.js";
import {
	CACHE_READ_COST_PER_MILLION,
	CUSTOM_STATE,
	DEFAULT_STATUS_MODE,
	EST_CHARS_PER_TOKEN,
	INPUT_COST_PER_MILLION,
} from "./config.js";
import { parseStatusMode } from "./status-modes.js";

export function createNeedleState(options = {}, defaults = {}) {
	return {
		sessionId: "",
		needleOn: true,
		backendStatus: "loading",
		lastError: null,
		busyPrunes: 0,
		counters: emptyCounters(),
		statusMode: DEFAULT_STATUS_MODE,
		inputCostPerToken: Number.isFinite(INPUT_COST_PER_MILLION)
			? INPUT_COST_PER_MILLION / 1_000_000
			: null,
		cacheReadCostPerToken: Number.isFinite(CACHE_READ_COST_PER_MILLION)
			? CACHE_READ_COST_PER_MILLION / 1_000_000
			: null,
		requestFn: options.requestFn || defaults.requestFn,
		ensureDaemonFn: options.ensureDaemonFn || defaults.ensureDaemonFn,
		nowFn: options.nowFn || (() => Date.now()),
		statusline: defaultStatuslineAppearance(),
		enablePromise: null,
		lastStatusPollMs: Number.NEGATIVE_INFINITY,
		statusPollInFlight: false,
	};
}

export function emptyCounters() {
	return {
		calls: 0,
		originalChars: 0,
		prunedChars: 0,
		savedChars: 0,
		originalTokensEstimate: 0,
		prunedTokensEstimate: 0,
		savedTokensEstimate: 0,
		costLowEstimate: null,
		costHighEstimate: null,
	};
}

export function restoreState(entries) {
	const counters = emptyCounters();
	let statusMode = null;
	try {
		for (const entry of entries) {
			if (entry.type === "custom" && entry.customType === CUSTOM_STATE && entry.data) {
				if (entry.data.counters) Object.assign(counters, entry.data.counters);
				else Object.assign(counters, entry.data);
				const restoredMode = parseStatusMode(entry.data.statusMode);
				if (restoredMode) statusMode = restoredMode;
			}
		}
	} catch {
		// Fresh sessions have nothing to restore.
	}
	return { counters, statusMode };
}

export function persistState(pi, state) {
	pi.appendEntry?.(CUSTOM_STATE, {
		counters: { ...state.counters },
		statusMode: state.statusMode,
	});
}

export function record(counters, originalText, prunedText, tool, state) {
	const originalLen = originalText.length;
	const prunedLen = prunedText.length;
	const originalTokens = estimateInputTokens(originalText);
	const prunedTokens = estimateInputTokens(prunedText);
	const savedTokens = Math.max(0, originalTokens - prunedTokens);
	counters.calls += 1;
	counters.originalChars += originalLen;
	counters.prunedChars += prunedLen;
	counters.savedChars = counters.originalChars - counters.prunedChars;
	counters.originalTokensEstimate += originalTokens;
	counters.prunedTokensEstimate += prunedTokens;
	counters.savedTokensEstimate = Math.max(
		0,
		counters.originalTokensEstimate - counters.prunedTokensEstimate,
	);
	recordCostEstimate(counters, savedTokens, state);
	counters.lastTool = tool;
	counters.updatedAt = Date.now();
}

export function formatCount(n) {
	if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
	if (n >= 10_000) return `${Math.round(n / 1_000)}k`;
	if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
	return String(n);
}

export function estimatedSavedTokens(counters, savedChars) {
	const saved = Number(counters?.savedTokensEstimate);
	if (Number.isFinite(saved) && saved > 0) return saved;
	return Math.max(0, Math.round(Number(savedChars || 0) / EST_CHARS_PER_TOKEN));
}

export function formatStoredCostRange(low, high) {
	const lowNumber = Number(low);
	const highNumber = Number(high);
	if (!Number.isFinite(lowNumber) || !Number.isFinite(highNumber) || highNumber <= 0) return null;
	const lowText = formatDollars(low);
	const highText = formatDollars(high);
	if (lowText === highText) return `~${highText}`;
	return `~${lowText}-${highText}`;
}

export function updatePricingFromContext(state, ctx, event) {
	if (Number.isFinite(INPUT_COST_PER_MILLION)) {
		state.inputCostPerToken = INPUT_COST_PER_MILLION / 1_000_000;
	}
	if (Number.isFinite(CACHE_READ_COST_PER_MILLION)) {
		state.cacheReadCostPerToken = CACHE_READ_COST_PER_MILLION / 1_000_000;
	}
	const cost = currentModelFromContext(ctx, event)?.cost;
	if (!Number.isFinite(INPUT_COST_PER_MILLION)) {
		const raw = Number(cost?.input);
		if (Number.isFinite(raw) && raw > 0) {
			state.inputCostPerToken = normalizeCost(raw);
		}
	}
	if (!Number.isFinite(CACHE_READ_COST_PER_MILLION)) {
		const raw = Number(cost?.cacheRead);
		if (Number.isFinite(raw) && raw > 0) {
			state.cacheReadCostPerToken = normalizeCost(raw);
		}
	}
}

function recordCostEstimate(counters, savedTokens, state) {
	const range = costRangeValues(savedTokens, state.inputCostPerToken, state.cacheReadCostPerToken);
	if (!range) return;
	counters.costLowEstimate = Number(counters.costLowEstimate || 0) + range.low;
	counters.costHighEstimate = Number(counters.costHighEstimate || 0) + range.high;
}

function estimateInputTokens(text) {
	return Math.max(0, Math.ceil(String(text || "").length / EST_CHARS_PER_TOKEN));
}

function costRangeValues(savedTokens, inputCostPerToken, cacheReadCostPerToken) {
	const highRate = Number(inputCostPerToken);
	if (!Number.isFinite(highRate) || highRate <= 0) return null;
	const lowRate = Number(cacheReadCostPerToken);
	const effectiveLowRate = Number.isFinite(lowRate) && lowRate > 0 ? lowRate : highRate;
	const low = savedTokens * Math.min(effectiveLowRate, highRate);
	const high = savedTokens * Math.max(effectiveLowRate, highRate);
	return { low, high };
}

function formatDollars(value) {
	if (!Number.isFinite(value) || value <= 0) return "$0.00";
	if (value < 0.01) return `$${value.toFixed(3)}`;
	if (value < 1) return `$${value.toFixed(2)}`;
	return `$${value.toFixed(2)}`;
}

function currentModelFromContext(ctx, event) {
	if (event?.model) return event.model;
	if (ctx?.model) return ctx.model;
	if (typeof ctx?.getModel === "function") return ctx.getModel();
	return null;
}

function normalizeCost(raw) {
	// Pi model configs describe cost per token. If a custom provider supplies a
	// human-style per-million value, keep the statusline estimate sane.
	return raw > 0.01 ? raw / 1_000_000 : raw;
}
