// Pi extension behaviors: visible prunes, loud missing-focus/failure paths,
// envelope protection, required schema, statusline states.
//
// Run: node tests/test_pi_extension.mjs

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import path from "node:path";

import {
	decideStatusState,
	extractText,
	formatStatus,
	installNeedlePiExtension,
	splitEnvelope,
	withRequiredFocusQuestion,
} from "../pi/extension.js";
import { request as clientRequest } from "../pi/client.mjs";

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
		shortcuts: new Map(),
		registerShortcut(shortcut, definition) {
			this.shortcuts.set(shortcut, definition);
		},
		messages: [],
		sendMessage(message) {
			this.messages.push(message);
		},
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

function throwingToolFactory(message) {
	return () => ({
		label: "fake",
		description: "fake tool",
		parameters: {
			type: "object",
			properties: { path: { type: "string" } },
			required: ["path"],
		},
		execute: async () => {
			throw new Error(message);
		},
	});
}

function recordingRequestFn({
	enableResponse,
	enableError,
	pruneResponse,
	pruneError,
	originalText = BIG_TEXT,
} = {}) {
	const calls = [];
	const fn = async (op, fields = {}) => {
		calls.push({ op, ...fields });
		if (op === "enable") {
			if (enableError) throw enableError;
			return enableResponse ?? { ok: true, backend_status: "resident" };
		}
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
		if (op === "status") return { ok: true, mode: "on", backend_status: "resident", sessions: 1 };
		if (op === "original") return { ok: true, text: originalText };
		return { ok: true };
	};
	fn.calls = calls;
	return fn;
}

function deferred() {
	let resolve;
	let reject;
	const promise = new Promise((resolveFn, rejectFn) => {
		resolve = resolveFn;
		reject = rejectFn;
	});
	return { promise, resolve, reject };
}

async function flushMicrotasks(count = 5) {
	for (let i = 0; i < count; i += 1) await Promise.resolve();
}

async function withFakeIntervals(fn) {
	const originalSetInterval = globalThis.setInterval;
	const originalClearInterval = globalThis.clearInterval;
	const timers = [];
	globalThis.setInterval = (callback, delay, ...args) => {
		const timer = { callback, delay, args, cleared: false };
		timers.push(timer);
		return timer;
	};
	globalThis.clearInterval = (timer) => {
		if (timer) timer.cleared = true;
	};
	try {
		return await fn(timers);
	} finally {
		globalThis.setInterval = originalSetInterval;
		globalThis.clearInterval = originalClearInterval;
	}
}

function findTimer(timers, predicate) {
	const timer = timers.find((candidate) => !candidate.cleared && predicate(candidate));
	assert.ok(timer, `expected timer among delays: ${timers.map((item) => item.delay).join(", ")}`);
	return timer;
}

