#!/usr/bin/env node

import assert from "node:assert/strict";
import { appendFile, mkdtemp, readFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { installHayPiExtension } from "./extension.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..");
const DEFAULT_PACK = "swe-pruner-reference";

async function main() {
	const packId = process.argv[2] || DEFAULT_PACK;
	const fixture = await loadFixturePack(packId);
	const temp = await mkdtemp(join(tmpdir(), "needle-pi-canary-"));
	const socketPath = join(temp, "manager.sock");
	const eventsPath = join(temp, "events.jsonl");
	const serverState = { pruneCalls: 0, sessions: new Set(), fixture, eventsPath };
	const server = createDemoManager(socketPath, serverState);
	await new Promise((resolve) => server.listen(socketPath, resolve));

	const oldEnv = captureEnv([
		"NEEDLE_MANAGER_SOCKET",
		"HAY_MANAGER_SOCKET",
		"NEEDLE_EVENTS",
		"HAY_EVENTS",
		"NEEDLE_CONFIG",
		"HAY_CONFIG",
		"NEEDLE_HOME",
		"HAY_HOME",
		"NEEDLE_PACKAGE",
		"HAY_PACKAGE",
	]);
	process.env.NEEDLE_MANAGER_SOCKET = socketPath;
	delete process.env.HAY_MANAGER_SOCKET;
	process.env.NEEDLE_EVENTS = eventsPath;
	delete process.env.HAY_EVENTS;
	process.env.NEEDLE_HOME = join(temp, "home");
	delete process.env.HAY_HOME;
	delete process.env.NEEDLE_PACKAGE;
	delete process.env.HAY_PACKAGE;

	try {
		const demo = installDemoExtension(fixture);
		await demo.handlers.get("session_start")({}, demo.ctx);

		const readPrune = await runReadCase(demo, fixture.caseById.get("read-visible-prune"), true);
		const bashPrune = await runBashCase(demo, fixture.caseById.get("bash-visible-prune"));
		const beforeMissingFocus = serverState.pruneCalls;
		const passThrough = await runReadCase(demo, fixture.caseById.get("read-missing-focus-passthrough"), false);

		assert.equal(serverState.pruneCalls, beforeMissingFocus, "missing-focus read must not call prune");
		await demo.commands.get("needle").handler("status", demo.ctx);
		await demo.handlers.get("session_shutdown")({}, demo.ctx);

		const counters = demo.customEntries.at(-1)?.data || { savedChars: 0, calls: 0 };
		assert.equal(counters.calls, 2);
		assert.ok(counters.savedChars > 0);
		const status = demo.messages.at(-1)?.content || "";
		assert.match(status, /this Pi session .* chars trimmed\s+\|\s+2 prunes/);

		printReport(fixture, [readPrune, bashPrune, passThrough], counters, status);
	} finally {
		restoreEnv(oldEnv);
		await new Promise((resolve) => server.close(resolve));
	}
}

async function loadFixturePack(packId) {
	const manifestPath = join(REPO_ROOT, "evidence", "fixture-packs", packId, "manifest.json");
	const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
	const cases = [];
	for (const ref of manifest.cases || []) {
		const path = join(dirname(manifestPath), ref.file);
		cases.push(JSON.parse(await readFile(path, "utf8")));
	}
	const caseById = new Map(cases.map((item) => [item.id, item]));
	for (const id of ["read-visible-prune", "bash-visible-prune", "read-missing-focus-passthrough"]) {
		assert.ok(caseById.has(id), `fixture pack ${packId} missing ${id}`);
	}
	return { packId, manifest, caseById };
}

function createDemoManager(socketPath, state) {
	return createServer((conn) => {
		let buf = "";
		conn.setEncoding("utf8");
		conn.on("data", async (chunk) => {
			buf += chunk;
			const idx = buf.indexOf("\n");
			if (idx < 0) return;
			const req = JSON.parse(buf.slice(0, idx));
			let resp;
			if (req.op === "stats") {
				resp = {
					ok: true,
					resident: true,
					backend: "demo-fixture-manager",
					sessions: state.sessions.size,
					version: "demo-canary",
					pressure: 1,
					available_mb: 4096,
				};
			} else if (req.op === "lease") {
				state.sessions.add(req.session);
				resp = { ok: true };
			} else if (req.op === "heartbeat") {
				resp = { ok: true };
			} else if (req.op === "release") {
				state.sessions.delete(req.session);
				resp = { ok: true };
			} else if (req.op === "prune") {
				state.pruneCalls += 1;
				const text = renderPrunedText(state.fixture, String(req.text || ""), String(req.query || ""));
				resp = {
					ok: true,
					text,
					original_len: String(req.text || "").length,
					pruned_len: text.length,
					backend: "demo-fixture-manager",
				};
			} else {
				resp = { ok: false, error: `unexpected op ${req.op}` };
			}
			await appendEvent(state.eventsPath, req, resp);
			conn.end(`${JSON.stringify(resp)}\n`);
		});
	});
}

async function appendEvent(path, req, resp) {
	const event = {
		ts: Math.floor(Date.now() / 1000),
		event: req.op,
		ok: Boolean(resp.ok),
	};
	if (req.op === "prune") {
		event.original = resp.original_len;
		event.returned = resp.pruned_len;
	}
	await appendFile(path, `${JSON.stringify(event)}\n`, "utf8");
}

