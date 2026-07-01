import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { chmod, cp, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
	acquireLease,
	activePackageId,
	appHome,
	backendIdentity,
	codeVersion,
	ensureManager,
	expandUserPath,
	managerSocketPath,
	managerTokenPath,
	packageIdentity,
	packageInventory,
	prune,
	readManagerToken,
	request,
	runtimeLaunchPlan,
	socketIsLive,
	sourceIdentity,
	stats,
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

const TEST_TOKEN = "test-manager-token";

async function writeManagerToken(socketPath, token = TEST_TOKEN) {
	await writeFile(managerTokenPath(socketPath), `${token}\n`);
}

function assertToken(req, token = TEST_TOKEN) {
	assert.equal(req.token, token);
}

function createJsonSocketServer(socketPath, handler) {
	return createServer((conn) => {
		let buf = "";
		conn.setEncoding("utf8");
		conn.on("data", async (chunk) => {
			buf += chunk;
			const idx = buf.indexOf("\n");
			if (idx < 0) return;
			const req = JSON.parse(buf.slice(0, idx));
			try {
				await handler(req, conn);
			} catch (err) {
				conn.end(JSON.stringify({ ok: false, error: err?.message || String(err) }) + "\n");
			}
		});
	});
}

async function listen(server, socketPath) {
	await new Promise((resolve, reject) => {
		server.once("error", reject);
		server.listen(socketPath, () => {
			server.off("error", reject);
			resolve();
		});
	});
}

async function closeServer(server) {
	await new Promise((resolve) => server.close(resolve));
}

async function startPythonManager(socketPath, options = {}) {
	const script = `
import json
import sys
import threading
from pathlib import Path

from needle.runtime.backends import FakePruner
from needle.runtime.manager import serve_manager

sock = Path(sys.argv[1])
identity = json.loads(sys.argv[2])
ready = threading.Event()
stop = threading.Event()
kwargs = {
    "backend_factory": FakePruner,
    "socket_path": sock,
    "ready_cb": lambda _path: ready.set(),
    "stop_event": stop,
    "poll_interval": 0.03,
}
if identity:
    kwargs["runtime_identity"] = identity
thread = threading.Thread(target=serve_manager, kwargs=kwargs, daemon=True)
thread.start()
if not ready.wait(5):
    raise SystemExit("manager did not start")
print("READY", flush=True)
sys.stdin.readline()
stop.set()
thread.join(timeout=2)
`;
	const proc = spawn(process.env.PYTHON || "python3", ["-c", script, socketPath, JSON.stringify(options.runtimeIdentity || {})], {
		cwd: process.cwd(),
		env: {
			...process.env,
			PYTHONPATH: join(process.cwd(), "src"),
			NEEDLE_NO_EVENTS: "1",
			HAY_NO_EVENTS: "1",
		},
		stdio: ["pipe", "pipe", "pipe"],
	});
	proc.stdout.setEncoding("utf8");
	proc.stderr.setEncoding("utf8");
	let stderr = "";
	proc.stderr.on("data", (chunk) => {
		stderr += chunk;
	});
	await new Promise((resolve, reject) => {
		let stdout = "";
		proc.stdout.on("data", (chunk) => {
			stdout += chunk;
			if (stdout.includes("READY")) resolve();
		});
		proc.on("exit", (code) => {
			reject(new Error(`python manager exited before ready (${code}): ${stderr}`));
		});
	});
	return {
		proc,
		async stop() {
			if (proc.exitCode !== null) return;
			proc.stdin.end("\n");
			await new Promise((resolve) => {
				const timer = setTimeout(() => {
					proc.kill("SIGKILL");
					resolve();
				}, 2_000);
				proc.on("exit", () => {
					clearTimeout(timer);
					resolve();
				});
			});
		},
	};
}

test("Pi client speaks Needle newline JSON protocol", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-"));
	const socketPath = join(dir, "manager.sock");
	await writeManagerToken(socketPath);
	const server = createJsonSocketServer(socketPath, (req, conn) => {
		assertToken(req);
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
	await listen(server, socketPath);
	try {
		assert.equal(await socketIsLive(socketPath), true);
		const req = { op: "prune", text: "abcdefghij", query: "letters" };
		const resp = await request(req, { socketPath });
		assert.equal(resp.text, "abcde");
		assert.equal("token" in req, false);
	} finally {
		await closeServer(server);
	}
});

test("Pi client authenticates to real Python manager for stats and prune", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-real-manager-"));
	const socketPath = join(dir, "manager.sock");
	const manager = await startPythonManager(socketPath);
	try {
		assert.equal(await socketIsLive(socketPath), true);
		const statsResp = await stats({ socketPath });
		assert.equal(statsResp.ok, true, JSON.stringify(statsResp));
		const pruneResp = await prune("abcdefghij", "letters", { socketPath });
		assert.equal(pruneResp.ok, true, JSON.stringify(pruneResp));
		assert.notEqual(pruneResp.error, "unauthorized");
		assert.equal(pruneResp.backend, "fake");
		assert.equal(pruneResp.text, "abcdefghij");
	} finally {
		await manager.stop();
	}
});

test("Pi client mirrors manager token path precedence", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-token-paths-"));
	const oldNeedleHome = process.env.NEEDLE_HOME;
	const oldHayHome = process.env.HAY_HOME;
	const oldNeedleSocket = process.env.NEEDLE_MANAGER_SOCKET;
	const oldHaySocket = process.env.HAY_MANAGER_SOCKET;
	const oldNeedleToken = process.env.NEEDLE_MANAGER_TOKEN_FILE;
	const oldHayToken = process.env.HAY_MANAGER_TOKEN_FILE;
	try {
		process.env.NEEDLE_HOME = join(dir, "home");
		delete process.env.HAY_HOME;
		delete process.env.NEEDLE_MANAGER_SOCKET;
		delete process.env.HAY_MANAGER_SOCKET;
		delete process.env.NEEDLE_MANAGER_TOKEN_FILE;
		delete process.env.HAY_MANAGER_TOKEN_FILE;
		assert.equal(managerTokenPath(), join(dir, "home", "manager.token"));
		const explicitSocket = join(dir, "custom.sock");
		assert.equal(managerTokenPath(explicitSocket), join(dir, "custom.sock.token"));
		process.env.HAY_MANAGER_TOKEN_FILE = join(dir, "legacy.token");
		assert.equal(managerTokenPath(explicitSocket), join(dir, "legacy.token"));
		process.env.NEEDLE_MANAGER_TOKEN_FILE = join(dir, "needle.token");
		assert.equal(managerTokenPath(explicitSocket), join(dir, "needle.token"));
	} finally {
		if (oldNeedleHome === undefined) {
			delete process.env.NEEDLE_HOME;
		} else {
			process.env.NEEDLE_HOME = oldNeedleHome;
		}
		if (oldHayHome === undefined) {
			delete process.env.HAY_HOME;
		} else {
			process.env.HAY_HOME = oldHayHome;
		}
		if (oldNeedleSocket === undefined) {
			delete process.env.NEEDLE_MANAGER_SOCKET;
		} else {
			process.env.NEEDLE_MANAGER_SOCKET = oldNeedleSocket;
		}
		if (oldHaySocket === undefined) {
			delete process.env.HAY_MANAGER_SOCKET;
		} else {
			process.env.HAY_MANAGER_SOCKET = oldHaySocket;
		}
		if (oldNeedleToken === undefined) {
			delete process.env.NEEDLE_MANAGER_TOKEN_FILE;
		} else {
			process.env.NEEDLE_MANAGER_TOKEN_FILE = oldNeedleToken;
		}
		if (oldHayToken === undefined) {
			delete process.env.HAY_MANAGER_TOKEN_FILE;
		} else {
			process.env.HAY_MANAGER_TOKEN_FILE = oldHayToken;
		}
	}
});

