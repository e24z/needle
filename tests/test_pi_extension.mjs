// Pi extension behaviors: visible prunes, loud missing-focus/failure paths,
// envelope protection, required schema, statusline states.
//
// Run: node tests/test_pi_extension.mjs

import assert from "node:assert/strict";

import {
	decideStatusState,
	extractText,
	formatStatus,
	installNeedlePiExtension,
	splitEnvelope,
	withRequiredFocusQuestion,
} from "../pi/extension.js";

const BIG_TEXT = "keep drop\n".repeat(40).trim(); // ~400 chars, above MIN_CHARS
const ENVELOPE = "\n\n[Showing lines 1-40 of 400. Full output: /tmp/pi-bash-x]";

function fakePi() {
	const pi = {
		tools: new Map(),
		handlers: new Map(),
		commands: new Map(),
		entries: [],
		registerTool(definition) {
			this.tools.set(definition.name, definition);
		},
		registerCommand(name, definition) {
			this.commands.set(name, definition);
		},
		on(event, handler) {
			this.handlers.set(event, handler);
		},
		appendEntry(type, data) {
			this.entries.push({ type, data });
		},
		sendMessage() {},
	};
	return pi;
}

function fakeToolFactory(text) {
	return () => ({
		label: "fake",
		description: "fake tool",
		parameters: {
			type: "object",
			properties: { path: { type: "string" } },
			required: ["path"],
		},
		execute: async () => ({ content: [{ type: "text", text }] }),
	});
}

function recordingRequestFn({ pruneResponse, pruneError } = {}) {
	const calls = [];
	const fn = async (op, fields = {}) => {
		calls.push({ op, ...fields });
		if (op === "prune") {
			if (pruneError) throw pruneError;
			return (
				pruneResponse ?? {
					ok: true,
					backend_status: "resident",
					decision: "pruned",
					reason: "model",
					text: fields.text.replaceAll(" drop", ""),
					stats: {},
				}
			);
		}
		if (op === "enable") return { ok: true, backend_status: "resident" };
		if (op === "status") return { ok: true, mode: "on", backend_status: "resident", sessions: 1 };
		return { ok: true };
	};
	fn.calls = calls;
	return fn;
}

async function installAndStart(text, requestFn) {
	const pi = fakePi();
	installNeedlePiExtension(pi, {
		createReadTool: fakeToolFactory(text),
		createBashTool: fakeToolFactory(text),
		requestFn,
		ensureDaemonFn: async () => true,
	});
	const ctx = {
		sessionManager: { getSessionId: () => "s-test", getEntries: () => [] },
		ui: { setStatus() {}, notify() {} },
	};
	await pi.handlers.get("session_start")(null, ctx);
	const stop = () => pi.handlers.get("session_shutdown")(null, ctx);
	return { pi, ctx, stop };
}

async function testSchemaRequiresFocusQuestion() {
	const requestFn = recordingRequestFn();
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		for (const name of ["read", "bash"]) {
			const parameters = pi.tools.get(name).parameters;
			assert.ok(parameters.properties.context_focus_question, `${name} has the parameter`);
			assert.ok(
				parameters.required.includes("context_focus_question"),
				`${name} schema requires context_focus_question`,
			);
			assert.ok(parameters.required.includes("path"), `${name} keeps native required params`);
		}
	} finally {
		await stop();
	}
}

async function testReadVisiblePrune() {
	const requestFn = recordingRequestFn();
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		const result = await pi.tools.get("read").execute(
			"t1",
			{ path: "x.py", context_focus_question: "which lines should be kept?" },
			null,
			null,
			{},
		);
		const text = extractText(result.content);
		assert.ok(!text.includes(" drop"), "pruned text dropped the noise");
		assert.equal(result.details.needle.decision, "pruned");
		const prune = requestFn.calls.find((call) => call.op === "prune");
		assert.equal(prune.session, "s-test");
		assert.equal(prune.query, "which lines should be kept?");
	} finally {
		await stop();
	}
}

async function testBashEnvelopeSurvivesPruning() {
	const requestFn = recordingRequestFn();
	const { pi, stop } = await installAndStart(BIG_TEXT + ENVELOPE, requestFn);
	try {
		const result = await pi.tools.get("bash").execute(
			"t1",
			{ path: "ls", context_focus_question: "what files exist?" },
			null,
			null,
			{},
		);
		const text = extractText(result.content);
		assert.ok(text.endsWith(ENVELOPE.trimStart() ? ENVELOPE : ""), "envelope reattached");
		assert.ok(text.includes("[Showing lines 1-40 of 400."), "truncation notice intact");
		const prune = requestFn.calls.find((call) => call.op === "prune");
		assert.ok(!prune.text.includes("[Showing lines"), "envelope never sent to the model");
	} finally {
		await stop();
	}
}

async function testMissingFocusQuestionIsLoud() {
	const requestFn = recordingRequestFn();
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		const result = await pi.tools.get("read").execute("t1", { path: "x.py" }, null, null, {});
		const text = extractText(result.content);
		assert.ok(
			text.startsWith("[needle: missing context_focus_question"),
			"loud banner on missing focus question",
		);
		assert.ok(text.includes(BIG_TEXT), "original output still present");
		assert.equal(result.details.needle.reason, "missing-focus-question");
		assert.ok(!requestFn.calls.some((call) => call.op === "prune"), "no prune attempted");
	} finally {
		await stop();
	}
}

