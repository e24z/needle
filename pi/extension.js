// Needle Pi extension: routes native read/bash observations through the
// local Needle daemon.
//
// Blocking semantics: if Needle is on, supported observations go through the
// model. Cold, loading, or slow are not bypass reasons. Critical memory
// pressure is refused by the daemon and rendered loudly, never silent
// pass-through. `context_focus_question` is required by the tool schema; its
// absence (schema drift, host quirks) is also loud.

import { execFileSync } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { ensureDaemon, needleHome, request, socketPath } from "./client.mjs";

const READ_TOOL = "read";
const BASH_TOOL = "bash";
const MIN_CHARS = Number.parseInt(process.env.NEEDLE_MIN_CHARS ?? "200", 10);
const PRUNE_TIMEOUT_MS = envSecs("NEEDLE_PRUNE_TIMEOUT_SECS", 300) * 1000;
const HEARTBEAT_MS = envSecs("NEEDLE_HEARTBEAT_INTERVAL", 30) * 1000;
const STATUS_POLL_MS = envSecs("NEEDLE_STATUS_POLL_SECS", 1) * 1000;
const ANIMATION_MS = envSecs("NEEDLE_ANIMATION_INTERVAL_SECS", 0.4) * 1000;
const CUSTOM_STATE = "needle-state";
const SEP = " · ";
const SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const PULSE_FRAMES = ["⠤", "⠶", "⠿", "⠶"];
const STATE_CODES = {
	off: "38;5;240",
	loading: "38;5;179",
	failed: "38;5;196",
	resident: "38;5;35",
	busy: "38;5;87",
};

/// Trailing host envelope: Pi appends bracketed notices like
/// "[Showing lines 1-2000 of 5000. Full output: /tmp/...]" and bash failure
/// status lines after a blank line. Those never reach the model pruner and are
/// reattached verbatim.
const ENVELOPE_PATTERN = /(?:\n+(?:\[[^\n]*\]|Command (?:exited with code \d+|timed out after [^\n]+ seconds|aborted)))+\s*$/;

export default async function needlePiExtension(pi) {
	return installNeedlePiExtension(pi, await loadPiTools());
}

export function installNeedlePiExtension(pi, options = {}) {
	const state = {
		sessionId: "",
		needleOn: true,
		backendStatus: "loading",
		lastError: null,
		busyPrunes: 0,
		counters: emptyCounters(),
		requestFn: options.requestFn || request,
		ensureDaemonFn: options.ensureDaemonFn || ensureDaemon,
		enablePromise: null,
	};
	let heartbeatTimer;
	let statusTimer;

	registerNeedleCommand(pi, state);
	registerOverride(pi, state, options.createReadTool, READ_TOOL);
	registerOverride(pi, state, options.createBashTool, BASH_TOOL);

	pi.on("session_start", async (_event, ctx) => {
		state.sessionId = ctx.sessionManager?.getSessionId?.() || `pi-${Date.now()}`;
		Object.assign(state.counters, restoreCounters(ctx.sessionManager?.getEntries?.() || []));
		// Kick enablement off in the background: the session UI comes up
		// immediately and the statusline shows loading. Tool calls await the
		// same promise, so the first observation blocks until the daemon is
		// up and the model resident — never a race, never a bypass.
		ensureEnabled(state).catch(() => undefined);
		heartbeatTimer = setInterval(() => {
			if (state.sessionId && state.needleOn) {
				state.requestFn("heartbeat", { session: state.sessionId }).catch(() => undefined);
			}
		}, HEARTBEAT_MS);
		statusTimer = setInterval(() => {
			pollStatus(state).catch(() => undefined);
			renderStatus(ctx, state);
		}, Math.min(STATUS_POLL_MS, ANIMATION_MS));
		renderStatus(ctx, state);
	});

	pi.on("session_shutdown", async () => {
		if (heartbeatTimer) clearInterval(heartbeatTimer);
		if (statusTimer) clearInterval(statusTimer);
		if (state.sessionId && state.needleOn) {
			await state.requestFn("disable", { session: state.sessionId }).catch(() => undefined);
		}
	});
}

