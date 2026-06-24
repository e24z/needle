import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cp, mkdtemp, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { codeVersion, packageIdentity, prune, socketIsLive, sourceIdentity, tailEvents } from "../adapters/pi/client.mjs";
import {
	buildToolResultPatch,
	decideStatusState,
	extractQuery,
	extractText,
	formatIndicator,
	formatStatus,
	installHayPiExtension,
	renderOperatorStatus,
} from "../adapters/pi/extension.js";

test("Pi client speaks Hay newline JSON protocol", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-"));
	const socketPath = join(dir, "manager.sock");
	const server = createServer((conn) => {
		let buf = "";
		conn.setEncoding("utf8");
		conn.on("data", (chunk) => {
			buf += chunk;
			const idx = buf.indexOf("\n");
			if (idx < 0) return;
			const req = JSON.parse(buf.slice(0, idx));
			if (req.op === "stats") {
				conn.end(JSON.stringify({ ok: true, resident: true, backend: "mock" }) + "\n");
				return;
			}
			assert.equal(req.op, "prune");
			conn.end(JSON.stringify({
				ok: true,
				text: req.text.slice(0, 5),
				original_len: req.text.length,
				pruned_len: 5,
				backend: "mock",
			}) + "\n");
		});
	});
	await new Promise((resolve) => server.listen(socketPath, resolve));
	try {
		assert.equal(await socketIsLive(socketPath), true);
		const resp = await prune("abcdefghij", "letters", { socketPath });
		assert.equal(resp.text, "abcde");
	} finally {
		await new Promise((resolve) => server.close(resolve));
	}
});

test("Pi adapter patches prunable tool results and records savings", async () => {
	const counters = { calls: 0, originalChars: 0, prunedChars: 0, savedChars: 0 };
	const event = {
		toolName: "read",
		content: [{ type: "text", text: "x".repeat(1000) }],
	};
	const ctx = {
		sessionManager: {
			getEntries: () => [
				{
					type: "message",
					message: { role: "assistant", content: [{ type: "text", text: "read the config" }] },
				},
			],
		},
	};
	const patch = await buildToolResultPatch(event, ctx, counters, async (text, query) => {
		assert.equal(query, "read the config");
		return { ok: true, text: text.slice(0, 400) };
	});
	assert.deepEqual(patch, { content: [{ type: "text", text: "x".repeat(400) }] });
	assert.equal(counters.calls, 1);
	assert.equal(counters.savedChars, 600);
});