async function testPruneFailureIsLoud() {
	const requestFn = recordingRequestFn({ pruneError: new Error("socket gone") });
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		const result = await pi.tools.get("bash").execute(
			"t1",
			{ path: "ls", context_focus_question: "what failed?" },
			null,
			null,
			{},
		);
		const text = extractText(result.content);
		assert.ok(text.startsWith("[needle failed: socket gone"), "loud failure banner");
		assert.ok(text.includes(BIG_TEXT), "original output still present");
		assert.equal(result.details.needle.decision, "failed");
	} finally {
		await stop();
	}
}

async function testToolCallBlocksOnSlowEnablement() {
	// Regression: the first tool call used to race daemon startup and fail
	// loudly instead of waiting. It must block on the shared enable promise.
	const requestFn = recordingRequestFn();
	const pi = fakePi();
	let daemonReady = false;
	installNeedlePiExtension(pi, {
		createReadTool: fakeToolFactory(BIG_TEXT),
		createBashTool: fakeToolFactory(BIG_TEXT),
		requestFn,
		ensureDaemonFn: async () => {
			await new Promise((resolve) => setTimeout(resolve, 150));
			daemonReady = true;
			return true;
		},
	});
	const ctx = {
		sessionManager: { getSessionId: () => "s-race", getEntries: () => [] },
		ui: { setStatus() {}, notify() {} },
	};
	await pi.handlers.get("session_start")(null, ctx);
	try {
		// Fire the tool call immediately: enablement is still in flight.
		const result = await pi.tools.get("read").execute(
			"t1",
			{ path: "x.py", context_focus_question: "which lines survive?" },
			null,
			null,
			{},
		);
		assert.ok(daemonReady, "tool call waited for the daemon");
		assert.equal(result.details.needle.decision, "pruned");
		const ops = requestFn.calls.map((call) => call.op);
		assert.ok(
			ops.indexOf("enable") < ops.indexOf("prune"),
			`enable precedes prune: ${ops.join(",")}`,
		);
	} finally {
		await pi.handlers.get("session_shutdown")(null, ctx);
	}
}

async function testSmallObservationsSkipped() {
	const requestFn = recordingRequestFn();
	const { pi, stop } = await installAndStart("tiny output", requestFn);
	try {
		const result = await pi.tools.get("read").execute(
			"t1",
			{ path: "x", context_focus_question: "anything?" },
			null,
			null,
			{},
		);
		assert.equal(extractText(result.content), "tiny output");
		assert.equal(result.details?.needle, undefined);
		assert.ok(!requestFn.calls.some((call) => call.op === "prune"), "no prune for tiny output");
	} finally {
		await stop();
	}
}

async function testUnchangedDecisionKeepsOriginal() {
	const requestFn = recordingRequestFn({
		pruneResponse: {
			ok: true,
			backend_status: "resident",
			decision: "unchanged",
			reason: "query-too-long",
			text: BIG_TEXT,
		},
	});
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		const result = await pi.tools.get("read").execute(
			"t1",
			{ path: "x.py", context_focus_question: "?".repeat(50) },
			null,
			null,
			{},
		);
		assert.equal(extractText(result.content), BIG_TEXT, "original content kept");
		assert.equal(result.details.needle.reason, "query-too-long");
	} finally {
		await stop();
	}
}

function testSplitEnvelope() {
	const { payload, envelope } = splitEnvelope(BIG_TEXT + ENVELOPE);
	assert.equal(payload, BIG_TEXT);
	assert.equal(envelope, ENVELOPE);

	const plain = splitEnvelope("no envelope here");
	assert.equal(plain.payload, "no envelope here");
	assert.equal(plain.envelope, "");

	// A bracketed line mid-text is payload, not envelope.
	const mid = splitEnvelope("before\n[not an envelope]\nafter");
	assert.equal(mid.envelope, "");
}

function testStatuslineStates() {
	const counters = { calls: 2, savedChars: 3100 };
	const base = { needleOn: true, backendStatus: "resident", busyPrunes: 0, counters };

	assert.equal(decideStatusState({ ...base, needleOn: false }), "off");
	assert.equal(decideStatusState({ ...base, backendStatus: "failed" }), "failed");
	assert.equal(decideStatusState({ ...base, busyPrunes: 1 }), "busy");
	assert.equal(decideStatusState({ ...base, backendStatus: "loading" }), "loading");
	assert.equal(decideStatusState(base), "resident");

	const line = formatStatus(base, { columns: 80, nowMs: 0 });
	assert.ok(line.includes("3.1k chars trimmed"), `counters shown: ${line}`);
	assert.ok(line.includes("2 prunes"), `prune count shown: ${line}`);

	const failed = formatStatus({ ...base, backendStatus: "failed" }, { columns: 80, nowMs: 0 });
	assert.ok(failed.includes("/needle off"), `failed state offers the off-ramp: ${failed}`);
}

function testSchemaHelperIdempotent() {
	const once = withRequiredFocusQuestion({ type: "object", properties: {}, required: [] });
	const twice = withRequiredFocusQuestion(once);
	assert.equal(
		twice.required.filter((name) => name === "context_focus_question").length,
		1,
		"required entry not duplicated",
	);
}

async function main() {
	await testSchemaRequiresFocusQuestion();
	await testReadVisiblePrune();
	await testBashEnvelopeSurvivesPruning();
	await testMissingFocusQuestionIsLoud();
	await testPruneFailureIsLoud();
	await testToolCallBlocksOnSlowEnablement();
	await testSmallObservationsSkipped();
	await testUnchangedDecisionKeepsOriginal();
	testSplitEnvelope();
	testStatuslineStates();
	testSchemaHelperIdempotent();
	console.log("test_pi_extension OK");
}

main().then(
	() => process.exit(0),
	(error) => {
		console.error(error);
		process.exit(1);
	},
);