test("Pi runtime paths expand ~ like Python naming helpers", () => {
	const dir = spawnSync("mktemp", ["-d", `${tmpdir()}/hay-pi-home-XXXXXX`], { encoding: "utf8" }).stdout.trim();
	const home = join(dir, "home");
	const env = {
		...process.env,
		HOME: home,
		NEEDLE_HOME: "~/needle-home",
		NEEDLE_MANAGER_SOCKET: "~/needle.sock",
		NEEDLE_MANAGER_TOKEN_FILE: "~/needle.token",
		PYTHONPATH: "src",
	};
	const py = spawnSync(
		process.env.PYTHON || "python3",
		[
			"-c",
			"import json; from needle.runtime import naming; print(json.dumps({'appHome': str(naming.app_home()), 'socket': str(naming.manager_socket_path()), 'token': str(naming.manager_token_path())}))",
		],
		{ cwd: process.cwd(), env, encoding: "utf8" },
	);
	assert.equal(py.status, 0, py.stderr);
	const pythonPaths = JSON.parse(py.stdout);
	const oldHome = process.env.HOME;
	const oldNeedleHome = process.env.NEEDLE_HOME;
	const oldHayHome = process.env.HAY_HOME;
	const oldNeedleSocket = process.env.NEEDLE_MANAGER_SOCKET;
	const oldHaySocket = process.env.HAY_MANAGER_SOCKET;
	const oldNeedleToken = process.env.NEEDLE_MANAGER_TOKEN_FILE;
	const oldHayToken = process.env.HAY_MANAGER_TOKEN_FILE;
	try {
		process.env.HOME = home;
		process.env.NEEDLE_HOME = "~/needle-home";
		process.env.NEEDLE_MANAGER_SOCKET = "~/needle.sock";
		process.env.NEEDLE_MANAGER_TOKEN_FILE = "~/needle.token";
		delete process.env.HAY_HOME;
		delete process.env.HAY_MANAGER_SOCKET;
		delete process.env.HAY_MANAGER_TOKEN_FILE;
		assert.equal(appHome(), pythonPaths.appHome);
		assert.equal(managerSocketPath(), pythonPaths.socket);
		assert.equal(managerTokenPath(), pythonPaths.token);
		assert.equal(expandUserPath("~/x"), join(home, "x"));
	} finally {
		if (oldHome === undefined) {
			delete process.env.HOME;
		} else {
			process.env.HOME = oldHome;
		}
		if (oldNeedleHome === undefined) {
			delete process.env.NEEDLE_HOME;
		} else {
			process.env.NEEDLE_HOME = oldNeedleHome;
		}
		if (oldHayHome === undefined) {
			delete process.env.HAY_HOME;
		} else {
			process.env.HAY_HOME = oldHayHome;
		}
		if (oldNeedleSocket === undefined) {
			delete process.env.NEEDLE_MANAGER_SOCKET;
		} else {
			process.env.NEEDLE_MANAGER_SOCKET = oldNeedleSocket;
		}
		if (oldHaySocket === undefined) {
			delete process.env.HAY_MANAGER_SOCKET;
		} else {
			process.env.HAY_MANAGER_SOCKET = oldHaySocket;
		}
		if (oldNeedleToken === undefined) {
			delete process.env.NEEDLE_MANAGER_TOKEN_FILE;
		} else {
			process.env.NEEDLE_MANAGER_TOKEN_FILE = oldNeedleToken;
		}
		if (oldHayToken === undefined) {
			delete process.env.HAY_MANAGER_TOKEN_FILE;
		} else {
			process.env.HAY_MANAGER_TOKEN_FILE = oldHayToken;
		}
	}
});

