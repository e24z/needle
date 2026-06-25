import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cp, mkdtemp, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
	activePackageId,
	backendIdentity,
	codeVersion,
	ensureManager,
	packageIdentity,
	packageInventory,
	prune,
	runtimeLaunchPlan,
	socketIsLive,
	sourceIdentity,
	tailEvents,
} from "../src/needle/hosts/pi/client.mjs";
import {
	buildBashResultPatch,
	buildPackageStatus,
	buildToolResultPatch,
	decideStatusState,
	extractFocusQuestion,
	extractText,
	formatIndicator,
	formatStatus,
	installNeedlePiExtension,
	renderPackageStatus,
	renderOperatorStatus,
} from "../src/needle/hosts/pi/extension.js";

test("Pi client speaks Needle newline JSON protocol", async () => {
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
		params: { context_focus_question: "read the config" },
		content: [{ type: "text", text: "x".repeat(1000) }],
	};
	const patch = await buildToolResultPatch(event, {}, counters, async (text, query) => {
		assert.equal(query, "read the config");
		return { ok: true, text: text.slice(0, 400) };
	});
	assert.deepEqual(patch, { content: [{ type: "text", text: "x".repeat(400) }] });
	assert.equal(counters.calls, 1);
	assert.equal(counters.savedChars, 600);
});