test("Pi extension lifecycle leases, overrides read, updates status, and releases", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-lifecycle-"));
	const socketPath = join(dir, "manager.sock");
	const ops = [];
	const server = createServer((conn) => {
		let buf = "";
		conn.setEncoding("utf8");
		conn.on("data", (chunk) => {
			buf += chunk;
			const idx = buf.indexOf("\n");
			if (idx < 0) return;
			const req = JSON.parse(buf.slice(0, idx));
			ops.push(req.op);
			if (req.op === "stats") {
				conn.end(JSON.stringify({ ok: true, resident: true, backend: "mock" }) + "\n");
			} else if (req.op === "lease" || req.op === "heartbeat" || req.op === "release") {
				conn.end(JSON.stringify({ ok: true }) + "\n");
			} else if (req.op === "prune") {
				conn.end(JSON.stringify({ ok: true, text: req.text.slice(0, 300) }) + "\n");
			} else {
				conn.end(JSON.stringify({ ok: false, error: "unexpected op" }) + "\n");
			}
		});
	});
	await new Promise((resolve) => server.listen(socketPath, resolve));

	const oldSocket = process.env.HAY_MANAGER_SOCKET;
	process.env.HAY_MANAGER_SOCKET = socketPath;
	try {
		const handlers = new Map();
		const commands = new Map();
		const tools = new Map();
		const customEntries = [];
		const messages = [];
		const statuses = [];
		const pi = {
			appendEntry: (customType, data) => customEntries.push({ type: "custom", customType, data }),
			on: (event, handler) => handlers.set(event, handler),
			registerCommand: (name, options) => commands.set(name, options),
			registerTool: (definition) => tools.set(definition.name, definition),
			sendMessage: (message) => messages.push(message),
		};
		installHayPiExtension(pi, {
			createReadTool: (cwd) => ({
				name: "read",
				label: "read",
				description: "mock Pi read",
				parameters: {},
				async execute() {
					return {
						content: [{ type: "text", text: "x".repeat(1000) }],
						details: { cwd },
					};
				},
			}),
		});
		const ctx = {
			signal: new AbortController().signal,
			cwd: "/tmp/pi-cwd",
			sessionManager: {
				getSessionId: () => "pi-test",
				getEntries: () => [
					{
						type: "message",
						message: { role: "assistant", content: [{ type: "text", text: "summarize this file" }] },
					},
				],
			},
			ui: {
				setStatus: (key, text) => statuses.push([key, text]),
				theme: { fg: (_name, text) => text },
			},
		};

		await handlers.get("session_start")({}, ctx);
		assert.equal(handlers.has("tool_result"), false);
		assert.equal(tools.has("read"), true);
		const result = await tools.get("read").execute("tool-call-1", { path: "file.py" }, ctx.signal, undefined, ctx);
		await commands.get("hay").handler("status", ctx);
		await handlers.get("session_shutdown")({}, ctx);

		assert.deepEqual(result.content, [{ type: "text", text: "x".repeat(300) }]);
		assert.equal(result.details.cwd, "/tmp/pi-cwd");
		assert.equal(customEntries.length, 1);
		assert.deepEqual(customEntries[0].data.calls, 1);
		assert.equal(messages.length, 1);
		assert.equal(messages[0].customType, "hay-status");
		assert.match(messages[0].content, /hay manager: ready \(mock resident\)/);
		assert.match(messages[0].content, /why running:/);
		assert.match(messages[0].content, /this Pi session 700 chars trimmed  \|  1 prunes/);
		assert.equal(statuses.at(-1)[0], "hay");
		assert.match(statuses.at(-1)[1], /hay · 700 chars trimmed · 1 prune/);
		assert.ok(ops.includes("lease"), ops);
		assert.ok(ops.includes("prune"), ops);
		assert.ok(ops.includes("release"), ops);
	} finally {
		if (oldSocket === undefined) {
			delete process.env.HAY_MANAGER_SOCKET;
		} else {
			process.env.HAY_MANAGER_SOCKET = oldSocket;
		}
		await new Promise((resolve) => server.close(resolve));
	}
});

test("Pi operator status renders loading, degraded, memory, and local events", async () => {
	const rendered = renderOperatorStatus(
		{
			ok: true,
			resident: true,
			backend: "fake (code-pruner unavailable: no mlx)",
			sessions: 2,
			version: "abcdef123456789",
			pressure: 2,
			available_mb: 2048,
		},
		[{ ts: 1710000000, event: "passthrough", reason: "low-memory", chars: 1200 }],
		{ calls: 3, savedChars: 4096, lastTool: "grep" },
		{
			appHome: "/tmp/hay",
			extensionPath: "/tmp/hay/adapters/pi/extension.js",
			socketPath: "/tmp/hay/manager.sock",
			source: {
				repoRoot: "/tmp/hay",
				packageName: "hay",
				packageVersion: "0.1.0",
				pyprojectVersion: "0.1.0",
				modelRoot: "/tmp/hay/models",
				activePackage: {
					available: true,
					id: "e24z/pi-local-mac",
					capabilities: ["swe-pruner/reference"],
					backend: "e24z/code-pruner-mlx",
					hostBinding: "pi/native-tools",
					packageCard: "package-cards/e24z/pi-local-mac",
					claimCard: "claims/pi-local-mac-swe-pruner-reference",
					compute: "local_mlx",
					privacy: "local_only",
					promptBundle: "pi/context-focus-question@0.1",
				},
				git: { available: true, branch: "pi-adapter", commit: "abcdef123456", dirty: true, dirtyFiles: 2 },
			},
		},
	);
	assert.match(rendered, /DEGRADED \(fake \(code-pruner unavailable: no mlx\)\)/);
	assert.match(rendered, /sessions 2  \|  version abcdef123456/);
	assert.match(rendered, /pressure warning  \|  free 2.0 GB/);
	assert.match(rendered, /this Pi session 4.1k chars trimmed  \|  3 prunes  \|  last tool grep/);
	assert.match(rendered, /extension \/tmp\/hay\/adapters\/pi\/extension\.js/);
	assert.match(rendered, /model dir \/tmp\/hay\/models/);
	assert.match(rendered, /active package e24z\/pi-local-mac/);
	assert.match(rendered, /capability swe-pruner\/reference/);
	assert.match(rendered, /backend e24z\/code-pruner-mlx/);
	assert.match(rendered, /host binding pi\/native-tools/);
	assert.match(rendered, /compute local_mlx \| privacy local_only/);
	assert.match(rendered, /prompt bundle pi\/context-focus-question@0\.1/);
	assert.match(rendered, /package card package-cards\/e24z\/pi-local-mac/);
	assert.match(rendered, /claim card claims\/pi-local-mac-swe-pruner-reference/);
	assert.match(rendered, /version package hay@0\.1\.0 \| pyproject 0\.1\.0/);
	assert.match(rendered, /git pi-adapter@abcdef123456 \(dirty, 2 files\)/);
	assert.match(rendered, /passthrough\s+reason=low-memory chars=1200/);
	assert.match(renderOperatorStatus("loading", [], {}), /loading or pruning/);
	assert.match(renderOperatorStatus(null, [], {}), /fails open/);
	assert.match(formatStatus("loading", { savedChars: 0, calls: 0 }), /hay · 0 chars trimmed · 0 prunes/);
});

