import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createConnection } from "node:net";
import { basename, dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir, readdir, readFile, stat } from "node:fs/promises";
import { promisify } from "node:util";

const DEFAULT_TIMEOUT_MS = 30_000;
const execFileAsync = promisify(execFile);

export function appName() {
	return process.env.HAY_APP_NAME || "hay";
}

export function appHome() {
	return process.env.HAY_HOME || join(process.env.HOME || "", `.${appName()}`);
}

export function managerSocketPath() {
	return process.env.HAY_MANAGER_SOCKET || join(appHome(), "manager.sock");
}

export function eventsPath() {
	return process.env.HAY_EVENTS || join(appHome(), "events.jsonl");
}

export function repoRootFromModuleUrl(moduleUrl) {
	return join(dirname(fileURLToPath(moduleUrl)), "..", "..");
}

export function pathFromModuleUrl(moduleUrl) {
	return fileURLToPath(moduleUrl);
}

export async function request(req, options = {}) {
	const socketPath = options.socketPath || managerSocketPath();
	const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const signal = options.signal;
	return new Promise((resolve, reject) => {
		let settled = false;
		let buf = "";
		const socket = createConnection(socketPath);
		const finish = (err, value) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal?.removeEventListener("abort", onAbort);
			socket.destroy();
			err ? reject(err) : resolve(value);
		};
		const onAbort = () => finish(new Error("aborted"));
		const timer = setTimeout(() => finish(new Error("timeout")), timeoutMs);
		signal?.addEventListener("abort", onAbort, { once: true });
		socket.setEncoding("utf8");
		socket.on("connect", () => socket.write(`${JSON.stringify(req)}\n`));
		socket.on("data", (chunk) => {
			buf += chunk;
			const idx = buf.indexOf("\n");
			if (idx < 0) return;
			try {
				finish(null, JSON.parse(buf.slice(0, idx)));
			} catch (err) {
				finish(err);
			}
		});
		socket.on("error", (err) => finish(err));
		socket.on("end", () => {
			if (!settled) finish(new Error("no response from manager"));
		});
	});
}

export async function prune(text, query = "", options = {}) {
	return request({ op: "prune", text, query }, options);
}

export async function lease(session, version = "", options = {}) {
	return request({ op: "lease", session, version }, { timeoutMs: 5_000, ...options });
}

export async function heartbeat(session, options = {}) {
	return request({ op: "heartbeat", session }, { timeoutMs: 5_000, ...options });
}

export async function release(session, options = {}) {
	return request({ op: "release", session }, { timeoutMs: 5_000, ...options });
}

export async function stats(options = {}) {
	return request({ op: "stats" }, { timeoutMs: 5_000, ...options });
}

export async function tailEvents(count = 20, options = {}) {
	const n = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 20;
	if (n === 0) return [];
	let text;
	try {
		text = await readFile(options.path || eventsPath(), "utf8");
	} catch {
		return [];
	}
	const lines = text.split(/\r?\n/).filter(Boolean).slice(-n);
	const out = [];
	for (const line of lines) {
		try {
			out.push(JSON.parse(line));
		} catch {
			// ignore corrupt partial lines
		}
	}
	return out;
}

export async function sourceIdentity(repoRoot, options = {}) {
	const identity = {
		repoRoot,
		packagePath: join(repoRoot, "package.json"),
		packageName: null,
		packageVersion: null,
		pyprojectVersion: null,
		git: { available: false, reason: "not checked" },
	};
	try {
		const pkg = JSON.parse(await readFile(identity.packagePath, "utf8"));
		identity.packageName = typeof pkg.name === "string" ? pkg.name : null;
		identity.packageVersion = typeof pkg.version === "string" ? pkg.version : null;
	} catch {
		// The extension can be loaded as a single file; package metadata is optional.
	}
	try {
		const pyproject = await readFile(join(repoRoot, "pyproject.toml"), "utf8");
		const match = pyproject.match(/^\s*version\s*=\s*"([^"]+)"/m);
		identity.pyprojectVersion = match?.[1] || null;
	} catch {
		// Optional outside a source checkout.
	}
	identity.git = await gitIdentity(repoRoot, options);
	return identity;
}

