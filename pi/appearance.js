import { readFileSync } from "node:fs";

import cliSpinners from "cli-spinners";

import { needlePaths } from "./client.mjs";
import { ANIMATION_INTERVAL_OVERRIDE_MS } from "./config.js";

export const DEFAULT_STATUSLINE_STATES = {
	loading: { spinner: "dots3", color: "amber", intervalMs: null },
	busy: { spinner: "dots2", color: "cyan", intervalMs: null },
	resident: { spinner: "simpleDots", color: "green", intervalMs: null },
	off: { spinner: "simpleDotsScrolling", color: "gray", intervalMs: null },
	failed: { spinner: "arc", color: "red", intervalMs: null },
};
const DEFAULT_STATUS_SPINNER_NAME = DEFAULT_STATUSLINE_STATES.loading.spinner;
const FALLBACK_STATUS_SPINNER = {
	interval: 80,
	frames: ["⠋", "⠙", "⠚", "⠞", "⠖", "⠦", "⠴", "⠲", "⠳", "⠓"],
};
export const PALETTE_CODES = {
	gray: "38;5;240",
	amber: "38;5;179",
	cyan: "38;5;87",
	green: "38;5;35",
	red: "38;5;196",
	blue: "38;5;75",
	white: "38;5;255",
};

export function readConfiguredStatuslineAppearance() {
	try {
		const config = JSON.parse(readFileSync(needlePaths().config, "utf8"));
		const statusline = defaultStatuslineAppearance();
		const legacy = parseStatusSpinnerName(config?.status_spinner);
		if (legacy) {
			statusline.loading.spinner = legacy;
			statusline.busy.spinner = legacy;
		}
		if (config?.status_spinners && typeof config.status_spinners === "object") {
			for (const state of Object.keys(DEFAULT_STATUSLINE_STATES)) {
				const spinner = parseStatusSpinnerName(config.status_spinners[state]);
				if (spinner) statusline[state].spinner = spinner;
			}
		}
		if (config?.statusline?.states && typeof config.statusline.states === "object") {
			for (const state of Object.keys(DEFAULT_STATUSLINE_STATES)) {
				const saved = config.statusline.states[state];
				if (!saved || typeof saved !== "object") continue;
				const spinner = parseStatusSpinnerName(saved.spinner);
				const color = parsePaletteColor(saved.color);
				const intervalMs = parseIntervalMs(saved.interval_ms);
				if (spinner) statusline[state].spinner = spinner;
				if (color) statusline[state].color = color;
				statusline[state].intervalMs = intervalMs;
			}
		}
		return normalizeStatuslineAppearance(statusline);
	} catch {
		return defaultStatuslineAppearance();
	}
}

export function parseStatusSpinnerName(name) {
	if (typeof name !== "string") return null;
	const spinner = cliSpinners[name];
	return spinner && Array.isArray(spinner.frames) && spinner.frames.length > 0 ? name : null;
}

function parsePaletteColor(name) {
	return typeof name === "string" && PALETTE_CODES[name] ? name : null;
}

function parseIntervalMs(value) {
	if (value === null || value === undefined) return null;
	const numeric = Number(value);
	return Number.isFinite(numeric) && numeric >= 20 && numeric <= 2000 ? Math.round(numeric) : null;
}

export function defaultStatuslineAppearance() {
	return normalizeStatuslineAppearance(DEFAULT_STATUSLINE_STATES);
}

export function normalizeStatuslineAppearance(value) {
	const base = {};
	for (const [state, defaults] of Object.entries(DEFAULT_STATUSLINE_STATES)) {
		const saved = value?.[state] || {};
		base[state] = {
			spinner: parseStatusSpinnerName(saved.spinner) || defaults.spinner,
			color: parsePaletteColor(saved.color) || defaults.color,
			intervalMs: parseIntervalMs(saved.intervalMs ?? saved.interval_ms),
		};
	}
	return base;
}

export function statusSpinnerForName(name) {
	const parsed = parseStatusSpinnerName(name) || DEFAULT_STATUS_SPINNER_NAME;
	const spinner = cliSpinners[parsed] || FALLBACK_STATUS_SPINNER;
	if (!Array.isArray(spinner.frames) || spinner.frames.length === 0) {
		return { name: DEFAULT_STATUS_SPINNER_NAME, ...FALLBACK_STATUS_SPINNER };
	}
	return {
		name: parsed,
		interval: Number.isFinite(spinner.interval) && spinner.interval > 0
			? spinner.interval
			: FALLBACK_STATUS_SPINNER.interval,
		frames: spinner.frames,
	};
}

export function statusAnimationMs(appearance, spinner) {
	if (ANIMATION_INTERVAL_OVERRIDE_MS) return ANIMATION_INTERVAL_OVERRIDE_MS;
	return appearance.intervalMs || spinner.interval;
}

export function statusRepaintMs(statusline) {
	if (ANIMATION_INTERVAL_OVERRIDE_MS) return ANIMATION_INTERVAL_OVERRIDE_MS;
	const appearance = normalizeStatuslineAppearance(statusline);
	const intervals = Object.values(appearance).map((state) => {
		const spinner = statusSpinnerForName(state.spinner);
		return statusAnimationMs(state, spinner);
	});
	return Math.max(20, Math.min(...intervals));
}