test("Pi client rejects unsafe manager token files before reading", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-unsafe-token-"));
	const socketPath = join(dir, "manager.sock");
	const tokenPath = managerTokenPath(socketPath);
	const target = join(dir, "target.token");
	await writeFile(target, `${TEST_TOKEN}\n`);
	await symlink(target, tokenPath);
	await assert.rejects(() => readManagerToken(socketPath), /symlink/);

	await rm(tokenPath);
	await mkdir(tokenPath);
	await assert.rejects(() => readManagerToken(socketPath), /regular file/);
	await rm(tokenPath, { recursive: true });

	const unsafeDir = join(dir, "unsafe-parent");
	await mkdir(unsafeDir);
	await chmod(unsafeDir, 0o777);
	const unsafeSocketPath = join(unsafeDir, "manager.sock");
	await writeFile(managerTokenPath(unsafeSocketPath), `${TEST_TOKEN}\n`);
	try {
		await assert.rejects(() => readManagerToken(unsafeSocketPath), /other-writable/);
	} finally {
		await chmod(unsafeDir, 0o700);
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
	await writeManagerToken(socketPath);
	const server = createJsonSocketServer(socketPath, (req, conn) => {
		assertToken(req);
		ops.push(req.op);
		if (req.op === "stats") {
			conn.end(JSON.stringify({
				ok: true,
				resident: true,
				backend: "mock",
				package_id: "e24z/mlx-pi-soft-lamr",
				host_binding: "pi/native-tools",
				backend_id: "e24z/code-pruner-mlx",
				runtime_profile: "local_mlx_adaptive",
			}) + "\n");
		} else if (req.op === "lease") {
			assert.equal(req.package_id, "e24z/mlx-pi-soft-lamr");
			assert.equal(req.host_binding, "pi/native-tools");
			assert.equal(req.backend_id, "e24z/code-pruner-mlx");
			assert.equal(req.runtime_profile, "local_mlx_adaptive");
			conn.end(JSON.stringify({ ok: true }) + "\n");
		} else if (req.op === "heartbeat" || req.op === "release") {
			conn.end(JSON.stringify({ ok: true }) + "\n");
		} else if (req.op === "prune") {
			conn.end(JSON.stringify({ ok: true, text: req.text.slice(0, 300) }) + "\n");
		} else {
			conn.end(JSON.stringify({ ok: false, error: "unexpected op" }) + "\n");
		}
	});
	await listen(server, socketPath);

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
		await closeServer(server);
	}
});

