import { execFileSync } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import {
	acquireLease,
	appHome,
	codeVersion,
	ensureManager,
	heartbeat,
	managerSocketPath,
	packageInventory,
	pathFromModuleUrl,
	prune,
	release,
	repoRootFromModuleUrl,
	sourceIdentity,
	stats,
	tailEvents,
} from "./client.mjs";

const READ_TOOL = "read";
const BASH_TOOL = "bash";
const PRUNABLE_TOOLS = new Set([READ_TOOL, BASH_TOOL]);
const MIN_CHARS = Number.parseInt(process.env.HAY_MIN_CHARS || "200", 10);
const MIN_RATIO = Number.parseFloat(process.env.HAY_MIN_SAVINGS_RATIO || "0.10");
const PRUNE_TIMEOUT_MS = envMs("HAY_PI_PRUNE_TIMEOUT_SECS", 180);
const HEARTBEAT_MS = Number.parseFloat(process.env.HAY_HEARTBEAT_INTERVAL || "30") * 1000;
const STATUS_MS = envMs("HAY_PI_STATUS_INTERVAL_SECS", 0.4);
const STATUS_POLL_MS = envMs("HAY_PI_STATUS_POLL_SECS", 1);
const ANIMATION_MS = envMs("HAY_PI_ANIMATION_INTERVAL_SECS", 0.4);
const ACTIVE_MS = Number.parseFloat(process.env.HAY_PI_ACTIVE_SECS || "3") * 1000;
const CUSTOM_STATE = "needle-state";
const SEP = " · ";
const SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const PULSE_FRAMES = ["⠤", "⠶", "⠿", "⠶"];
const INTENSITY_CODES = ["2", "", "1", ""];
const STATE_CODES = {
	down: "38;5;240",
	cold: "38;5;67",
	loading: "38;5;179",
	degraded: "38;5;196",
	ready: "38;5;35",
	active: "38;5;87",
};
const PRESSURE = new Map([
	[1, "normal"],
	[2, "warning"],
	[4, "critical"],
]);

export default async function hayPiExtension(pi) {
	return installHayPiExtension(pi, await loadPiTools());
}

export function installHayPiExtension(pi, options = {}) {
	const repoRoot = repoRootFromModuleUrl(import.meta.url);
	const extensionPath = pathFromModuleUrl(import.meta.url);
	const counters = emptyCounters();
	const statusCache = { snapshot: null, checkedAt: 0, pending: null, busyPrunes: 0 };
	let sessionId = "";
	let heartbeatTimer;
	let statusTimer;

	registerNeedleCommand(pi, counters, { repoRoot, extensionPath });
	registerReadOverride(pi, counters, options.createReadTool, statusCache);
	registerBashOverride(pi, counters, options.createBashTool, statusCache);

	pi.on("session_start", async (_event, ctx) => {
		sessionId = ctx.sessionManager?.getSessionId?.() || `pi-${Date.now()}`;
		Object.assign(counters, restoreCounters(ctx.sessionManager?.getEntries?.() || []));
		const version = await codeVersion(repoRoot);
		const ready = await ensureManager({ repoRoot });
		if (ready) await acquireLease(sessionId, version, { repoRoot });
		await updateStatus(ctx, counters, statusCache, { force: true });
		heartbeatTimer = setInterval(() => {
			if (sessionId) heartbeat(sessionId).catch(() => undefined);
		}, HEARTBEAT_MS);
		statusTimer = setInterval(() => {
			updateStatus(ctx, counters, statusCache).catch(() => undefined);
		}, STATUS_MS);
	});

	pi.on("session_shutdown", async () => {
		if (heartbeatTimer) clearInterval(heartbeatTimer);
		if (statusTimer) clearInterval(statusTimer);
		if (sessionId) await release(sessionId).catch(() => undefined);
	});
}