test("Pi adapter patches bash output through the same focus contract", async () => {
	const counters = { calls: 0, originalChars: 0, prunedChars: 0, savedChars: 0 };
	const patch = await buildBashResultPatch(
		{ content: [{ type: "text", text: "log\n".repeat(300) }], details: { exitCode: 0 } },
		{ context_focus_question: "find failing tests" },
		{},
		counters,
		async (text, query) => {
			assert.equal(query, "find failing tests");
			return { ok: true, text: text.slice(0, 100) };
		},
	);
	assert.deepEqual(patch, { content: [{ type: "text", text: "log\n".repeat(25) }] });
	assert.equal(counters.calls, 1);
	assert.equal(counters.lastTool, "bash");
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
		installNeedlePiExtension(pi, {
			createReadTool: (cwd) => ({
				name: "read",
				label: "read",
				description: "mock Pi read",
				parameters: {},
				async execute(_toolCallId, params) {
					assert.deepEqual(params, { path: "file.py" });
					return {
						content: [{ type: "text", text: "x".repeat(1000) }],
						details: { cwd },
					};
				},
			}),
			createBashTool: (cwd) => ({
				name: "bash",
				label: "bash",
				description: "mock Pi bash",
				parameters: { properties: { command: { type: "string" } } },
				async execute(_toolCallId, params) {
					assert.deepEqual(params, { command: "printf hi" });
					return {
						content: [{ type: "text", text: "hi" }],
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
		assert.equal(commands.has("needle"), true);
		assert.equal(commands.has("hay"), false);
		assert.equal(tools.has("read"), true);
		assert.equal(tools.has("bash"), true);
		assert.equal(tools.get("read").parameters.properties.context_focus_question.type, "string");
		assert.equal(tools.get("bash").parameters.properties.context_focus_question.type, "string");
		const result = await tools.get("read").execute(
			"tool-call-1",
			{ path: "file.py", context_focus_question: "summarize this file" },
			ctx.signal,
			undefined,
			ctx,
		);
		const bashResult = await tools.get("bash").execute(
			"tool-call-2",
			{ command: "printf hi", context_focus_question: "tiny output" },
			ctx.signal,
			undefined,
			ctx,
		);
		await commands.get("needle").handler("status", ctx);
		await handlers.get("session_shutdown")({}, ctx);

		assert.deepEqual(result.content, [{ type: "text", text: "x".repeat(300) }]);
		assert.equal(result.details.cwd, "/tmp/pi-cwd");
		assert.deepEqual(bashResult.content, [{ type: "text", text: "hi" }]);
		assert.equal(bashResult.details.cwd, "/tmp/pi-cwd");
		assert.equal(customEntries.length, 1);
		assert.deepEqual(customEntries[0].data.calls, 1);
		assert.equal(messages.length, 1);
		assert.equal(messages[0].customType, "needle-status");
		assert.match(messages[0].content, /needle runtime: ready \(mock resident\)/);
		assert.match(messages[0].content, /why running:/);
		assert.match(messages[0].content, /this Pi session 700 chars trimmed  \|  1 prunes/);
		assert.equal(statuses.at(-1)[0], "needle");
		assert.match(statuses.at(-1)[1], /needle · 700 chars trimmed · 1 prune/);
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
			extensionPath: "/tmp/hay/src/needle/hosts/pi/extension.js",
			socketPath: "/tmp/hay/manager.sock",
			source: {
				repoRoot: "/tmp/hay",
				packageName: "needle",
				packageVersion: "0.1.0",
				pyprojectVersion: "0.1.0",
				modelRoot: "/tmp/hay/models",
				activePackage: {
					available: true,
					id: "e24z/mlx-pi-reference",
					capabilities: ["swe-pruner/reference"],
					backend: "e24z/code-pruner-mlx",
					backendLauncher: {
						kind: "needle-cli",
						command: ["needle", "runtime", "manage"],
					},
					runtimeProfile: "local_mlx_adaptive",
					hostBinding: "pi/native-tools",
					packageCard: "package-cards/e24z/mlx-pi-reference",
					claimCard: "claims/mlx-pi-reference",
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
	assert.match(rendered, /extension \/tmp\/hay\/src\/needle\/hosts\/pi\/extension\.js/);
	assert.match(rendered, /model dir \/tmp\/hay\/models/);
	assert.match(rendered, /active package e24z\/mlx-pi-reference/);
	assert.match(rendered, /capability swe-pruner\/reference/);
	assert.match(rendered, /backend e24z\/code-pruner-mlx/);
	assert.match(rendered, /backend launch needle runtime manage/);
	assert.match(rendered, /runtime profile local_mlx_adaptive/);
	assert.match(rendered, /host binding pi\/native-tools/);
	assert.match(rendered, /compute local_mlx \| privacy local_only/);
	assert.match(rendered, /prompt bundle pi\/context-focus-question@0\.1/);
	assert.match(rendered, /package card package-cards\/e24z\/mlx-pi-reference/);
	assert.match(rendered, /claim card claims\/mlx-pi-reference/);
	assert.match(rendered, /version package needle@0\.1\.0 \| pyproject 0\.1\.0/);
	assert.match(rendered, /git pi-adapter@abcdef123456 \(dirty, 2 files\)/);
	assert.match(rendered, /passthrough\s+reason=low-memory chars=1200/);
	assert.match(renderOperatorStatus("loading", [], {}), /loading or pruning/);
	assert.match(renderOperatorStatus(null, [], {}), /fails open/);
	assert.match(formatStatus("loading", { savedChars: 0, calls: 0 }), /needle · 0 chars trimmed · 0 prunes/);
});

test("Pi source identity reads package, pyproject, git state, and active Needle package", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-empty-config-"));
	const oldConfig = process.env.HAY_CONFIG;
	const oldPackage = process.env.HAY_PACKAGE;
	const oldNeedlePackage = process.env.NEEDLE_PACKAGE;
	try {
		process.env.HAY_CONFIG = join(dir, "missing.json");
		delete process.env.HAY_PACKAGE;
		delete process.env.NEEDLE_PACKAGE;

		const identity = await sourceIdentity(process.cwd(), { timeoutMs: 1_000 });
		assert.equal(identity.packageName, "needle");
		assert.equal(identity.packageVersion, "0.1.0");
		assert.equal(identity.pyprojectVersion, "0.1.0");
		assert.match(identity.modelRoot, /\/\.needle\/models$/);
		assert.equal(identity.activePackage.id, "e24z/mlx-pi-soft-lamr");
		assert.deepEqual(identity.activePackage.capabilities, ["e24z/soft-lamr"]);
		assert.equal(identity.activePackage.backend, "e24z/code-pruner-mlx");
		assert.equal(identity.activePackage.backendRuntime, "local_manager");
		assert.equal(identity.activePackage.backendLauncher.kind, "needle-cli");
		assert.deepEqual(identity.activePackage.backendLauncher.command, ["needle", "runtime", "manage"]);
		assert.equal(identity.activePackage.runtimeProfile, "local_mlx_adaptive");
		assert.equal(identity.activePackage.runtimeProfileEnv.NEEDLE_MLX_PROFILE, "local_adaptive");
		assert.equal(identity.activePackage.runtimeProfileEnv.NEEDLE_MLX_MAX_BATCH_SIZE, "1");
		assert.equal(identity.activePackage.hostBinding, "pi/native-tools");
		assert.equal(identity.activePackage.claimCard, "claims/mlx-pi-soft-lamr");
		assert.equal(typeof identity.git.available, "boolean");
	} finally {
		if (oldConfig === undefined) {
			delete process.env.HAY_CONFIG;
		} else {
			process.env.HAY_CONFIG = oldConfig;
		}
		if (oldPackage === undefined) {
			delete process.env.HAY_PACKAGE;
		} else {
			process.env.HAY_PACKAGE = oldPackage;
		}
		if (oldNeedlePackage === undefined) {
			delete process.env.NEEDLE_PACKAGE;
		} else {
			process.env.NEEDLE_PACKAGE = oldNeedlePackage;
		}
	}

	const missing = await packageIdentity(process.cwd(), "e24z/does-not-exist");
	assert.equal(missing.available, false);
	assert.equal(missing.id, "e24z/does-not-exist");
});

test("Pi resolves backend launch plan from the active package graph", async () => {
	const backend = await backendIdentity(process.cwd(), "e24z/code-pruner-mlx");
	assert.equal(backend.available, true);
	assert.equal(backend.launcher.kind, "needle-cli");
	assert.deepEqual(backend.launcher.command, ["needle", "runtime", "manage"]);
	const httpBackend = await backendIdentity(process.cwd(), "e24z/code-pruner-http");
	assert.equal(httpBackend.available, true);
	assert.deepEqual(httpBackend.launcher.command, ["needle", "runtime", "manage"]);

	const plan = await runtimeLaunchPlan(process.cwd(), { hostBinding: "pi/native-tools" });
	assert.equal(plan.packageId, "e24z/mlx-pi-soft-lamr");
	assert.equal(plan.backendId, "e24z/code-pruner-mlx");
	assert.deepEqual(plan.command, ["needle", "runtime", "manage"]);
	assert.equal(plan.env.NEEDLE_BACKEND, "e24z/code-pruner-mlx");
	assert.equal(plan.env.HAY_BACKEND, "code-pruner");
	assert.equal(plan.runtimeProfile, "local_mlx_adaptive");
	assert.equal(plan.env.NEEDLE_MLX_PROFILE, "local_adaptive");
	assert.equal(plan.env.NEEDLE_MLX_MAX_BATCH_SIZE, "1");
});

test("Pi ensureManager spawns from backend launch metadata", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-launch-"));
	const socketPath = join(dir, "manager.sock");
	const spawned = [];
	const ok = await ensureManager({
		repoRoot: process.cwd(),
		socketPath,
		timeoutMs: 0,
		spawn(command, args, options) {
			spawned.push({ command, args, options });
			return { unref() {} };
		},
	});
	assert.equal(ok, false);
	assert.equal(spawned.length, 1);
	assert.equal(spawned[0].command, "needle");
	assert.deepEqual(spawned[0].args, ["runtime", "manage"]);
	assert.equal(spawned[0].options.env.NEEDLE_BACKEND, "e24z/code-pruner-mlx");
	assert.equal(spawned[0].options.env.HAY_BACKEND, "code-pruner");
	assert.equal(spawned[0].options.env.NEEDLE_MLX_PROFILE, "local_adaptive");
	assert.equal(spawned[0].options.env.NEEDLE_MLX_MAX_BATCH_SIZE, "1");
});

test("Pi package identity can load from an external registry root", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-registry-"));
	for (const name of ["packages", "capabilities", "backends", "bindings", "claims", "package-cards", "protocols"]) {
		await cp(join(process.cwd(), "src", "needle", "registry_data", name), join(dir, name), { recursive: true });
	}

	const oldRoot = process.env.HAY_REGISTRY_ROOT;
	const oldConfig = process.env.HAY_CONFIG;
	const oldPackage = process.env.HAY_PACKAGE;
	const oldNeedlePackage = process.env.NEEDLE_PACKAGE;
	try {
		process.env.HAY_REGISTRY_ROOT = dir;
		process.env.HAY_CONFIG = join(dir, "missing-config.json");
		delete process.env.HAY_PACKAGE;
		delete process.env.NEEDLE_PACKAGE;
		const identity = await packageIdentity(process.cwd());
		assert.equal(identity.available, true);
		assert.equal(identity.id, "e24z/mlx-pi-soft-lamr");
		assert.equal(identity.backend, "e24z/code-pruner-mlx");
		assert.equal(identity.runtimeProfile, "local_mlx_adaptive");
	} finally {
		if (oldRoot === undefined) {
			delete process.env.HAY_REGISTRY_ROOT;
		} else {
			process.env.HAY_REGISTRY_ROOT = oldRoot;
		}
		if (oldConfig === undefined) {
			delete process.env.HAY_CONFIG;
		} else {
			process.env.HAY_CONFIG = oldConfig;
		}
		if (oldPackage === undefined) {
			delete process.env.HAY_PACKAGE;
		} else {
			process.env.HAY_PACKAGE = oldPackage;
		}
		if (oldNeedlePackage === undefined) {
			delete process.env.NEEDLE_PACKAGE;
		} else {
			process.env.NEEDLE_PACKAGE = oldNeedlePackage;
		}
	}
});

test("Pi package identity reads CLI user config unless env overrides it", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-config-"));
	const configPath = join(dir, "config.json");
	await writeFile(configPath, JSON.stringify({ packages: { "pi/native-tools": "e24z/mlx-pi-soft-lamr" } }));

	const oldConfig = process.env.HAY_CONFIG;
	const oldNeedleConfig = process.env.NEEDLE_CONFIG;
	const oldPackage = process.env.HAY_PACKAGE;
	const oldNeedlePackage = process.env.NEEDLE_PACKAGE;
	try {
		process.env.NEEDLE_CONFIG = configPath;
		delete process.env.HAY_CONFIG;
		delete process.env.HAY_PACKAGE;
		delete process.env.NEEDLE_PACKAGE;

		assert.equal(await activePackageId({ hostBinding: "pi/native-tools" }), "e24z/mlx-pi-soft-lamr");
		const identity = await packageIdentity(process.cwd(), undefined, { hostBinding: "pi/native-tools" });
		assert.equal(identity.id, "e24z/mlx-pi-soft-lamr");
		assert.deepEqual(identity.capabilities, ["e24z/soft-lamr"]);

		process.env.HAY_PACKAGE = "e24z/mlx-pi-reference";
		process.env.NEEDLE_PACKAGE = "e24z/mlx-pi-soft-lamr";
		assert.equal(await activePackageId(), "e24z/mlx-pi-soft-lamr");

		delete process.env.NEEDLE_PACKAGE;
		assert.equal(await activePackageId(), "e24z/mlx-pi-reference");
	} finally {
		if (oldConfig === undefined) {
			delete process.env.HAY_CONFIG;
		} else {
			process.env.HAY_CONFIG = oldConfig;
		}
		if (oldNeedleConfig === undefined) {
			delete process.env.NEEDLE_CONFIG;
		} else {
			process.env.NEEDLE_CONFIG = oldNeedleConfig;
		}
		if (oldPackage === undefined) {
			delete process.env.HAY_PACKAGE;
		} else {
			process.env.HAY_PACKAGE = oldPackage;
		}
		if (oldNeedlePackage === undefined) {
			delete process.env.NEEDLE_PACKAGE;
		} else {
			process.env.NEEDLE_PACKAGE = oldNeedlePackage;
		}
	}
});

test("Pi package inventory lists reference and Soft-LaMR packages", async () => {
	const oldPackage = process.env.HAY_PACKAGE;
	const oldNeedlePackage = process.env.NEEDLE_PACKAGE;
	try {
		delete process.env.HAY_PACKAGE;
		process.env.NEEDLE_PACKAGE = "e24z/mlx-pi-soft-lamr";
		const packages = await packageInventory(process.cwd());
		const ids = packages.map((pkg) => pkg.id);
		assert.ok(ids.includes("e24z/mlx-pi-reference"), ids);
		assert.ok(ids.includes("e24z/mlx-pi-soft-lamr"), ids);
		assert.equal(packages.find((pkg) => pkg.id === "e24z/mlx-pi-soft-lamr").active, true);
		assert.deepEqual(
			packages.find((pkg) => pkg.id === "e24z/mlx-pi-soft-lamr").capabilities,
			["e24z/soft-lamr"],
		);
		assert.equal(
			packages.find((pkg) => pkg.id === "e24z/mlx-pi-soft-lamr").runtimeProfile,
			"local_mlx_adaptive",
		);
	} finally {
		if (oldPackage === undefined) {
			delete process.env.HAY_PACKAGE;
		} else {
			process.env.HAY_PACKAGE = oldPackage;
		}
		if (oldNeedlePackage === undefined) {
			delete process.env.NEEDLE_PACKAGE;
		} else {
			process.env.NEEDLE_PACKAGE = oldNeedlePackage;
		}
	}
});

test("Pi package inventory can filter to Pi host packages", async () => {
	const packages = await packageInventory(process.cwd(), { hostBinding: "pi/native-tools" });
	const ids = packages.map((pkg) => pkg.id);
	assert.ok(ids.includes("e24z/mlx-pi-reference"), ids);
	assert.ok(ids.includes("e24z/mlx-pi-soft-lamr"), ids);
	assert.ok(!ids.includes("e24z/mlx-mcp-bash-reference"), ids);
});

test("Pi package status explains package selection", async () => {
	const rendered = renderPackageStatus([
		{
			available: true,
			active: true,
			id: "e24z/mlx-pi-reference",
			capabilities: ["swe-pruner/reference"],
			backend: "e24z/code-pruner-mlx",
			runtimeProfile: "local_mlx_adaptive",
		},
		{
			available: true,
			active: false,
			id: "e24z/mlx-pi-soft-lamr",
			capabilities: ["e24z/soft-lamr"],
			backend: "e24z/code-pruner-mlx",
			runtimeProfile: "local_mlx_adaptive",
		},
	]);
	assert.match(rendered, /\[active\] e24z\/mlx-pi-reference/);
	assert.match(rendered, /\[ \] e24z\/mlx-pi-soft-lamr/);
	assert.match(rendered, /no AST repair/);
	assert.match(rendered, /python AST repair/);
	assert.match(rendered, /runtime profile local_mlx_adaptive/);
	assert.match(rendered, /needle packages:/);
	assert.match(rendered, /needle package use <package-id>/);
	assert.match(rendered, /NEEDLE_PACKAGE=<package-id> pi/);

	const live = await buildPackageStatus(process.cwd());
	assert.match(live, /e24z\/mlx-pi-reference/);
	assert.match(live, /e24z\/mlx-pi-soft-lamr/);
	assert.doesNotMatch(live, /e24z\/mlx-mcp-bash-reference/);
});

test("Pi demo canary prints proof report", () => {
	const result = spawnSync(process.execPath, ["src/needle/hosts/pi/demo-canary.mjs"], {
		cwd: process.cwd(),
		encoding: "utf8",
	});
	assert.equal(result.status, 0, result.stderr);
	assert.match(result.stdout, /Needle Pi demo canary/);
	assert.match(result.stdout, /total chars trimmed:/);
	assert.match(result.stdout, /prunes accepted: 2/);
	assert.match(result.stdout, /This proves the Pi extension path/);
});

test("Pi client reads the local Needle event log", async () => {
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
			{
				toolName: "read",
				params: { context_focus_question: "focus" },
				content: [{ type: "text", text: "short" }],
			},
			{},
			counters,
			async () => ({ ok: true, text: "" }),
		),
		undefined,
	);
	assert.equal(
		await buildToolResultPatch(
			{
				toolName: "read",
				params: { context_focus_question: "focus" },
				content: [{ type: "text", text: "x".repeat(1000) }],
			},
			{},
			counters,
			async (text) => ({ ok: true, text }),
		),
		undefined,
	);
});

test("Pi pruning requires explicit context_focus_question", async () => {
	assert.equal(extractFocusQuestion({ context_focus_question: "  inspect imports  " }), "inspect imports");
	assert.equal(extractFocusQuestion({}), "");
	let called = false;
	const patch = await buildToolResultPatch(
		{ toolName: "read", params: {}, content: [{ type: "text", text: "x".repeat(1000) }] },
		{},
		{ calls: 0, originalChars: 0, prunedChars: 0, savedChars: 0 },
		async () => {
			called = true;
			return { ok: true, text: "" };
		},
	);
	assert.equal(patch, undefined);
	assert.equal(called, false);
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
		"ready",
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
	assert.match(formatStatus(null, { savedChars: 400, calls: 1 }, undefined, { columns: 100 }), /^.+ needle · 400 chars trimmed · 1 prune$/);
	assert.match(formatStatus({ ok: true, resident: false }, { savedChars: 0, calls: 0 }, undefined, { columns: 100 }), /needle · 0 chars trimmed · 0 prunes/);
	assert.match(
		formatStatus({ ok: true, resident: true, backend: "code-pruner" }, { calls: 12, savedChars: 4096 }, undefined, {
			columns: 12,
		}),
		/^.+ needle$/,
	);
});

test("Pi codeVersion matches the Python engine hash", async () => {
	const jsVersion = await codeVersion(process.cwd());
	const py = spawnSync("python3", ["-c", "from needle.runtime.naming import code_version; print(code_version())"], {
		cwd: process.cwd(),
		env: { ...process.env, PYTHONPATH: "src" },
		encoding: "utf8",
	});
	assert.equal(py.status, 0, py.stderr);
	assert.equal(jsVersion, py.stdout.trim());
});