test("Pi extension reports failed lease without starting runtime loops", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-lease-fail-"));
	const socketPath = join(dir, "manager.sock");
	const ops = [];
	await writeManagerToken(socketPath);
	const server = createJsonSocketServer(socketPath, (req, conn) => {
		assertToken(req);
		ops.push(req.op);
		if (req.op === "lease") {
			conn.end(JSON.stringify({ ok: false, error: "refused" }) + "\n");
			return;
		}
		conn.end(JSON.stringify({ ok: true, resident: true, backend: "mock" }) + "\n");
	});
	await listen(server, socketPath);

	const oldSocket = process.env.HAY_MANAGER_SOCKET;
	process.env.HAY_MANAGER_SOCKET = socketPath;
	try {
		const handlers = new Map();
		const statuses = [];
		const pi = {
			on: (event, handler) => handlers.set(event, handler),
			registerCommand() {},
			registerTool() {},
		};
		installNeedlePiExtension(pi, {
			createReadTool: () => ({ name: "read", parameters: {}, async execute() {} }),
			createBashTool: () => ({ name: "bash", parameters: {}, async execute() {} }),
		});
		const ctx = {
			sessionManager: { getSessionId: () => "lease-fail", getEntries: () => [] },
			ui: { setStatus: (key, text) => statuses.push([key, text]), theme: { fg: (_name, text) => text } },
		};
		await handlers.get("session_start")({}, ctx);
		await new Promise((resolve) => setTimeout(resolve, 650));
		await handlers.get("session_shutdown")({}, ctx);
		assert.deepEqual(ops, ["lease"]);
		assert.equal(statuses.at(-1)[0], "needle");
		assert.match(statuses.at(-1)[1], /needle · 0 chars trimmed · 0 prunes/);
	} finally {
		if (oldSocket === undefined) {
			delete process.env.HAY_MANAGER_SOCKET;
		} else {
			process.env.HAY_MANAGER_SOCKET = oldSocket;
		}
		await closeServer(server);
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
			package_id: "e24z/mlx-pi-reference",
			host_binding: "pi/native-tools",
			runtime_profile: "local_mlx_adaptive",
			backend_id: "e24z/code-pruner-mlx",
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
	assert.match(rendered, /package e24z\/mlx-pi-reference  \|  host pi\/native-tools/);
	assert.match(rendered, /profile local_mlx_adaptive  \|  backend-id e24z\/code-pruner-mlx/);
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
	assert.equal(plan.hostBinding, "pi/native-tools");
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
	assert.deepEqual(spawned[0].args, [
		"runtime",
		"manage",
		"--package",
		"e24z/mlx-pi-soft-lamr",
		"--host-binding",
		"pi/native-tools",
	]);
	assert.equal(spawned[0].options.env.NEEDLE_BACKEND, "e24z/code-pruner-mlx");
	assert.equal(spawned[0].options.env.HAY_BACKEND, "code-pruner");
	assert.equal(spawned[0].options.env.NEEDLE_MLX_PROFILE, "local_adaptive");
	assert.equal(spawned[0].options.env.NEEDLE_MLX_MAX_BATCH_SIZE, "1");
});