function renderPrunedText(fixture, original, query) {
	const selected = [...fixture.caseById.values()].find((item) => item.context_focus_question === query);
	assert.ok(selected, `unexpected prune query ${query}`);
	const wanted = selected.assertions?.returned_contains || [];
	const lines = original.split(/\r?\n/);
	const kept = lines.filter((line) => wanted.some((fragment) => line.includes(fragment)));
	const marker = `(filtered ${Math.max(1, lines.length - kept.length)} lines)`;
	const rendered = [...kept.slice(0, 1), marker, ...kept.slice(1)].join("\n");
	for (const fragment of wanted) assert.ok(rendered.includes(fragment), `missing ${fragment}`);
	assert.ok(rendered.length < original.length, `${selected.id} did not shrink`);
	return rendered;
}

function installDemoExtension(fixture) {
	const handlers = new Map();
	const commands = new Map();
	const tools = new Map();
	const customEntries = [];
	const messages = [];
	const statuses = [];
	const pi = {
		appendEntry: (customType, data) => customEntries.push({ customType, data: { ...data } }),
		on: (event, handler) => handlers.set(event, handler),
		registerCommand: (name, options) => commands.set(name, options),
		registerTool: (definition) => tools.set(definition.name, definition),
		sendMessage: (message) => messages.push(message),
	};
	installHayPiExtension(pi, {
		createReadTool: () => ({
			name: "read",
			label: "read",
			description: "demo read",
			parameters: { type: "object", properties: { path: { type: "string" } } },
			async execute(_toolCallId, params) {
				const found = [...fixture.caseById.values()].find((item) => item.input.path === params.path);
				assert.ok(found, `unknown demo read path ${params.path}`);
				return { content: [{ type: "text", text: found.input.text }], details: { path: params.path } };
			},
		}),
		createBashTool: () => ({
			name: "bash",
			label: "bash",
			description: "demo bash",
			parameters: { type: "object", properties: { command: { type: "string" } } },
			async execute(_toolCallId, params) {
				const found = [...fixture.caseById.values()].find((item) => item.input.command === params.command);
				assert.ok(found, `unknown demo bash command ${params.command}`);
				return { content: [{ type: "text", text: found.input.text }], details: { command: params.command } };
			},
		}),
	});
	const ctx = {
		signal: new AbortController().signal,
		cwd: REPO_ROOT,
		sessionManager: {
			getSessionId: () => "needle-pi-canary",
			getEntries: () => [],
		},
		ui: {
			setStatus: (key, text) => statuses.push([key, text]),
			theme: { fg: (_name, text) => text },
		},
	};
	return { handlers, commands, tools, customEntries, messages, statuses, ctx };
}

async function runReadCase(demo, fixtureCase, includeFocus) {
	const params = { path: fixtureCase.input.path };
	if (includeFocus) params.context_focus_question = fixtureCase.context_focus_question;
	const result = await demo.tools.get("read").execute(`canary-${fixtureCase.id}`, params, demo.ctx.signal, undefined, demo.ctx);
	return assertCaseResult(fixtureCase, result);
}

async function runBashCase(demo, fixtureCase) {
	const result = await demo.tools.get("bash").execute(
		`canary-${fixtureCase.id}`,
		{
			command: fixtureCase.input.command,
			context_focus_question: fixtureCase.context_focus_question,
		},
		demo.ctx.signal,
		undefined,
		demo.ctx,
	);
	return assertCaseResult(fixtureCase, result);
}

function assertCaseResult(fixtureCase, result) {
	const returned = result.content?.[0]?.text || "";
	const original = fixtureCase.input.text;
	if (fixtureCase.expected_behavior === "passthrough_original") {
		assert.equal(returned, original);
		return summarizeCase(fixtureCase, original, returned);
	}
	assert.ok(returned.length < original.length);
	assert.ok(returned.includes("(filtered "));
	for (const fragment of fixtureCase.assertions.returned_contains || []) {
		assert.ok(returned.includes(fragment), `${fixtureCase.id} missing ${fragment}`);
	}
	return summarizeCase(fixtureCase, original, returned);
}

function summarizeCase(fixtureCase, original, returned) {
	return {
		id: fixtureCase.id,
		tool: fixtureCase.tool,
		behavior: fixtureCase.expected_behavior,
		originalChars: original.length,
		returnedChars: returned.length,
		savedChars: original.length - returned.length,
	};
}

function printReport(fixture, rows, counters, status) {
	console.log("Needle Pi demo canary");
	console.log(`package: ${fixture.manifest.package}`);
	console.log(`fixture pack: ${fixture.packId}`);
	console.log(`capability: ${fixture.manifest.capability}`);
	console.log("");
	console.log("case                            tool  behavior              original  returned  saved");
	for (const row of rows) {
		console.log(
			`${row.id.padEnd(31)} ${row.tool.padEnd(5)} ${row.behavior.padEnd(21)} ` +
				`${String(row.originalChars).padStart(8)}  ${String(row.returnedChars).padStart(8)}  ` +
				`${String(row.savedChars).padStart(5)}`,
		);
	}
	console.log("");
	console.log(`total chars trimmed: ${counters.savedChars}`);
	console.log(`prunes accepted: ${counters.calls}`);
	console.log("");
	console.log("status:");
	console.log(status);
	console.log("");
	console.log("This proves the Pi extension path, fixture wiring, pass-through, and local character accounting.");
	console.log("It does not prove MLX model quality, SWE-bench acceptance, token savings, or dollar savings.");
}

function captureEnv(names) {
	return Object.fromEntries(names.map((name) => [name, process.env[name]]));
}

function restoreEnv(values) {
	for (const [name, value] of Object.entries(values)) {
		if (value === undefined) {
			delete process.env[name];
		} else {
			process.env[name] = value;
		}
	}
}

main().catch((err) => {
	console.error(err?.stack || err);
	process.exit(1);
});