test("Pi source identity reads package, pyproject, git state, and active Needle package", async () => {
	const identity = await sourceIdentity(process.cwd(), { timeoutMs: 1_000 });
	assert.equal(identity.packageName, "hay");
	assert.equal(identity.packageVersion, "0.1.0");
	assert.equal(identity.pyprojectVersion, "0.1.0");
	assert.match(identity.modelRoot, /\/\.hay\/models$/);
	assert.equal(identity.activePackage.id, "e24z/pi-local-mac");
	assert.deepEqual(identity.activePackage.capabilities, ["swe-pruner/reference"]);
	assert.equal(identity.activePackage.backend, "e24z/code-pruner-mlx");
	assert.equal(identity.activePackage.hostBinding, "pi/native-tools");
	assert.equal(identity.activePackage.claimCard, "claims/pi-local-mac-swe-pruner-reference");
	assert.equal(typeof identity.git.available, "boolean");

	const missing = await packageIdentity(process.cwd(), "e24z/does-not-exist");
	assert.equal(missing.available, false);
	assert.equal(missing.id, "e24z/does-not-exist");
});

test("Pi package identity can load from an external registry root", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-registry-"));
	for (const name of ["packages", "capabilities", "backends", "bindings", "claims", "package-cards", "protocols"]) {
		await cp(join(process.cwd(), name), join(dir, name), { recursive: true });
	}

	const oldRoot = process.env.HAY_REGISTRY_ROOT;
	try {
		process.env.HAY_REGISTRY_ROOT = dir;
		const identity = await packageIdentity(process.cwd());
		assert.equal(identity.available, true);
		assert.equal(identity.id, "e24z/pi-local-mac");
		assert.equal(identity.backend, "e24z/code-pruner-mlx");
	} finally {
		if (oldRoot === undefined) {
			delete process.env.HAY_REGISTRY_ROOT;
		} else {
			process.env.HAY_REGISTRY_ROOT = oldRoot;
		}
	}
});

test("Pi client reads the local Hay event log", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-events-"));
	const path = join(dir, "events.jsonl");
	await writeFile(
		path,
		[
			JSON.stringify({ ts: 1, event: "lease", session: "a" }),
			"not-json",
			JSON.stringify({ ts: 2, event: "model_load", backend: "mock" }),
			JSON.stringify({ ts: 3, event: "release", session: "a" }),
			"",
		].join("\n"),
	);
	assert.deepEqual(await tailEvents(2, { path }), [
		{ ts: 2, event: "model_load", backend: "mock" },
		{ ts: 3, event: "release", session: "a" },
	]);
});

test("Pi adapter ignores tiny, non-target, and unchanged results", async () => {
	const counters = { calls: 0, originalChars: 0, prunedChars: 0, savedChars: 0 };
	assert.equal(extractText([{ type: "text", text: "a" }, { type: "text", text: "b" }]), "a\nb");
	assert.equal(extractText([{ type: "image", data: "nope" }]), "");
	assert.equal(
		await buildToolResultPatch(
			{ toolName: "bash", content: [{ type: "text", text: "x".repeat(1000) }] },
			{},
			counters,
			async () => ({ ok: true, text: "" }),
		),
		undefined,
	);
	assert.equal(
		await buildToolResultPatch(
			{ toolName: "read", content: [{ type: "text", text: "short" }] },
			{},
			counters,
			async () => ({ ok: true, text: "" }),
		),
		undefined,
	);
	assert.equal(
		await buildToolResultPatch(
			{ toolName: "read", content: [{ type: "text", text: "x".repeat(1000) }] },
			{},
			counters,
			async (text) => ({ ok: true, text }),
		),
		undefined,
	);
});

