import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { codeVersion, prune, request, socketIsLive } from "../adapters/pi/client.mjs";
import hayPiExtension, {
	buildToolResultPatch,
	extractQuery,
	extractText,
	formatStatus,
} from "../adapters/pi/extension.mjs";

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

test("Pi extension lifecycle leases, prunes, updates status, and releases", async () => {
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
		const customEntries = [];
		const statuses = [];
		const pi = {
			appendEntry: (customType, data) => customEntries.push({ type: "custom", customType, data }),
			on: (event, handler) => handlers.set(event, handler),
		};
		hayPiExtension(pi);
		const ctx = {
			signal: new AbortController().signal,
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
		const patch = await handlers.get("tool_result")(
			{ toolName: "read", content: [{ type: "text", text: "x".repeat(1000) }] },
			ctx,
		);
		await handlers.get("session_shutdown")({}, ctx);

		assert.deepEqual(patch, { content: [{ type: "text", text: "x".repeat(300) }] });
		assert.equal(customEntries.length, 1);
		assert.deepEqual(customEntries[0].data.calls, 1);
		assert.equal(statuses.at(-1)[0], "hay");
		assert.match(statuses.at(-1)[1], /hay ready 175t 1p/);
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
});

test("Pi status formatter is honest about cold and degraded states", () => {
	assert.match(formatStatus(null, { savedChars: 400, calls: 1 }), /hay down 100t 1p/);
	assert.match(formatStatus({ ok: true, resident: false }, { savedChars: 0, calls: 0 }), /hay cold/);
	assert.match(
		formatStatus({ ok: true, resident: true, backend: "fake (code-pruner unavailable: x)" }, {}),
		/hay degraded/,
	);
	assert.match(formatStatus({ ok: true, resident: true, backend: "code-pruner" }, {}), /hay ready/);
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