async function installAndStart(text, requestFn, options = {}) {
	const pi = fakePi();
	installNeedlePiExtension(pi, {
		createReadTool: options.createReadTool || fakeToolFactory(text),
		createBashTool: options.createBashTool || fakeToolFactory(text),
		requestFn,
		ensureDaemonFn: async () => true,
		nowFn: options.nowFn,
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
			assert.ok(parameters.properties.verbatim, `${name} has the verbatim bypass parameter`);
			assert.ok(!parameters.required.includes("verbatim"), `${name} does not require verbatim`);
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

async function testCostAccountingIsRecordedAtPruneTime() {
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
		assert.equal(result.details.needle.decision, "pruned");
		const first = pi.entries.at(-1).data.counters;
		assert.ok(first.savedTokensEstimate > 0, "saved token estimate recorded");
		assert.equal(first.costLowEstimate, null, "no pricing means no cost estimate");

		const ctx = {
			model: {
				cost: {
					input: 0.000010,
					cacheRead: 0.000001,
					output: 0,
					cacheWrite: 0,
				},
			},
			ui: { setStatus() {}, notify() {} },
		};
		await pi.handlers.get("model_select")({ model: ctx.model }, ctx);
		const unchanged = pi.entries.at(-1).data.counters;
		assert.equal(unchanged.costLowEstimate, null, "switching models does not reprice old prunes");
		assert.equal(unchanged.costHighEstimate, null, "switching models does not reprice old prunes");

		const pricedResult = await pi.tools.get("read").execute(
			"t2",
			{ path: "x.py", context_focus_question: "which lines should be kept?" },
			null,
			null,
			{},
		);
		assert.equal(pricedResult.details.needle.decision, "pruned");
		const priced = pi.entries.at(-1).data.counters;
		assert.ok(priced.costLowEstimate > 0, "priced prune records low estimate");
		assert.ok(priced.costHighEstimate > priced.costLowEstimate, "priced prune records high estimate");
		const costLine = formatStatus(
			{ needleOn: true, backendStatus: "resident", busyPrunes: 0, counters: priced, statusMode: "cost" },
			{ columns: 80, nowMs: 0 },
		);
		assert.ok(costLine.includes("est input avoided"), `priced cost line: ${costLine}`);

		const recordedLow = priced.costLowEstimate;
		const recordedHigh = priced.costHighEstimate;
		const expensiveModel = {
			cost: {
				input: 1,
				cacheRead: 0.5,
				output: 0,
				cacheWrite: 0,
			},
		};
		await pi.handlers.get("model_select")({ model: expensiveModel }, { ...ctx, model: expensiveModel });
		const afterSwitch = pi.entries.at(-1).data.counters;
		assert.equal(afterSwitch.costLowEstimate, recordedLow, "switching models keeps old low estimate");
		assert.equal(afterSwitch.costHighEstimate, recordedHigh, "switching models keeps old high estimate");
	} finally {
		await stop();
	}
}

async function testStatusPollingDecoupledFromAnimationAndThrottled() {
	await withFakeIntervals(async (timers) => {
		let now = 0;
		let statusRequests = 0;
		const statusGate = deferred();
		const requestFn = async (op) => {
			if (op === "enable") return { ok: true, backend_status: "resident" };
			if (op === "status") {
				statusRequests += 1;
				await statusGate.promise;
				return { ok: true, mode: "on", backend_status: "resident", sessions: 1 };
			}
			return { ok: true };
		};
		const { stop } = await installAndStart(BIG_TEXT, requestFn, { nowFn: () => now });
		try {
			await flushMicrotasks();
			const animationTimer = findTimer(timers, (timer) => timer.delay < 1000);
			const statusTimer = findTimer(
				timers,
				(timer) => timer.delay > animationTimer.delay && timer.delay < 30_000,
			);
			assert.ok(statusTimer.delay > animationTimer.delay, "status polling is slower than repaint");

			for (let i = 0; i < 10; i += 1) {
				now += animationTimer.delay;
				animationTimer.callback(...animationTimer.args);
			}
			assert.equal(statusRequests, 0, "animation ticks do not hit the daemon");

			statusTimer.callback(...statusTimer.args);
			statusTimer.callback(...statusTimer.args);
			statusTimer.callback(...statusTimer.args);
			await flushMicrotasks();
			assert.equal(statusRequests, 1, "burst status ticks collapse while one poll is in flight");

			statusGate.resolve();
			await flushMicrotasks();
			now += statusTimer.delay - 1;
			statusTimer.callback(...statusTimer.args);
			await flushMicrotasks();
			assert.equal(statusRequests, 1, "status polling is throttled by the last checked time");

			now += 1;
			statusTimer.callback(...statusTimer.args);
			await flushMicrotasks();
			assert.equal(statusRequests, 2, "status polling resumes after the poll interval");
		} finally {
			statusGate.resolve();
			await stop();
		}
	});
}

async function testStatusPollingSkipsWhilePruneIsBusy() {
	await withFakeIntervals(async (timers) => {
		let now = 0;
		let statusRequests = 0;
		const pruneGate = deferred();
		const pruneStarted = deferred();
		const requestFn = async (op, fields = {}) => {
			if (op === "enable") return { ok: true, backend_status: "resident" };
			if (op === "status") {
				statusRequests += 1;
				return { ok: true, mode: "on", backend_status: "resident", sessions: 1 };
			}
			if (op === "prune") {
				pruneStarted.resolve();
				await pruneGate.promise;
				return {
					ok: true,
					backend_status: "resident",
					decision: "pruned",
					reason: "model",
					text: fields.text.replaceAll(" drop", ""),
				};
			}
			return { ok: true };
		};
		const { pi, stop } = await installAndStart(BIG_TEXT, requestFn, { nowFn: () => now });
		let toolPromise;
		try {
			await flushMicrotasks();
			const statusTimer = findTimer(timers, (timer) => timer.delay > 1000 && timer.delay < 30_000);
			toolPromise = pi.tools.get("read").execute(
				"t1",
				{ path: "x.py", context_focus_question: "which lines survive?" },
				null,
				null,
				{},
			);
			await pruneStarted.promise;
			now += statusTimer.delay;
			statusTimer.callback(...statusTimer.args);
			await flushMicrotasks();
			assert.equal(statusRequests, 0, "status poll skipped while prune is busy");

			pruneGate.resolve();
			const result = await toolPromise;
			assert.equal(result.details.needle.decision, "pruned");
		} finally {
			pruneGate.resolve();
			if (toolPromise) await toolPromise.catch(() => undefined);
			await stop();
		}
	});
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

async function testVerbatimBypassesPruning() {
	const requestFn = recordingRequestFn();
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		const result = await pi.tools.get("read").execute(
			"t1",
			{
				path: "x.py",
				context_focus_question: "What exact file contents are needed before editing?",
				verbatim: true,
			},
			null,
			null,
			{},
		);
		assert.equal(extractText(result.content), BIG_TEXT, "verbatim read keeps original");
		assert.equal(result.details.needle.reason, "verbatim");
		assert.ok(!requestFn.calls.some((call) => call.op === "prune"), "no prune for verbatim read");
	} finally {
		await stop();
	}
}

async function testThrownBashErrorIsPrunedAndKeepsExitStatus() {
	const requestFn = recordingRequestFn();
	const errorText = `${BIG_TEXT}\n\nCommand exited with code 1`;
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn, {
		createBashTool: throwingToolFactory(errorText),
	});
	try {
		await assert.rejects(
			() => pi.tools.get("bash").execute(
				"t1",
				{ path: "ls", context_focus_question: "Which failing lines explain the error?" },
				null,
				null,
				{},
			),
			(error) => {
				assert.ok(!error.message.includes(" drop"), "thrown output was pruned");
				assert.ok(error.message.includes("Command exited with code 1"), "exit status preserved");
				return true;
			},
		);
		const prune = requestFn.calls.find((call) => call.op === "prune");
		assert.ok(prune, "thrown error was sent to the pruner");
		assert.ok(!prune.text.includes("Command exited with code 1"), "exit status kept out of model payload");
	} finally {
		await stop();
	}
}

async function testCriticalMemoryPressureIsLoud() {
	const error =
		"memory pressure critical (12 MB available; minimum 2048 MB for cold model load; source vm_stat)";
	const requestFn = recordingRequestFn({
		enableResponse: { ok: false, backend_status: "failed", error },
	});
	const { pi, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		const result = await pi.tools.get("read").execute(
			"t1",
			{ path: "x.py", context_focus_question: "Which lines matter?" },
			null,
			null,
			{},
		);
		const text = extractText(result.content);
		assert.ok(text.startsWith(`[needle failed: ${error}`), "loud memory banner");
		assert.ok(text.includes(BIG_TEXT), "original output still follows");
		assert.equal(result.details.needle.reason, error);
		assert.ok(requestFn.calls.some((call) => call.op === "enable"), "enable surfaced the daemon refusal");
		assert.ok(!requestFn.calls.some((call) => call.op === "prune"), "no prune under critical memory");
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

async function testNeedleOriginalCommand() {
	const requestFn = recordingRequestFn({ originalText: "pre-prune output" });
	const { pi, ctx, stop } = await installAndStart(BIG_TEXT, requestFn);
	try {
		await pi.commands.get("needle").handler("original", ctx);
		const message = pi.messages.at(-1);
		assert.equal(message.customType, "needle-original");
		assert.equal(message.content, "pre-prune output");
		const original = requestFn.calls.find((call) => call.op === "original");
		assert.equal(original.session, "s-test");
	} finally {
		await stop();
	}
}

async function testNeedleOnPostsTranscriptStatusOnFailure() {
	const scratch = mkdtempSync("/tmp/needle-on-command-");
	const fakeNeedle = path.join(scratch, "needle");
	writeFileSync(
		fakeNeedle,
		"#!/bin/sh\n" +
			"if [ \"$1\" = paths ] && [ \"$2\" = --json ]; then\n" +
			"  echo '{\"home\":\"/tmp/needle-test-home\",\"socket\":\"/tmp/needle-test.sock\",\"config\":\"/tmp/needle-test-home/config.json\"}'\n" +
			"  exit 0\n" +
			"fi\n" +
			"exit 1\n",
	);
	chmodSync(fakeNeedle, 0o755);
	const previousNeedleBin = process.env.NEEDLE_BIN;
	process.env.NEEDLE_BIN = fakeNeedle;
	const pi = fakePi();
	const notifications = [];
	const requestFn = async (op) => {
		if (op === "status") throw new Error("socket unavailable");
		return { ok: true };
	};
	installNeedlePiExtension(pi, {
		createReadTool: fakeToolFactory(BIG_TEXT),
		createBashTool: fakeToolFactory(BIG_TEXT),
		requestFn,
		ensureDaemonFn: async () => false,
	});
	const ctx = {
		ui: {
			setStatus() {},
			notify(message, level) {
				notifications.push({ message, level });
			},
		},
	};

	try {
		await pi.commands.get("needle").handler("on", ctx);
	} finally {
		if (previousNeedleBin === undefined) delete process.env.NEEDLE_BIN;
		else process.env.NEEDLE_BIN = previousNeedleBin;
		rmSync(scratch, { recursive: true, force: true });
	}

	const message = pi.messages.at(-1);
	assert.equal(message.customType, "needle-status");
	assert.equal(message.display, true);
	assert.ok(message.content.startsWith("needle: on failed"), message.content);
	assert.ok(message.content.includes("last error: needle daemon did not start"), message.content);
	assert.deepEqual(notifications, [], "sendMessage avoids transient-only notifications");
}

function testSplitEnvelope() {
	const { payload, envelope } = splitEnvelope(BIG_TEXT + ENVELOPE);
	assert.equal(payload, BIG_TEXT);
	assert.equal(envelope, ENVELOPE);

	const failed = splitEnvelope(`${BIG_TEXT}\n\nCommand exited with code 1`);
	assert.equal(failed.payload, BIG_TEXT);
	assert.equal(failed.envelope, "\n\nCommand exited with code 1");

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

	const off = formatStatus({ ...base, needleOn: false }, { columns: 80, nowMs: 0 });
	assert.ok(off.includes("3.1k chars trimmed"), `off keeps counter text stable: ${off}`);
	assert.ok(off.includes("2 prunes"), `off keeps prune count stable: ${off}`);
	assert.ok(!off.includes("needle off"), `off state uses glyph rather than text churn: ${off}`);

	const failed = formatStatus({ ...base, backendStatus: "failed" }, { columns: 80, nowMs: 0 });
	assert.ok(failed.includes("3.1k chars trimmed"), `failed keeps counter text stable: ${failed}`);
	assert.ok(!failed.includes("/needle off"), `failed state uses status message for off-ramp: ${failed}`);
}

function testStatuslineModes() {
	const counters = { calls: 2, savedChars: 4000 };
	const base = { needleOn: true, backendStatus: "resident", busyPrunes: 0, counters };

	const chars = formatStatus({ ...base, statusMode: "chars" }, { columns: 80, nowMs: 0 });
	assert.ok(chars.includes("4.0k chars trimmed"), `chars mode: ${chars}`);

	const tokens = formatStatus({ ...base, statusMode: "tokens" }, { columns: 80, nowMs: 0 });
	assert.ok(tokens.includes("~1.0k input tokens avoided"), `tokens mode: ${tokens}`);

	const costUnavailable = formatStatus({ ...base, statusMode: "cost" }, { columns: 80, nowMs: 0 });
	assert.ok(costUnavailable.includes("pricing unavailable"), `cost unavailable mode: ${costUnavailable}`);
	assert.ok(!costUnavailable.includes("input tokens avoided"), `cost unavailable is distinct: ${costUnavailable}`);

	const cost = formatStatus(
		{
			...base,
			statusMode: "cost",
			counters: {
				...counters,
				costLowEstimate: 0.001,
				costHighEstimate: 0.005,
			},
		},
		{ columns: 80, nowMs: 0 },
	);
	assert.ok(cost.includes("~$0.001-$0.005 est input avoided"), `cost mode: ${cost}`);

	const compact = formatStatus({ ...base, statusMode: "compact" }, { columns: 80, nowMs: 0 });
	assert.ok(compact.includes("4.0kc"), `compact mode: ${compact}`);
	assert.ok(compact.includes("2p"), `compact mode prunes: ${compact}`);
}

async function testStatuslineShortcutCyclesMode() {
	const pi = fakePi();
	const statuses = [];
	const notifications = [];
	const requestFn = recordingRequestFn();
	installNeedlePiExtension(pi, {
		createReadTool: fakeToolFactory(BIG_TEXT),
		createBashTool: fakeToolFactory(BIG_TEXT),
		requestFn,
		ensureDaemonFn: async () => true,
	});
	const ctx = {
		model: {
			cost: {
				input: 0.000005,
				cacheRead: 0.000001,
				output: 0,
				cacheWrite: 0,
			},
		},
		ui: {
			setStatus(_key, value) {
				statuses.push(value);
			},
			notify(message, level) {
				notifications.push({ message, level });
			},
		},
	};

	assert.ok(pi.shortcuts.has("ctrl+shift+n"), "toggle shortcut registered");
	assert.ok(pi.shortcuts.has("f8"), "toggle fallback shortcut registered");
	assert.ok(pi.shortcuts.has("ctrl+shift+."), "cycle shortcut registered");
	assert.ok(pi.shortcuts.has("f9"), "cycle fallback shortcut registered");

	await pi.shortcuts.get("f8").handler(ctx);
	assert.equal(requestFn.calls.length, 0);
	assert.ok(statuses.at(-1).includes("needle"), `toggle status updated: ${statuses.at(-1)}`);
	assert.deepEqual(notifications.at(-1), {
		message: "needle off: tool output passes through untouched",
		level: "info",
	});

	await pi.shortcuts.get("f8").handler(ctx);
	assert.ok(requestFn.calls.some((call) => call.op === "enable"), "toggle on enables Needle");
	assert.equal(pi.messages.at(-1).customType, "needle-status");

	await pi.shortcuts.get("f9").handler(ctx);

	assert.ok(statuses.at(-1).includes("input tokens avoided"), `status updated: ${statuses.at(-1)}`);
	assert.deepEqual(notifications.at(-1), {
		message: "needle statusline: estimated tokens",
		level: "info",
	});
	assert.equal(pi.entries.at(-1).type, "needle-state");
	assert.equal(pi.entries.at(-1).data.statusMode, "tokens");

	await pi.shortcuts.get("f9").handler(ctx);
	assert.ok(statuses.at(-1).includes("pricing unavailable"), `cost mode waits for priced prunes: ${statuses.at(-1)}`);
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

async function withSocketServer(handler, fn) {
	const scratch = mkdtempSync("/tmp/needle-client-");
	const socketPath = path.join(scratch, "needle.sock");
	const sockets = new Set();
	const server = net.createServer((socket) => {
		sockets.add(socket);
		socket.on("close", () => sockets.delete(socket));
		handler(socket);
	});
	await new Promise((resolve, reject) => {
		server.once("error", reject);
		server.listen(socketPath, resolve);
	});
	const previousSocket = process.env.NEEDLE_SOCKET;
	process.env.NEEDLE_SOCKET = socketPath;
	try {
		return await fn();
	} finally {
		if (previousSocket === undefined) delete process.env.NEEDLE_SOCKET;
		else process.env.NEEDLE_SOCKET = previousSocket;
		for (const socket of sockets) socket.destroy();
		await new Promise((resolve) => server.close(resolve));
		rmSync(scratch, { recursive: true, force: true });
	}
}

async function testClientRequestSettlesOnFirstLine() {
	await withSocketServer((socket) => {
		socket.on("error", () => undefined);
		socket.on("data", () => {
			socket.write('{"ok":true,"first":true}\n');
			socket.write('this is not json\n');
			setImmediate(() => socket.destroy(new Error("late reset")));
		});
	}, async () => {
		const response = await clientRequest("status", {}, { timeoutMs: 1000 });
		assert.deepEqual(response, { ok: true, first: true });
		await new Promise((resolve) => setTimeout(resolve, 20));
	});
}

async function testClientRequestRejectsClosedConnectionWithoutNewline() {
	await withSocketServer((socket) => {
		socket.on("data", () => {
			socket.write('{"ok":true');
			socket.end();
		});
	}, async () => {
		await assert.rejects(
			() => clientRequest("status", {}, { timeoutMs: 1000 }),
			/connection (ended|closed) before response/,
		);
	});
}

async function main() {
	await testSchemaRequiresFocusQuestion();
	await testReadVisiblePrune();
	await testCostAccountingIsRecordedAtPruneTime();
	await testStatusPollingDecoupledFromAnimationAndThrottled();
	await testStatusPollingSkipsWhilePruneIsBusy();
	await testBashEnvelopeSurvivesPruning();
	await testMissingFocusQuestionIsLoud();
	await testPruneFailureIsLoud();
	await testVerbatimBypassesPruning();
	await testThrownBashErrorIsPrunedAndKeepsExitStatus();
	await testCriticalMemoryPressureIsLoud();
	await testToolCallBlocksOnSlowEnablement();
	await testSmallObservationsSkipped();
	await testUnchangedDecisionKeepsOriginal();
	await testNeedleOriginalCommand();
	await testNeedleOnPostsTranscriptStatusOnFailure();
	testSplitEnvelope();
	testStatuslineStates();
	testStatuslineModes();
	await testStatuslineShortcutCyclesMode();
	testSchemaHelperIdempotent();
	await testClientRequestSettlesOnFirstLine();
	await testClientRequestRejectsClosedConnectionWithoutNewline();
	console.log("test_pi_extension OK");
}

main().then(
	() => process.exit(0),
	(error) => {
		console.error(error);
		process.exit(1);
	},
);
