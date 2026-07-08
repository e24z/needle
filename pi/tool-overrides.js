import { MIN_CHARS, PRUNE_TIMEOUT_MS } from "./config.js";
import { ensureEnabled } from "./daemon.js";
import { persistState, record } from "./state.js";

/// Trailing host envelope: Pi appends bracketed notices like
/// "[Showing lines 1-2000 of 5000. Full output: /tmp/...]" and bash failure
/// status lines after a blank line. Those never reach the model pruner and are
/// reattached verbatim.
const ENVELOPE_PATTERN = /(?:\n+(?:\[[^\n]*\]|Command (?:exited with code \d+|timed out after [^\n]+ seconds|aborted)))+\s*$/;

export function registerOverride(pi, state, createTool, toolName) {
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
				persistState(pi, state);
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
					"information need: what you want to learn from this output. Needle " +
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
		// Schema drift or host quirk: required parameter missing. Return the
		// original output with a banner the model and user can see.
		return banner(
			original,
			"needle: missing context_focus_question; output returned unpruned. " +
				"Provide a goal hint (see the needle-goal-hints skill).",
			{ decision: "unchanged", reason: "missing-focus-question" },
		);
	}

	// Block until the daemon is up and the model resident. Cold starts and
	// slow loads do not bypass pruning; failures return banners below.
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
	record(state.counters, original, pruned, toolName, state);
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

function stripNeedleParams(params) {
	if (!params || typeof params !== "object") return params;
	const { context_focus_question: _focus, verbatim: _verbatim, ...rest } = params;
	return rest;
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
		`needle failed: ${error}; original output follows. /needle off to disable pruning`,
		{ decision: "failed", reason: error },
	);
}