/// Single-flight enablement: everyone (session start, tool calls, /needle on)
/// awaits the same attempt; a failed attempt clears so the next call retries.
function ensureEnabled(state) {
	if (!state.enablePromise) {
		state.enablePromise = enableNeedle(state).then((ok) => {
			if (!ok) state.enablePromise = null;
			return ok;
		});
	}
	return state.enablePromise;
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

async function pollStatus(state) {
	if (!state.needleOn) return;
	try {
		const response = await state.requestFn("status", {}, { timeoutMs: 1000 });
		if (response?.ok && response.backend_status) {
			state.backendStatus = response.backend_status;
			if (response.backend_status === "resident") state.lastError = null;
		}
	} catch {
		// Daemon gone (campfire out, crash): visible, not fatal — the next
		// tool call re-lights it.
		if (state.backendStatus === "resident") state.backendStatus = "cold";
	}
}

// --- tool overrides ---------------------------------------------------------

function registerOverride(pi, state, createTool, toolName) {
	if (typeof pi.registerTool !== "function" || typeof createTool !== "function") return false;
	const template = createTool(process.cwd());
	const definition = {
		name: toolName,
		label: template.label || toolName,
		description: template.description,
		parameters: withRequiredFocusQuestion(template.parameters),
		async execute(toolCallId, params, signal, onUpdate, ctx) {
			const nativeTool = createTool(ctx?.cwd || process.cwd());
			let result;
			try {
				result = await nativeTool.execute(
					toolCallId,
					stripNeedleParams(params),
					signal,
					onUpdate,
					ctx,
				);
			} catch (error) {
				if (!state.needleOn) throw error;
				state.busyPrunes += 1;
				try {
					throw await pruneThrownToolError(toolName, error, params, state);
				} finally {
					state.busyPrunes = Math.max(0, state.busyPrunes - 1);
				}
			}
			if (!state.needleOn) return result;
			state.busyPrunes += 1;
			try {
				const patch = await buildToolResultPatch(toolName, result, params, state);
				if (!patch) return result;
				pi.appendEntry?.(CUSTOM_STATE, state.counters);
				return {
					...result,
					...patch,
					details: { ...(result?.details || {}), ...(patch.details || {}) },
				};
			} finally {
				state.busyPrunes = Math.max(0, state.busyPrunes - 1);
			}
		},
	};
	for (const key of [
		"promptSnippet",
		"promptGuidelines",
		"prepareArguments",
		"renderCall",
		"renderResult",
		"renderShell",
	]) {
		if (template[key] !== undefined) definition[key] = template[key];
	}
	pi.registerTool(definition);
	return true;
}

export function withRequiredFocusQuestion(parameters) {
	const base = parameters && typeof parameters === "object" ? parameters : {};
	const required = Array.isArray(base.required) ? base.required : [];
	return {
		...base,
		type: base.type || "object",
		properties: {
			...(base.properties || {}),
			context_focus_question: {
				type: "string",
				description:
					"Required. A complete, self-contained question describing your current " +
					"information need — what you want to learn from this output. Needle " +
					"prunes the observation to the lines relevant to it.",
			},
			verbatim: {
				type: "boolean",
				description:
					"Set true only when you need the exact, unpruned output, such as a ranged " +
					"read before editing. Needle returns the native output unchanged.",
			},
		},
		required: required.includes("context_focus_question")
			? required
			: [...required, "context_focus_question"],
	};
}

function stripNeedleParams(params) {
	if (!params || typeof params !== "object") return params;
	const { context_focus_question: _focus, verbatim: _verbatim, ...rest } = params;
	return rest;
}

export async function buildToolResultPatch(toolName, result, params, state) {
	const original = extractText(result?.content);
	if (!original || original.length < MIN_CHARS) return undefined;
	if (params?.verbatim === true) {
		return {
			details: { needle: { decision: "unchanged", reason: "verbatim" } },
		};
	}

	const query = typeof params?.context_focus_question === "string"
		? params.context_focus_question.trim()
		: "";
	if (!query) {
		// Schema drift or host quirk: required parameter missing. Loud, never
		// silent — the model (and the user scrolling the transcript) sees it.
		return banner(
			original,
			"needle: missing context_focus_question — output returned unpruned. " +
				"Provide a goal hint (see the needle-goal-hints skill).",
			{ decision: "unchanged", reason: "missing-focus-question" },
		);
	}

	// Block until the daemon is up and the model resident. Cold, loading,
	// or slow are not bypass reasons; genuine failure and critical memory
	// pressure are loud below.
	const enabled = await ensureEnabled(state).catch(() => false);
	if (!enabled) {
		return failureBanner(original, state.lastError || "needle could not start");
	}

	const { payload, envelope } = splitEnvelope(original);
	let response;
	try {
		response = await state.requestFn(
			"prune",
			{ session: state.sessionId, text: payload, query },
			{ timeoutMs: PRUNE_TIMEOUT_MS },
		);
	} catch (error) {
		state.backendStatus = "failed";
		state.lastError = String(error?.message || error);
		return failureBanner(original, state.lastError);
	}
	if (!response?.ok) {
		state.backendStatus = response?.backend_status || "failed";
		state.lastError = response?.error || "prune failed";
		return failureBanner(original, state.lastError);
	}

	state.backendStatus = response.backend_status || "resident";
	state.lastError = null;
	if (response.decision !== "pruned") {
		return {
			details: { needle: { decision: "unchanged", reason: response.reason || "model" } },
		};
	}
	const pruned = String(response.text ?? "") + envelope;
	record(state.counters, original.length, pruned.length, toolName);
	return {
		content: [{ type: "text", text: pruned }],
		details: {
			needle: {
				decision: "pruned",
				reason: response.reason || "model",
				originalChars: original.length,
				prunedChars: pruned.length,
			},
		},
	};
}

async function pruneThrownToolError(toolName, error, params, state) {
	const original = String(error?.message || error || "");
	const result = { content: [{ type: "text", text: original }] };
	const patch = await buildToolResultPatch(toolName, result, params, state);
	if (!patch?.content) return error;
	const pruned = extractText(patch.content);
	if (!pruned) return error;
	const wrapped = new Error(pruned);
	wrapped.name = error?.name || "Error";
	if (error && typeof error === "object") {
		for (const key of ["code", "errno", "syscall", "path", "signal"]) {
			if (key in error) wrapped[key] = error[key];
		}
	}
	return wrapped;
}

function banner(original, message, needleDetails) {
	return {
		content: [{ type: "text", text: `[${message}]\n\n${original}` }],
		details: { needle: needleDetails },
	};
}

function failureBanner(original, error) {
	return banner(
		original,
		`needle failed: ${error} — original output follows. /needle off to disable pruning`,
		{ decision: "failed", reason: error },
	);
}

export function splitEnvelope(text) {
	const match = text.match(ENVELOPE_PATTERN);
	if (!match) return { payload: text, envelope: "" };
	return {
		payload: text.slice(0, match.index),
		envelope: text.slice(match.index),
	};
}

export function extractText(content) {
	if (!Array.isArray(content)) return "";
	const parts = [];
	for (const block of content) {
		if (block?.type !== "text" || typeof block.text !== "string") return "";
		parts.push(block.text);
	}
	return parts.join("\n");
}

// --- /needle command --------------------------------------------------------

function registerNeedleCommand(pi, state) {
	pi.registerCommand?.("needle", {
		description: "Needle status, recovery, and on/off",
		getArgumentCompletions: (prefix) => {
			const items = ["status", "original", "on", "off"].filter((item) => item.startsWith(prefix));
			return items.length ? items.map((value) => ({ value, label: value })) : null;
		},
		handler: async (args, ctx) => {
			const sub = String(args || "").trim() || "status";
			if (sub === "off") {
				state.needleOn = false;
				if (state.sessionId) {
					await state.requestFn("disable", { session: state.sessionId }).catch(() => undefined);
				}
				state.backendStatus = "cold";
				ctx.ui?.notify?.("needle off: tool output passes through untouched", "info");
				return;
			}
			if (sub === "on") {
				state.needleOn = true;
				state.enablePromise = null;
				const ok = await ensureEnabled(state);
				ctx.ui?.notify?.(
					ok ? "needle on: model resident" : `needle failed: ${state.lastError}`,
					ok ? "info" : "error",
				);
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

async function buildStatusMessage(state) {
	const lines = [];
	let daemon = null;
	try {
		daemon = await state.requestFn("status", {}, { timeoutMs: 1000 });
	} catch {
		daemon = null;
	}
	if (!state.needleOn) {
		lines.push("needle: off (this session) — /needle on to re-enable");
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
	lines.push(`socket: ${socketPath()}`);
	lines.push(`home: ${needleHome()}`);
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

// --- statusline --------------------------------------------------------------

function renderStatus(ctx, state) {
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
	if (visual === "off") return `${indicator} needle off`;
	if (visual === "failed") {
		return `${indicator} needle failed${SEP}/needle off to disable`;
	}
	const calls = Number(state.counters.calls || 0);
	const saved = Number(state.counters.savedChars || 0);
	const plural = calls === 1 ? "" : "s";
	const forms = [
		`needle${SEP}${formatCount(saved)} chars trimmed${SEP}${calls} prune${plural}`,
		`needle${SEP}${formatCount(saved)}c${SEP}${calls}p`,
		"needle",
	];
	const cols = Number(options.columns || process.env.COLUMNS || 80);
	for (const line of forms) {
		if (2 + line.length <= cols - 1) return `${indicator} ${line}`;
	}
	return `${indicator} needle`;
}

function formatIndicator(visual, tick) {
	if (visual === "loading" || visual === "busy") {
		return ansi(STATE_CODES[visual], SPIN_FRAMES[tick % SPIN_FRAMES.length]);
	}
	if (visual === "resident") {
		return ansi(STATE_CODES.resident, PULSE_FRAMES[tick % PULSE_FRAMES.length]);
	}
	if (visual === "failed") return ansi(STATE_CODES.failed, "✗");
	return ansi(STATE_CODES.off, "·");
}

// --- helpers -----------------------------------------------------------------

async function loadPiTools() {
	const mod = await importPiSdk();
	if (typeof mod.createReadTool !== "function") {
		throw new Error("Needle Pi extension requires createReadTool from @mariozechner/pi-coding-agent");
	}
	return {
		createReadTool: mod.createReadTool,
		createBashTool: typeof mod.createBashTool === "function" ? mod.createBashTool : undefined,
	};
}

async function importPiSdk() {
	try {
		return await import("@mariozechner/pi-coding-agent");
	} catch (error) {
		return importPiSdkFromCli(error);
	}
}

async function importPiSdkFromCli(cause) {
	const candidates = [];
	if (process.argv[1]) candidates.push(process.argv[1]);
	try {
		const piPath = execFileSync("which", ["pi"], { encoding: "utf8" }).trim();
		if (piPath) candidates.push(piPath);
	} catch {
		// Fall through to the clearer package import error below.
	}
	for (const candidate of candidates) {
		try {
			const real = realpathSync(candidate);
			const indexPath = resolve(dirname(dirname(real)), "dist/index.js");
			if (existsSync(indexPath)) return import(pathToFileURL(indexPath).href);
		} catch {
			// Try the next candidate.
		}
	}
	throw cause;
}

function emptyCounters() {
	return { calls: 0, originalChars: 0, prunedChars: 0, savedChars: 0 };
}

function restoreCounters(entries) {
	const counters = emptyCounters();
	try {
		for (const entry of entries) {
			if (entry.type === "custom" && entry.customType === CUSTOM_STATE && entry.data) {
				Object.assign(counters, entry.data);
			}
		}
	} catch {
		// Fresh sessions have nothing to restore.
	}
	return counters;
}

function record(counters, originalLen, prunedLen, tool) {
	counters.calls += 1;
	counters.originalChars += originalLen;
	counters.prunedChars += prunedLen;
	counters.savedChars = counters.originalChars - counters.prunedChars;
	counters.lastTool = tool;
	counters.updatedAt = Date.now();
}

function envSecs(name, fallback) {
	const value = Number.parseFloat(process.env[name] ?? "");
	return Number.isFinite(value) && value > 0 ? value : fallback;
}

function ansi(code, text) {
	return `\x1b[${code}m${text}\x1b[0m`;
}

function formatCount(n) {
	if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
	if (n >= 10_000) return `${Math.round(n / 1_000)}k`;
	if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
	return String(n);
}
