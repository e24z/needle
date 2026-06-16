import {
	acquireLease,
	appHome,
	codeVersion,
	ensureManager,
	heartbeat,
	managerSocketPath,
	pathFromModuleUrl,
	prune,
	release,
	repoRootFromModuleUrl,
	sourceIdentity,
	stats,
	tailEvents,
} from "./client.mjs";

const TARGET_TOOLS = new Set(["read", "grep", "find"]);
const MIN_CHARS = Number.parseInt(process.env.HAY_MIN_CHARS || "200", 10);
const MIN_RATIO = Number.parseFloat(process.env.HAY_MIN_SAVINGS_RATIO || "0.10");
const HEARTBEAT_MS = Number.parseFloat(process.env.HAY_HEARTBEAT_INTERVAL || "30") * 1000;
const STATUS_MS = Number.parseFloat(process.env.HAY_PI_STATUS_INTERVAL || "1") * 1000;
const CUSTOM_STATE = "hay-state";
const PRESSURE = new Map([
	[1, "normal"],
	[2, "warning"],
	[4, "critical"],
]);

export default function hayPiExtension(pi) {
	const repoRoot = repoRootFromModuleUrl(import.meta.url);
	const extensionPath = pathFromModuleUrl(import.meta.url);
	const counters = emptyCounters();
	let sessionId = "";
	let heartbeatTimer;
	let statusTimer;

	registerHayCommand(pi, counters, { repoRoot, extensionPath });

	pi.on("session_start", async (_event, ctx) => {
		sessionId = ctx.sessionManager?.getSessionId?.() || `pi-${Date.now()}`;
		Object.assign(counters, restoreCounters(ctx.sessionManager?.getEntries?.() || []));
		const version = await codeVersion(repoRoot);
		const ready = await ensureManager({ repoRoot });
		if (ready) await acquireLease(sessionId, version, { repoRoot });
		await updateStatus(ctx, counters);
		heartbeatTimer = setInterval(() => {
			if (sessionId) heartbeat(sessionId).catch(() => undefined);
		}, HEARTBEAT_MS);
		statusTimer = setInterval(() => {
			updateStatus(ctx, counters).catch(() => undefined);
		}, STATUS_MS);
	});

	pi.on("tool_result", async (event, ctx) => {
		const patch = await buildToolResultPatch(event, ctx, counters, (text, query) =>
			prune(text, query, { signal: ctx.signal }),
		);
		if (patch) {
			pi.appendEntry?.(CUSTOM_STATE, counters);
			await updateStatus(ctx, counters);
		}
		return patch;
	});

	pi.on("session_shutdown", async () => {
		if (heartbeatTimer) clearInterval(heartbeatTimer);
		if (statusTimer) clearInterval(statusTimer);
		if (sessionId) await release(sessionId).catch(() => undefined);
	});
}

export async function buildToolResultPatch(event, ctx, counters, pruneFn) {
	if (!TARGET_TOOLS.has(event.toolName)) return undefined;
	const original = extractText(event.content);
	if (!original || original.length < MIN_CHARS) return undefined;
	const query = extractQuery(ctx);
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

export function extractText(content) {
	if (!Array.isArray(content)) return "";
	const parts = [];
	for (const block of content) {
		if (block?.type !== "text" || typeof block.text !== "string") return "";
		parts.push(block.text);
	}
	return parts.join("\n");
}

export function extractQuery(ctx) {
	const entries = ctx?.sessionManager?.getEntries?.() || [];
	for (let i = entries.length - 1; i >= 0; i--) {
		const entry = entries[i];
		if (entry?.type !== "message") continue;
		const msg = entry.message;
		if (msg?.role !== "assistant") continue;
		const text = messageText(msg);
		if (text) return text;
	}
	return "";
}

function messageText(msg) {
	if (typeof msg.content === "string") return msg.content.trim();
	if (Array.isArray(msg.content)) {
		return msg.content
			.filter((block) => block?.type === "text" && typeof block.text === "string")
			.map((block) => block.text)
			.join("\n")
			.trim();
	}
	return "";
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

async function updateStatus(ctx, counters) {
	let snapshot = null;
	try {
		snapshot = await stats({ timeoutMs: 250 });
	} catch (err) {
		// A serial manager cannot answer while cold-loading or pruning.
		snapshot = err?.message === "timeout" ? "loading" : null;
	}
	ctx.ui?.setStatus?.("hay", formatStatus(snapshot, counters, ctx.ui.theme));
}

export function formatStatus(snapshot, counters = {}, theme) {
	const saved = formatTokens(Math.floor((counters.savedChars || 0) / 4));
	const suffix = `${saved}t ${counters.calls || 0}p`;
	if (snapshot === "loading") return color(theme, "warning", `hay loading ${suffix}`);
	if (!snapshot?.ok) return color(theme, "muted", `hay down ${suffix}`);
	if (!snapshot.resident) return color(theme, "dim", `hay cold ${suffix}`);
	const backend = snapshot.backend || "?";
	if (typeof backend === "string" && backend.startsWith("fake (")) {
		return color(theme, "error", `hay degraded ${suffix}`);
	}
	return color(theme, "success", `hay ready ${suffix}`);
}

function registerHayCommand(pi, counters, runtime) {
	pi.registerCommand?.("hay", {
		description: "Show Hay manager status",
		getArgumentCompletions: (prefix) => {
			const items = ["status", "doctor", "events"].filter((item) => item.startsWith(prefix));
			return items.length ? items.map((value) => ({ value, label: value })) : null;
		},
		handler: async (args, ctx) => {
			const parsed = parseHayArgs(args);
			if (!parsed.ok) {
				ctx.ui?.notify?.("Usage: /hay [status|doctor|events] [count]", "warning");
				return;
			}
			const content = await buildOperatorStatus(counters, {
				events: parsed.events,
				includeSource: parsed.mode === "doctor",
				...runtime,
			});
			if (typeof pi.sendMessage === "function") {
				pi.sendMessage({
					customType: "hay-status",
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

function parseHayArgs(args) {
	const parts = String(args || "").trim().split(/\s+/).filter(Boolean);
	const sub = parts[0] || "status";
	if (!["status", "doctor", "events"].includes(sub)) return { ok: false };
	const fallback = sub === "doctor" ? 20 : 12;
	const rawCount = parts[1];
	const events = rawCount === undefined ? fallback : Number.parseInt(rawCount, 10);
	return { ok: true, mode: sub, events: Number.isFinite(events) && events >= 0 ? events : fallback };
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
		lines.push("hay manager: loading or pruning (socket busy)");
	} else if (!snapshot?.ok) {
		lines.push("hay manager: down (not running)");
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
		lines.push(`hay manager: ${state}`);
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
		`  this Pi session ${formatTokens(Math.floor((counters.savedChars || 0) / 4))} tokens saved` +
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

function color(theme, name, text) {
	return theme?.fg ? theme.fg(name, text) : text;
}

function formatTokens(n) {
	if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
	if (n >= 10_000) return `${Math.round(n / 1_000)}k`;
	if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
	return String(n);
}
