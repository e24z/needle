import { parseStatusMode } from "./status-modes.js";

export const READ_TOOL = "read";
export const BASH_TOOL = "bash";
export const MIN_CHARS = Number.parseInt(process.env.NEEDLE_MIN_CHARS ?? "200", 10);
export const PRUNE_TIMEOUT_MS = envSecs("NEEDLE_PRUNE_TIMEOUT_SECS", 300) * 1000;
export const HEARTBEAT_MS = envSecs("NEEDLE_HEARTBEAT_INTERVAL", 30) * 1000;
export const STATUS_POLL_MS = envSecs("NEEDLE_STATUS_POLL_SECS", 5) * 1000;
export const ANIMATION_MS = envSecs("NEEDLE_ANIMATION_INTERVAL_SECS", 0.4) * 1000;
export const EST_CHARS_PER_TOKEN = envPositiveFloat("NEEDLE_EST_CHARS_PER_TOKEN", 4);
export const INPUT_COST_PER_MILLION = envPositiveFloat("NEEDLE_INPUT_COST_PER_MILLION", NaN);
export const CACHE_READ_COST_PER_MILLION = envNonNegativeFloat(
	"NEEDLE_CACHE_READ_COST_PER_MILLION",
	NaN,
);
export const CUSTOM_STATE = "needle-state";
export const SEP = " · ";
export const SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
export const PULSE_FRAMES = ["⠤", "⠶", "⠿", "⠶"];
export const DEFAULT_STATUS_MODE = parseStatusMode(process.env.NEEDLE_STATUSLINE_MODE) || "chars";
export const TOGGLE_SHORTCUTS = envList("NEEDLE_TOGGLE_SHORTCUTS", ["ctrl+shift+n", "f8"]);
export const STATUSLINE_SHORTCUTS = envList("NEEDLE_STATUSLINE_SHORTCUTS", [
	"ctrl+shift+.",
	"f9",
]);
export const INTENSITY_CODES = ["2", "", "1", ""];
export const STATE_CODES = {
	off: "38;5;240",
	loading: "38;5;179",
	failed: "38;5;196",
	resident: "38;5;35",
	busy: "38;5;87",
};

function envSecs(name, fallback) {
	const value = Number.parseFloat(process.env[name] ?? "");
	return Number.isFinite(value) && value > 0 ? value : fallback;
}

function envPositiveFloat(name, fallback) {
	const value = Number.parseFloat(process.env[name] ?? "");
	return Number.isFinite(value) && value > 0 ? value : fallback;
}

function envNonNegativeFloat(name, fallback) {
	const value = Number.parseFloat(process.env[name] ?? "");
	return Number.isFinite(value) && value >= 0 ? value : fallback;
}

function envList(name, fallback) {
	const raw = process.env[name];
	if (!raw) return fallback;
	const items = raw.split(",").map((item) => item.trim()).filter(Boolean);
	return items.length ? items : fallback;
}