test("Pi ensureManager refuses unauthorized live manager as unusable", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-unauthorized-"));
	const socketPath = join(dir, "manager.sock");
	await writeManagerToken(socketPath);
	const plan = await runtimeLaunchPlan(process.cwd(), { hostBinding: "pi/native-tools" });
	const server = createJsonSocketServer(socketPath, (req, conn) => {
		assertToken(req);
		assert.equal(req.op, "stats");
		conn.end(JSON.stringify({ ok: false, error: "unauthorized" }) + "\n");
	});
	await listen(server, socketPath);
	let spawned = 0;
	try {
		const ok = await ensureManager({
			repoRoot: process.cwd(),
			socketPath,
			launchPlan: plan,
			spawn() {
				spawned += 1;
				return { unref() {} };
			},
		});
		assert.equal(ok, false);
		assert.equal(spawned, 0);
	} finally {
		await closeServer(server);
	}
});

test("Pi ensureManager refuses a live manager when the token file is missing", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-missing-token-"));
	const socketPath = join(dir, "manager.sock");
	const plan = await runtimeLaunchPlan(process.cwd(), { hostBinding: "pi/native-tools" });
	const server = createJsonSocketServer(socketPath, (_req, conn) => {
		conn.end(JSON.stringify({ ok: true }) + "\n");
	});
	await listen(server, socketPath);
	let spawned = 0;
	try {
		const ok = await ensureManager({
			repoRoot: process.cwd(),
			socketPath,
			launchPlan: plan,
			spawn() {
				spawned += 1;
				return { unref() {} };
			},
		});
		assert.equal(ok, false);
		assert.equal(spawned, 0);
	} finally {
		await closeServer(server);
	}
});

test("Pi client refuses unsafe manager sockets", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-unsafe-socket-"));
	const socketPath = join(dir, "manager.sock");
	await writeFile(socketPath, "not a socket");
	await writeManagerToken(socketPath);
	assert.equal(await socketIsLive(socketPath), false);
	await assert.rejects(
		() => request({ op: "stats" }, { socketPath, timeoutMs: 100 }),
		/manager socket is not safe/,
	);
});

test("Pi ensureManager refuses a live manager with a mismatched code version", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-version-mismatch-"));
	const socketPath = join(dir, "manager.sock");
	await writeManagerToken(socketPath);
	const plan = await runtimeLaunchPlan(process.cwd(), { hostBinding: "pi/native-tools" });
	const server = createJsonSocketServer(socketPath, (req, conn) => {
		assertToken(req);
		assert.equal(req.op, "stats");
		conn.end(JSON.stringify({
			ok: true,
			resident: false,
			version: "old-version",
			package_id: plan.packageId,
			host_binding: plan.hostBinding,
			backend_id: plan.backendId,
			runtime_profile: plan.runtimeProfile,
		}) + "\n");
	});
	await listen(server, socketPath);
	let spawned = 0;
	try {
		const ok = await ensureManager({
			repoRoot: process.cwd(),
			socketPath,
			launchPlan: plan,
			expectedVersion: "new-version",
			spawn() {
				spawned += 1;
				return { unref() {} };
			},
		});
		assert.equal(ok, false);
		assert.equal(spawned, 0);
	} finally {
		await closeServer(server);
	}
});

test("Pi request caps newline-free manager responses", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-response-cap-"));
	const socketPath = join(dir, "manager.sock");
	await writeManagerToken(socketPath);
	const server = createServer((conn) => {
		conn.setEncoding("utf8");
		conn.on("data", () => {
			conn.write("x".repeat(128));
		});
	});
	await listen(server, socketPath);
	try {
		await assert.rejects(
			() => request({ op: "stats" }, { socketPath, maxResponseBytes: 32, timeoutMs: 500 }),
			/response exceeded 32 bytes/,
		);
	} finally {
		await closeServer(server);
	}
});