test("Pi query extraction uses the latest assistant text", () => {
	const query = extractQuery({
		sessionManager: {
			getEntries: () => [
				{ type: "message", message: { role: "user", content: "what" } },
				{ type: "message", message: { role: "assistant", content: [{ type: "text", text: "old" }] } },
				{ type: "message", message: { role: "assistant", content: [{ type: "text", text: "new" }] } },
			],
		},
	});
	assert.equal(query, "new");
	const fallback = extractQuery({
		sessionManager: {
			getEntries: () => [
				{ type: "message", message: { role: "user", content: "read the model path code" } },
				{ type: "message", message: { role: "assistant", content: [{ type: "toolCall", name: "read" }] } },
			],
		},
	});
	assert.equal(fallback, "read the model path code");
});

test("Pi status formatter is honest about cold and degraded states", () => {
	assert.equal(decideStatusState(null, {}), "down");
	assert.equal(decideStatusState("loading", {}), "loading");
	assert.equal(decideStatusState({ ok: false }, {}), "down");
	assert.equal(decideStatusState({ ok: true, resident: false }, { updatedAt: Date.now() }), "cold");
	assert.equal(
		decideStatusState(
			{ ok: true, resident: true, backend: "fake (code-pruner unavailable: x)" },
			{ updatedAt: Date.now() },
		),
		"degraded",
	);
	assert.equal(
		decideStatusState({ ok: true, resident: true, backend: "code-pruner" }, { updatedAt: 0 }, { nowMs: 10_000 }),
		"ready",
	);
	assert.equal(
		decideStatusState(
			{ ok: true, resident: true, backend: "code-pruner" },
			{ updatedAt: 0 },
			{ nowMs: 10_000, busy: true },
		),
		"active",
	);
	assert.equal(
		decideStatusState(
			{ ok: true, resident: false, backend: "code-pruner" },
			{ updatedAt: 9_000 },
			{ nowMs: 10_000, busy: true },
		),
		"cold",
	);
	assert.equal(
		decideStatusState(
			{ ok: true, resident: true, backend: "code-pruner" },
			{ updatedAt: 9_000 },
			{ nowMs: 10_000 },
		),
		"active",
	);
	for (const state of ["down", "cold", "loading", "degraded", "ready", "active"]) {
		assert.ok(formatIndicator(state, undefined, { nowMs: 10_000 }));
	}
	assert.match(formatIndicator("ready", undefined, { nowMs: 10_000 }), /\x1b\[38;5;35m/);
	assert.match(formatIndicator("loading", undefined, { nowMs: 10_000 }), /\x1b\[38;5;179m/);
	assert.match(formatIndicator("active", undefined, { nowMs: 10_000 }), /\x1b\[38;5;87m/);
	assert.match(
		formatStatus(
			{ ok: true, resident: true, backend: "code-pruner" },
			{ savedChars: 0, calls: 0 },
			undefined,
			{ nowMs: 10_000, busy: true },
		),
		/^\x1b\[38;5;87m/,
	);
	assert.notEqual(formatIndicator("ready", undefined, { nowMs: 0 }), formatIndicator("ready", undefined, { nowMs: 400 }));
	assert.match(formatStatus(null, { savedChars: 400, calls: 1 }, undefined, { columns: 100 }), /^.+ hay · 400 chars trimmed · 1 prune$/);
	assert.match(formatStatus({ ok: true, resident: false }, { savedChars: 0, calls: 0 }, undefined, { columns: 100 }), /hay · 0 chars trimmed · 0 prunes/);
	assert.match(
		formatStatus({ ok: true, resident: true, backend: "code-pruner" }, { calls: 12, savedChars: 4096 }, undefined, {
			columns: 12,
		}),
		/^.+ hay$/,
	);
});

test("Pi codeVersion matches the Python engine hash", async () => {
	const jsVersion = await codeVersion(process.cwd());
	const py = spawnSync("python3", ["-c", "from pruner.naming import code_version; print(code_version())"], {
		cwd: process.cwd(),
		env: { ...process.env, PYTHONPATH: "." },
		encoding: "utf8",
	});
	assert.equal(py.status, 0, py.stderr);
	assert.equal(jsVersion, py.stdout.trim());
});
