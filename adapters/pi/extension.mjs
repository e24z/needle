import {
	acquireLease,
	codeVersion,
	ensureManager,
	heartbeat,
	prune,
	release,
	repoRootFromModuleUrl,
	stats,
} from "./client.mjs";

const TARGET_TOOLS = new Set(["read", "grep", "find"]);
const MIN_CHARS = Number.parseInt(process.env.HAY_MIN_CHARS || "200", 10);
const MIN_RATIO = Number.parseFloat(process.env.HAY_MIN_SAVINGS_RATIO || "0.10");
const HEARTBEAT_MS = Number.parseFloat(process.env.HAY_HEARTBEAT_INTERVAL || "30") * 1000;
const STATUS_MS = Number.parseFloat(process.env.HAY_PI_STATUS_INTERVAL || "1") * 1000;
const CUSTOM_STATE = "hay-state";

export default function hayPiExtension(pi) {
	const repoRoot = repoRootFromModuleUrl(import.meta.url);
	const counters = emptyCounters();
	let sessionId = "";
	let heartbeatTimer;
	let statusTimer;

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
	} catch {
		// manager down or busy: status formatter handles it.
	}
	ctx.ui?.setStatus?.("hay", formatStatus(snapshot, counters, ctx.ui.theme));
}

export function formatStatus(snapshot, counters = {}, theme) {
	const saved = formatTokens(Math.floor((counters.savedChars || 0) / 4));
	const suffix = `${saved}t ${counters.calls || 0}p`;
	if (!snapshot?.ok) return color(theme, "muted", `hay down ${suffix}`);
	if (!snapshot.resident) return color(theme, "dim", `hay cold ${suffix}`);
	const backend = snapshot.backend || "?";
	if (typeof backend === "string" && backend.startsWith("fake (")) {
		return color(theme, "error", `hay degraded ${suffix}`);
	}
	return color(theme, "success", `hay ready ${suffix}`);
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