async function loadPiTools() {
	const mod = await importPiSdk();
	if (typeof mod.createReadTool !== "function") {
		throw new Error("Hay Pi extension requires createReadTool from @mariozechner/pi-coding-agent");
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

function registerReadOverride(pi, counters, createReadTool, statusCache) {
	return registerNativeToolOverride(pi, counters, createReadTool, statusCache, READ_TOOL);
}

function registerBashOverride(pi, counters, createBashTool, statusCache) {
	return registerNativeToolOverride(pi, counters, createBashTool, statusCache, BASH_TOOL);
}

function registerNativeToolOverride(pi, counters, createTool, statusCache, toolName) {
	if (typeof pi.registerTool !== "function" || typeof createTool !== "function") return false;
	const template = createTool(process.cwd());
	const definition = {
		name: toolName,
		label: template.label || toolName,
		description: template.description,
		parameters: withFocusQuestionParameter(template.parameters),
		async execute(toolCallId, params, signal, onUpdate, ctx) {
			const nativeTool = createTool(ctx?.cwd || process.cwd());
			const result = await nativeTool.execute(toolCallId, stripNeedleParams(params), signal, onUpdate, ctx);
			statusCache.busyPrunes += 1;
			await updateStatus(ctx, counters, statusCache);
			let patch;
			try {
				patch = await buildNativeToolResultPatch(toolName, result, params, ctx, counters, (text, query) =>
					prune(text, query, { signal, timeoutMs: PRUNE_TIMEOUT_MS }),
				);
			} finally {
				statusCache.busyPrunes = Math.max(0, statusCache.busyPrunes - 1);
			}
			await updateStatus(ctx, counters, statusCache, { force: true });
			if (!patch) return result;
			pi.appendEntry?.(CUSTOM_STATE, counters);
			return {
				...result,
				...patch,
				details: { ...(result?.details || {}), ...(patch.details || {}) },
			};
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

function withFocusQuestionParameter(parameters) {
	const base = parameters && typeof parameters === "object" ? parameters : {};
	return {
		...base,
		type: base.type || "object",
		properties: {
			...(base.properties || {}),
			context_focus_question: {
				type: "string",
				description: "Optional task focus for Needle pruning. Omit to return the original output unchanged.",
			},
		},
	};
}

function stripNeedleParams(params) {
	if (!params || typeof params !== "object" || !("context_focus_question" in params)) return params;
	const { context_focus_question: _focus, ...rest } = params;
	return rest;
}

export async function buildReadResultPatch(result, params, ctx, counters, pruneFn) {
	return buildNativeToolResultPatch(READ_TOOL, result, params, ctx, counters, pruneFn);
}

export async function buildBashResultPatch(result, params, ctx, counters, pruneFn) {
	return buildNativeToolResultPatch(BASH_TOOL, result, params, ctx, counters, pruneFn);
}

async function buildNativeToolResultPatch(toolName, result, params, ctx, counters, pruneFn) {
	return buildToolResultPatch({ toolName, params, ...result }, ctx, counters, pruneFn);
}

export async function buildToolResultPatch(event, ctx, counters, pruneFn) {
	if (!PRUNABLE_TOOLS.has(event.toolName)) return undefined;
	const original = extractText(event.content);
	if (!original || original.length < MIN_CHARS) return undefined;
	const query = extractFocusQuestion(event.params);
	if (!query) return undefined;
	let resp;
	try {
		resp = await pruneFn(original, query);
	} catch {
		return undefined;
	}
	if (!resp?.ok) return undefined;
	const pruned = String(resp.text ?? "");
	const saved = original.length - pruned.length;
	if (saved <= 0 || saved / original.length < MIN_RATIO) return undefined;
	record(counters, original.length, pruned.length, event.toolName);
	return { content: [{ type: "text", text: pruned }] };
}

export function extractFocusQuestion(params) {
	const value = params?.context_focus_question;
	return typeof value === "string" ? value.trim() : "";
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

async function updateStatus(ctx, counters, cache = undefined, options = {}) {
	const now = Date.now();
	let snapshot = cache?.snapshot ?? null;
	if (options.force || shouldPollStatus(cache, now)) {
		snapshot = await refreshStatus(cache);
	} else if (cache?.pending) {
		snapshot = cache.snapshot ?? "loading";
	}
	const previousResident = Boolean(cache?.snapshot?.ok && cache.snapshot.resident);
	const managerBusy = snapshot === "loading" || Boolean(cache?.pending);
	const busy = Number(cache?.busyPrunes || 0) > 0 || (managerBusy && previousResident);
	if (snapshot === "loading" && previousResident) snapshot = cache.snapshot;
	ctx.ui?.setStatus?.("needle", formatStatus(snapshot, counters, ctx.ui.theme, { nowMs: now, busy }));
}

function shouldPollStatus(cache, now) {
	if (!cache) return true;
	if (cache.pending) return false;
	return now - cache.checkedAt >= STATUS_POLL_MS;
}

async function refreshStatus(cache) {
	const read = readStatusSnapshot();
	if (!cache) return read;
	cache.pending = read;
	try {
		const snapshot = await read;
		cache.checkedAt = Date.now();
		if (snapshot !== "loading") cache.snapshot = snapshot;
		return snapshot;
	} finally {
		cache.pending = null;
	}
}

async function readStatusSnapshot() {
	try {
		return await stats({ timeoutMs: 250 });
	} catch (err) {
		// A serial manager cannot answer while cold-loading or pruning.
		return err?.message === "timeout" ? "loading" : null;
	}
}

export function decideStatusState(snapshot, counters = {}, options = {}) {
	if (snapshot === null || snapshot === undefined) return "down";
	if (snapshot === "loading") return "loading";
	if (typeof snapshot !== "object" || !snapshot.ok) return "down";
	if (!snapshot.resident) return "cold";
	const backend = snapshot.backend || "?";
	if (typeof backend === "string" && backend.startsWith("fake (")) {
		return "degraded";
	}
	if (options.busy) return "active";
	const now = options.nowMs ?? Date.now();
	const updatedAt = Number(counters.updatedAt || 0);
	const recent = Number.isFinite(updatedAt) && updatedAt > 0 && now - updatedAt < ACTIVE_MS;
	return recent ? "active" : "ready";
}

export function formatIndicator(state, theme, options = {}) {
	const now = options.nowMs ?? Date.now();
	const tick = Math.floor(now / ANIMATION_MS);
	if (state === "loading") {
		return color(theme, STATE_CODES.loading, SPIN_FRAMES[tick % SPIN_FRAMES.length]);
	}
	if (state === "active") {
		return color(theme, STATE_CODES.active, SPIN_FRAMES[tick % SPIN_FRAMES.length]);
	}
	if (state === "ready") {
		return color(theme, STATE_CODES.ready, PULSE_FRAMES[tick % PULSE_FRAMES.length]);
	}
	if (state === "cold") {
		return breathe(theme, STATE_CODES.cold, "·", tick);
	}
	if (state === "degraded") {
		return breathe(theme, STATE_CODES.degraded, "✗", tick);
	}
	return breathe(theme, STATE_CODES.down, "-", tick);
}

export function formatStatus(snapshot, counters = {}, theme, options = {}) {
	const calls = Number(counters.calls || 0);
	const savedChars = Number(counters.savedChars || 0);
	const state = decideStatusState(snapshot, counters, options);
	const plural = calls === 1 ? "" : "s";
	const forms = [
		`needle${SEP}${formatCount(savedChars)} chars trimmed${SEP}${calls} prune${plural}`,
		`needle${SEP}${formatCount(savedChars)}c${SEP}${calls}p`,
		"needle",
	];
	const cols = Number(options.columns || process.env.COLUMNS || 80);
	const indicator = formatIndicator(state, theme, options);
	for (const line of forms) {
		if (2 + line.length <= cols - 1) {
			return `${indicator} ${line}`;
		}
	}
	return `${indicator} needle`;
}

function registerNeedleCommand(pi, counters, runtime) {
	for (const command of ["needle", "hay"]) {
		pi.registerCommand?.(command, {
			description: command === "needle" ? "Show Needle runtime status" : "Alias for /needle",
			getArgumentCompletions: (prefix) => {
				const items = ["status", "doctor", "events", "packages"].filter((item) => item.startsWith(prefix));
				return items.length ? items.map((value) => ({ value, label: value })) : null;
			},
			handler: async (args, ctx) => {
				const parsed = parseNeedleArgs(args);
				if (!parsed.ok) {
					ctx.ui?.notify?.(`Usage: /${command} [status|doctor|events|packages] [count]`, "warning");
					return;
				}
				const content =
					parsed.mode === "packages"
						? await buildPackageStatus(runtime.repoRoot)
						: await buildOperatorStatus(counters, {
								events: parsed.events,
								includeSource: parsed.mode === "doctor",
								...runtime,
							});
				if (typeof pi.sendMessage === "function") {
					pi.sendMessage({
						customType: "needle-status",
						content,
						display: true,
						details: { events: parsed.events, mode: parsed.mode },
					});
				} else {
					ctx.ui?.notify?.(content, "info");
				}
			},
		});
	}
}

function parseNeedleArgs(args) {
	const parts = String(args || "").trim().split(/\s+/).filter(Boolean);
	const sub = parts[0] || "status";
	if (!["status", "doctor", "events", "packages"].includes(sub)) return { ok: false };
	const fallback = sub === "doctor" ? 20 : 12;
	const rawCount = parts[1];
	const events = rawCount === undefined ? fallback : Number.parseInt(rawCount, 10);
	return { ok: true, mode: sub, events: Number.isFinite(events) && events >= 0 ? events : fallback };
}

export async function buildPackageStatus(repoRoot = repoRootFromModuleUrl(import.meta.url)) {
	const packages = await packageInventory(repoRoot, { hostBinding: "pi/native-tools" });
	return renderPackageStatus(packages);
}

export function renderPackageStatus(packages) {
	const lines = ["needle packages:"];
	if (!packages.length) {
		lines.push("  none found");
		return lines.join("\n");
	}
	for (const pkg of packages) {
		const marker = pkg.active ? "*" : "-";
		const capability = pkg.capabilities?.length ? pkg.capabilities.join(", ") : "?";
		const backend = pkg.backend || "?";
		const repair = pkg.capabilities?.includes("e24z/soft-lamr") ? "python AST repair" : "no AST repair";
		lines.push(`  ${marker} ${pkg.id}`);
		lines.push(`      capability ${capability}`);
		lines.push(`      backend ${backend} | ${repair}`);
	}
	lines.push("");
	lines.push("select package for its host binding:");
	lines.push("  needle package use <package-id>");
	lines.push("one-run override:");
	lines.push("  NEEDLE_PACKAGE=<package-id> pi");
	lines.push("restart the resident Needle runtime if it is already running.");
	return lines.join("\n");
}

export async function buildOperatorStatus(counters = {}, options = {}) {
	let snapshot = null;
	try {
		snapshot = await stats({ timeoutMs: options.timeoutMs ?? 500 });
	} catch (err) {
		snapshot = err?.message === "timeout" ? "loading" : null;
	}
	const recent = await tailEvents(options.events ?? 12);
	const source = options.includeSource
		? await sourceIdentity(options.repoRoot || repoRootFromModuleUrl(import.meta.url))
		: null;
	return renderOperatorStatus(snapshot, recent, counters, {
		appHome: appHome(),
		extensionPath: options.extensionPath,
		socketPath: managerSocketPath(),
		source,
	});
}

export function renderOperatorStatus(snapshot, recent = [], counters = {}, options = {}) {
	const lines = [];
	if (snapshot === "loading") {
		lines.push("needle runtime: loading or pruning (socket busy)");
	} else if (!snapshot?.ok) {
		lines.push("needle runtime: down (not running)");
	} else {
		const backend = snapshot.backend;
		let state;
		if (!snapshot.resident) {
			state = "cold (model not loaded)";
		} else if (typeof backend === "string" && backend.startsWith("fake (")) {
			state = `DEGRADED (${backend})`;
		} else {
			state = `ready (${backend || "unknown"} resident)`;
		}
		lines.push(`needle runtime: ${state}`);
		lines.push(
			`  sessions ${snapshot.sessions ?? 0}` +
				`  |  version ${String(snapshot.version || "").slice(0, 12) || "?"}` +
				`  |  pressure ${PRESSURE.get(snapshot.pressure) || "?"}` +
				`  |  free ${formatMb(snapshot.available_mb)}`,
		);
	}
	lines.push(`  socket ${options.socketPath || managerSocketPath()}`);
	lines.push(`  home ${options.appHome || appHome()}`);
	lines.push(
		`  this Pi session ${formatCount(counters.savedChars || 0)} chars trimmed` +
			`  |  ${counters.calls || 0} prunes` +
			(counters.lastTool ? `  |  last tool ${counters.lastTool}` : ""),
	);
	if (options.source || options.extensionPath) {
		lines.push("");
		lines.push("source:");
		for (const line of renderSource(options.source, options.extensionPath)) {
			lines.push(`  ${line}`);
		}
	}
	lines.push("");
	lines.push("why running:");
	lines.push(`  ${whyRunning(snapshot)}`);
	if (recent.length) {
		lines.push("");
		lines.push("recent events:");
		for (const event of recent) {
			lines.push(`  ${formatEvent(event)}`);
		}
	}
	return lines.join("\n");
}

function renderSource(source, extensionPath) {
	const lines = [];
	if (extensionPath) lines.push(`extension ${extensionPath}`);
	if (!source) return lines;
	lines.push(`package root ${source.repoRoot}`);
	if (source.modelRoot) lines.push(`model dir ${source.modelRoot}`);
	if (source.activePackage) {
		for (const line of renderPackageIdentity(source.activePackage)) {
			lines.push(line);
		}
	}
	const versions = [
		source.packageVersion ? `package ${source.packageName || "package"}@${source.packageVersion}` : null,
		source.pyprojectVersion ? `pyproject ${source.pyprojectVersion}` : null,
	].filter(Boolean);
	if (versions.length) lines.push(`version ${versions.join(" | ")}`);
	if (source.git?.available) {
		lines.push(
			`git ${source.git.branch || "unknown"}@${source.git.commit || "unknown"}` +
				` (${source.git.dirty ? `dirty, ${source.git.dirtyFiles} files` : "clean"})`,
		);
	} else {
		lines.push(`git ${source.git?.reason || "not available"}`);
	}
	return lines;
}

function renderPackageIdentity(activePackage) {
	if (!activePackage.available) {
		return [`active package ${activePackage.id || "?"} unavailable (${activePackage.reason || "unknown reason"})`];
	}
	const lines = [`active package ${activePackage.id}`];
	if (activePackage.capabilities?.length) lines.push(`capability ${activePackage.capabilities.join(", ")}`);
	if (activePackage.backend) lines.push(`backend ${activePackage.backend}`);
	if (activePackage.hostBinding) lines.push(`host binding ${activePackage.hostBinding}`);
	if (activePackage.compute || activePackage.privacy) {
		lines.push(`compute ${activePackage.compute || "?"} | privacy ${activePackage.privacy || "?"}`);
	}
	if (activePackage.promptBundle) lines.push(`prompt bundle ${activePackage.promptBundle}`);
	if (activePackage.packageCard) lines.push(`package card ${activePackage.packageCard}`);
	if (activePackage.claimCard) lines.push(`claim card ${activePackage.claimCard}`);
	return lines;
}

function whyRunning(snapshot) {
	if (snapshot === "loading") {
		return "a prune or model cold-load is in progress; the serial manager is busy.";
	}
	if (!snapshot?.ok) {
		return "no manager is listening; the Pi adapter fails open and leaves tool output unchanged.";
	}
	if ((snapshot.sessions || 0) > 0) {
		return "one or more agent sessions hold leases, so the manager keeps coordinating pruning.";
	}
	if (snapshot.resident) {
		return "the model is resident until idle eviction or memory pressure asks it to unload.";
	}
	return "the manager is available, but the model lazy-loads only when a prunable tool result arrives.";
}

function formatEvent(event) {
	const time = formatTime(event?.ts);
	const name = String(event?.event || "?").padEnd(16, " ");
	const extra = Object.entries(event || {})
		.filter(([key]) => key !== "ts" && key !== "event")
		.map(([key, value]) => `${key}=${value}`)
		.join(" ");
	return `${time}  ${name} ${extra}`.trimEnd();
}

function formatTime(value) {
	const date = new Date(Number(value) * 1000);
	if (!Number.isFinite(date.getTime())) return "--:--:--";
	return date.toLocaleTimeString(undefined, { hour12: false });
}

function formatMb(value) {
	if (typeof value !== "number" || !Number.isFinite(value)) return "?";
	return `${(value / 1024).toFixed(1)} GB`;
}

function envMs(name, defaultSecs) {
	const secs = Number.parseFloat(process.env[name] || "");
	return (Number.isFinite(secs) && secs > 0 ? secs : defaultSecs) * 1000;
}

function color(_theme, code, text) {
	return ansi(code, text);
}

function ansi(code, text) {
	return `\x1b[${code}m${text}\x1b[0m`;
}

function breathe(theme, code, glyph, tick) {
	const intensity = INTENSITY_CODES[tick % INTENSITY_CODES.length];
	return color(theme, intensity ? `${code};${intensity}` : code, glyph);
}

function formatCount(n) {
	if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
	if (n >= 10_000) return `${Math.round(n / 1_000)}k`;
	if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
	return String(n);
}