async function gitIdentity(repoRoot, options = {}) {
	const timeout = options.timeoutMs ?? 500;
	try {
		const inside = await runGit(repoRoot, ["rev-parse", "--is-inside-work-tree"], timeout);
		if (inside.trim() !== "true") return { available: false, reason: "not a git checkout" };
	} catch {
		return { available: false, reason: "not a git checkout" };
	}
	try {
		const [branchRaw, commitRaw, statusRaw] = await Promise.all([
			runGit(repoRoot, ["rev-parse", "--abbrev-ref", "HEAD"], timeout),
			runGit(repoRoot, ["rev-parse", "--short=12", "HEAD"], timeout),
			runGit(repoRoot, ["status", "--porcelain"], timeout),
		]);
		const dirtyFiles = statusRaw.split(/\r?\n/).filter(Boolean).length;
		return {
			available: true,
			branch: branchRaw.trim() || "unknown",
			commit: commitRaw.trim() || "unknown",
			dirty: dirtyFiles > 0,
			dirtyFiles,
		};
	} catch (err) {
		return { available: false, reason: err?.message || "git probe failed" };
	}
}

async function runGit(repoRoot, args, timeoutMs) {
	const { stdout } = await execFileAsync("git", ["-C", repoRoot, ...args], {
		encoding: "utf8",
		timeout: timeoutMs,
		maxBuffer: 64 * 1024,
	});
	return stdout;
}

export async function socketIsLive(socketPath = managerSocketPath()) {
	try {
		await request({ op: "stats" }, { socketPath, timeoutMs: 500 });
		return true;
	} catch {
		return false;
	}
}

export async function ensureManager(options = {}) {
	const socketPath = options.socketPath || managerSocketPath();
	if (await socketIsLive(socketPath)) return true;
	const repoRoot = options.repoRoot;
	if (!repoRoot) throw new Error("repoRoot is required to spawn the manager");
	const env = {
		...process.env,
		HAY_BACKEND: process.env.HAY_BACKEND || "code-pruner",
	};
	const child = spawn("uv", ["run", "-m", "pruner", "manage"], {
		cwd: repoRoot,
		env,
		detached: true,
		stdio: "ignore",
	});
	child.unref();
	const timeoutMs = options.timeoutMs ?? 10_000;
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await socketIsLive(socketPath)) return true;
		await sleep(100);
	}
	return false;
}

export async function codeVersion(repoRoot) {
	const prunerRoot = join(repoRoot, "pruner");
	const files = (await walkPython(prunerRoot)).sort((a, b) =>
		relative(prunerRoot, a).localeCompare(relative(prunerRoot, b)),
	);
	const hash = createHash("sha1");
	for (const file of files) {
		hash.update(basename(file));
		hash.update(await readFile(file));
	}
	return hash.digest("hex").slice(0, 12);
}

export async function acquireLease(sessionId, version, options = {}) {
	const attempts = options.attempts ?? 4;
	for (let i = 0; i < attempts; i++) {
		try {
			const resp = await lease(sessionId, version, options);
			if (resp.ok) return true;
			if (resp.stale) {
				await waitForSocketDown(options.socketPath || managerSocketPath(), 10_000);
				await ensureManager(options);
				continue;
			}
			return false;
		} catch {
			if (!(await ensureManager(options))) return false;
		}
	}
	return false;
}

async function waitForSocketDown(socketPath, timeoutMs) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (!(await socketIsLive(socketPath))) return true;
		await sleep(100);
	}
	return false;
}

async function walkPython(root) {
	const out = [];
	async function visit(dir) {
		let entries;
		try {
			entries = await readdir(dir, { withFileTypes: true });
		} catch {
			return;
		}
		for (const entry of entries) {
			if (entry.name === "__pycache__") continue;
			const path = join(dir, entry.name);
			if (entry.isDirectory()) {
				await visit(path);
			} else if (entry.isFile() && entry.name.endsWith(".py")) {
				out.push(path);
			} else if (entry.isSymbolicLink()) {
				try {
					const s = await stat(path);
					if (s.isFile() && entry.name.endsWith(".py")) out.push(path);
				} catch {
					// ignore broken links
				}
			}
		}
	}
	await visit(root);
	return out;
}

function sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function ensureAppHome() {
	await mkdir(appHome(), { recursive: true });
}