test("Pi ensureManager handles spawn failures as unavailable", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-spawn-fail-"));
	const socketPath = join(dir, "manager.sock");
	const plan = await runtimeLaunchPlan(process.cwd(), { hostBinding: "pi/native-tools" });
	const thrown = await ensureManager({
		repoRoot: process.cwd(),
		socketPath,
		launchPlan: plan,
		spawn() {
			throw new Error("boom");
		},
	});
	assert.equal(thrown, false);
	const malformed = await ensureManager({
		repoRoot: process.cwd(),
		socketPath,
		launchPlan: plan,
		spawn() {
			return {};
		},
	});
	assert.equal(malformed, false);
});

test("Pi acquireLease sends runtime identity and replaces stale manager", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-pi-stale-"));
	const socketPath = join(dir, "manager.sock");
	await writeManagerToken(socketPath);
	const launchPlan = {
		packageId: "pkg/new",
		hostBinding: "pi/native-tools",
		backendId: "backend/new",
		runtimeProfile: "profile-new",
		command: ["needle", "runtime", "manage"],
		env: {},
	};
	const seen = [];
	let staleClosed;
	const staleClosedPromise = new Promise((resolve) => {
		staleClosed = resolve;
	});
	const oldServer = createJsonSocketServer(socketPath, (req, conn) => {
		assertToken(req);
		seen.push({ server: "old", req });
		assert.equal(req.op, "lease");
		assert.equal(req.package_id, "pkg/new");
		assert.equal(req.host_binding, "pi/native-tools");
		assert.equal(req.backend_id, "backend/new");
		assert.equal(req.runtime_profile, "profile-new");
		conn.end(JSON.stringify({
			ok: false,
			stale: true,
			identity_mismatch: true,
			mismatches: { package_id: { requested: "pkg/new", actual: "pkg/old" } },
		}) + "\n", () => {
			oldServer.close(async () => {
				await rm(socketPath, { force: true });
				staleClosed();
			});
		});
	});
	await listen(oldServer, socketPath);

	let replacementServer;
	const replacementReady = new Promise((resolve, reject) => {
		replacementServer = createJsonSocketServer(socketPath, (req, conn) => {
			assertToken(req);
			seen.push({ server: "new", req });
			if (req.op === "stats") {
				conn.end(JSON.stringify({
					ok: true,
					resident: false,
					package_id: "pkg/new",
					host_binding: "pi/native-tools",
					backend_id: "backend/new",
					runtime_profile: "profile-new",
				}) + "\n");
				return;
			}
			assert.equal(req.op, "lease");
			assert.equal(req.package_id, "pkg/new");
			assert.equal(req.host_binding, "pi/native-tools");
			assert.equal(req.backend_id, "backend/new");
			assert.equal(req.runtime_profile, "profile-new");
			conn.end(JSON.stringify({ ok: true }) + "\n");
		});
		replacementServer.once("error", reject);
		replacementServer.on("listening", resolve);
	});
	let spawned = 0;
	const ok = await acquireLease("session-new", "v-new", {
		repoRoot: process.cwd(),
		socketPath,
		launchPlan,
		attempts: 3,
		spawn() {
			spawned += 1;
			staleClosedPromise.then(() => {
				replacementServer.listen(socketPath);
			});
			return { unref() {} };
		},
	});
	try {
		assert.equal(ok, true);
		await replacementReady;
		assert.equal(spawned, 1);
		assert.ok(seen.some((entry) => entry.server === "old" && entry.req.op === "lease"), seen);
		assert.ok(seen.some((entry) => entry.server === "new" && entry.req.op === "stats"), seen);
		assert.ok(seen.some((entry) => entry.server === "new" && entry.req.op === "lease"), seen);
	} finally {
		if (replacementServer.listening) await closeServer(replacementServer);
		if (oldServer.listening) await closeServer(oldServer);
	}
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

test("Pi package identity rejects invalid runtime profile env", async () => {
	const dir = await mkdtemp(join(tmpdir(), "hay-registry-invalid-profile-"));
	for (const name of ["packages", "capabilities", "backends", "bindings", "claims", "package-cards", "protocols"]) {
		await cp(join(process.cwd(), "src", "needle", "registry_data", name), join(dir, name), { recursive: true });
	}
	const packagePath = join(dir, "packages", "e24z", "mlx-pi-soft-lamr.yaml");
	const originalPackage = JSON.parse(await readFile(packagePath, "utf8"));
	const cases = [
		[{ NEEDLE_MLX_MAX_BATCH_SIZE: "0" }, /NEEDLE_MLX_MAX_BATCH_SIZE.*positive integer/],
		[{ NEEDLE_THRESHOLD: "   " }, /NEEDLE_THRESHOLD.*must be a number/],
		[{ NEEDLE_THRESHOLD: "nan" }, /NEEDLE_THRESHOLD.*finite number/],
		[{ NEEDLE_MLX_MAX_LENGTH_RATIO: "inf" }, /NEEDLE_MLX_MAX_LENGTH_RATIO.*finite number/],
	];

	const oldRoot = process.env.NEEDLE_REGISTRY_ROOT;
	const oldHayRoot = process.env.HAY_REGISTRY_ROOT;
	const oldConfig = process.env.NEEDLE_CONFIG;
	const oldHayConfig = process.env.HAY_CONFIG;
	const oldPackage = process.env.NEEDLE_PACKAGE;
	const oldHayPackage = process.env.HAY_PACKAGE;
	try {
		process.env.NEEDLE_REGISTRY_ROOT = dir;
		process.env.NEEDLE_CONFIG = join(dir, "missing-config.json");
		delete process.env.HAY_REGISTRY_ROOT;
		delete process.env.HAY_CONFIG;
		delete process.env.NEEDLE_PACKAGE;
		delete process.env.HAY_PACKAGE;

		for (const [env, expected] of cases) {
			const pkg = structuredClone(originalPackage);
			pkg.runtime_profile.env = env;
			await writeFile(packagePath, JSON.stringify(pkg, null, 2));

			const identity = await packageIdentity(process.cwd(), "e24z/mlx-pi-soft-lamr");
			assert.equal(identity.available, false);
			assert.match(identity.reason, expected);
		}
	} finally {
		if (oldRoot === undefined) {
			delete process.env.NEEDLE_REGISTRY_ROOT;
		} else {
			process.env.NEEDLE_REGISTRY_ROOT = oldRoot;
		}
		if (oldHayRoot === undefined) {
			delete process.env.HAY_REGISTRY_ROOT;
		} else {
			process.env.HAY_REGISTRY_ROOT = oldHayRoot;
		}
		if (oldConfig === undefined) {
			delete process.env.NEEDLE_CONFIG;
		} else {
			process.env.NEEDLE_CONFIG = oldConfig;
		}
		if (oldHayConfig === undefined) {
			delete process.env.HAY_CONFIG;
		} else {
			process.env.HAY_CONFIG = oldHayConfig;
		}
		if (oldPackage === undefined) {
			delete process.env.NEEDLE_PACKAGE;
		} else {
			process.env.NEEDLE_PACKAGE = oldPackage;
		}
		if (oldHayPackage === undefined) {
			delete process.env.HAY_PACKAGE;
		} else {
			process.env.HAY_PACKAGE = oldHayPackage;
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

test("Pi codeVersion changes for backend-affecting files", async () => {
	const dir = await mkdtemp(join(tmpdir(), "needle-code-version-"));
	const root = join(dir, "src", "needle");
	await mkdir(join(root, "runtime"), { recursive: true });
	await mkdir(join(root, "backends"), { recursive: true });
	await mkdir(join(root, "registry_data", "backends", "e24z"), { recursive: true });
	await mkdir(join(root, "registry_data", "packages", "e24z"), { recursive: true });
	await writeFile(join(root, "runtime", "manager.py"), "runtime = 1\n");
	await writeFile(join(root, "backends", "fake.py"), "backend = 1\n");
	await writeFile(join(root, "registry.py"), "registry = 1\n");
	const backendPath = join(root, "registry_data", "backends", "e24z", "backend.yaml");
	await writeFile(join(root, "registry_data", "packages", "e24z", "pkg.yaml"), '{"id":"pkg"}\n');
	await writeFile(backendPath, '{"id":"backend","launcher":{"command":["needle"]}}\n');

	const before = await codeVersion(dir);
	await writeFile(backendPath, '{"id":"backend","launcher":{"command":["needle","runtime"]}}\n');
	const afterBackend = await codeVersion(dir);
	await writeFile(join(root, "backends", "fake.py"), "backend = 2\n");
	const afterSource = await codeVersion(dir);

	assert.notEqual(before, afterBackend);
	assert.notEqual(afterBackend, afterSource);
});
