import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { createConnection } from "node:net";
import { basename, dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir, readdir, readFile, stat } from "node:fs/promises";
import { promisify } from "node:util";

const DEFAULT_TIMEOUT_MS = 30_000;
const execFileAsync = promisify(execFile);
const PI_HOST_BINDING = "pi/native-tools";
const DEFAULT_PACKAGE_ID = "e24z/mlx-pi-soft-lamr";
// Transitional mirror of needle.runtime.naming while the Pi adapter runs in Node.
const PACKAGE_ALIASES = new Map([
	["e24z/pi-local-mac", "e24z/mlx-pi-reference"],
	["e24z/pi-local-mac-soft-lamr", "e24z/mlx-pi-soft-lamr"],
	["e24z/mcp-bash-local", "e24z/mlx-mcp-bash-reference"],
]);

export function canonicalPackageId(packageId) {
	return PACKAGE_ALIASES.get(packageId) || packageId;
}

export function appName() {
	return process.env.NEEDLE_APP_NAME || process.env.HAY_APP_NAME || "needle";
}

export function appHome() {
	return process.env.NEEDLE_HOME || process.env.HAY_HOME || join(process.env.HOME || "", `.${appName()}`);
}

export function packageConfigPath() {
	return process.env.NEEDLE_CONFIG || process.env.HAY_CONFIG || join(appHome(), "config.json");
}

export function modelRoot() {
	return process.env.NEEDLE_MODEL_ROOT || process.env.HAY_MODEL_ROOT || join(appHome(), "models");
}

export function managerSocketPath() {
	return process.env.NEEDLE_MANAGER_SOCKET || process.env.HAY_MANAGER_SOCKET || join(appHome(), "manager.sock");
}

export function eventsPath() {
	return process.env.NEEDLE_EVENTS || process.env.HAY_EVENTS || join(appHome(), "events.jsonl");
}

export function repoRootFromModuleUrl(moduleUrl) {
	return join(dirname(fileURLToPath(moduleUrl)), "..", "..", "..");
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
		modelRoot: modelRoot(),
		activePackage: null,
		git: { available: false, reason: "not checked" },
	};
	for (const packagePath of [
		join(repoRoot, "package.json"),
		join(repoRoot, "needle", "hosts", "pi", "package.json"),
		join(repoRoot, "hosts", "pi", "package.json"),
	]) {
		try {
			const pkg = JSON.parse(await readFile(packagePath, "utf8"));
			identity.packagePath = packagePath;
			identity.packageName = typeof pkg.name === "string" ? pkg.name : null;
			identity.packageVersion = typeof pkg.version === "string" ? pkg.version : null;
			break;
		} catch {
			// Try the next packaged/source metadata location.
		}
	}
	try {
		const pyproject = await readFile(join(repoRoot, "pyproject.toml"), "utf8");
		const match = pyproject.match(/^\s*version\s*=\s*"([^"]+)"/m);
		identity.pyprojectVersion = match?.[1] || null;
	} catch {
		// Optional outside a source checkout.
	}
	identity.activePackage = await packageIdentity(repoRoot, undefined, { hostBinding: PI_HOST_BINDING });
	identity.git = await gitIdentity(repoRoot, options);
	return identity;
}

export async function packageIdentity(
	repoRoot,
	packageId = undefined,
	options = {},
) {
	packageId = canonicalPackageId(packageId || (await activePackageId({ hostBinding: options.hostBinding })));
	try {
		const root = registryRoot(repoRoot);
		const pkg = await readRegistryJson(root, "packages", packageId);
		const hostBinding = typeof pkg.host_binding === "string" ? pkg.host_binding : null;
		if (options.hostBinding && hostBinding !== options.hostBinding) {
			return {
				available: false,
				id: packageId,
				reason: `package is bound to ${hostBinding || "unknown"}, not ${options.hostBinding}`,
				hostBinding,
			};
		}
		const backendId = typeof pkg.uses?.backend === "string" ? pkg.uses.backend : null;
		const backend = backendId ? await backendIdentity(repoRoot, backendId) : null;
		return {
			available: true,
			id: pkg.id || packageId,
			capabilities: Array.isArray(pkg.implements)
				? pkg.implements.filter((item) => typeof item === "string")
				: [],
			backend: backendId,
			backendRuntime: backend?.runtime || null,
			backendLauncher: backend?.launcher || null,
			backendAvailable: backend ? backend.available : false,
			backendReason: backend && !backend.available ? backend.reason : null,
			hostBinding,
			packageCard: typeof pkg.package_card === "string" ? pkg.package_card : null,
			claimCard: typeof pkg.claim_card === "string" ? pkg.claim_card : null,
			compute: typeof pkg.compute?.default === "string" ? pkg.compute.default : null,
			privacy: typeof pkg.privacy?.default === "string" ? pkg.privacy.default : null,
			promptBundle: typeof pkg.focus_contract?.prompt_bundle === "string" ? pkg.focus_contract.prompt_bundle : null,
		};
	} catch (err) {
		return {
			available: false,
			id: packageId,
			reason: err?.message || "package manifest unavailable",
		};
	}
}

export async function backendIdentity(repoRoot, backendId) {
	try {
		const backend = await loadBackendManifest(repoRoot, backendId);
		return {
			available: true,
			id: backend.id || backendId,
			runtime: typeof backend.runtime === "string" ? backend.runtime : null,
			supports: Array.isArray(backend.supports)
				? backend.supports.filter((item) => typeof item === "string")
				: [],
			launcher: normalizeLauncher(backend, backendId),
		};
	} catch (err) {
		return {
			available: false,
			id: backendId,
			reason: err?.message || "backend manifest unavailable",
		};
	}
}

export async function runtimeLaunchPlan(repoRoot, options = {}) {
	const packageId = canonicalPackageId(
		options.packageId || (await activePackageId({ hostBinding: options.hostBinding })),
	);
	const pkg = await packageIdentity(repoRoot, packageId, { hostBinding: options.hostBinding });
	if (!pkg.available) throw new Error(`package ${packageId} unavailable: ${pkg.reason || "unknown reason"}`);
	if (!pkg.backend) throw new Error(`package ${pkg.id} does not declare uses.backend`);
	const backend = await loadBackendManifest(repoRoot, pkg.backend);
	const launcher = normalizeLauncher(backend, pkg.backend);
	return {
		packageId: pkg.id,
		backendId: pkg.backend,
		launcher,
		command: launcherCommand(launcher),
		env: { ...launcher.env },
	};
}

export async function packageInventory(repoRoot, options = {}) {
	const root = registryRoot(repoRoot);
	const ids = await registryObjectIds(root, "packages");
	const activeId = canonicalPackageId(
		options.activePackageId || (await activePackageId({ hostBinding: options.hostBinding })),
	);
	const packages = [];
	for (const id of ids) {
		const pkg = await packageIdentity(repoRoot, id);
		if (options.hostBinding && pkg.hostBinding !== options.hostBinding) continue;
		packages.push({
			...pkg,
			active: pkg.id === activeId,
		});
	}
	return packages.sort((a, b) => String(a.id).localeCompare(String(b.id)));
}

export async function activePackageId(options = {}) {
	if (process.env.NEEDLE_PACKAGE) return canonicalPackageId(process.env.NEEDLE_PACKAGE);
	if (process.env.HAY_PACKAGE) return canonicalPackageId(process.env.HAY_PACKAGE);
	try {
		const config = JSON.parse(await readFile(packageConfigPath(), "utf8"));
		if (options.hostBinding && typeof config.packages === "object" && config.packages) {
			const scoped = config.packages[options.hostBinding];
			if (typeof scoped === "string" && scoped) return canonicalPackageId(scoped);
		}
		if (typeof config.package === "string" && config.package) return canonicalPackageId(config.package);
	} catch {
		// Missing or invalid user config falls back to the built-in default.
	}
	return DEFAULT_PACKAGE_ID;
}

export function registryRoot(repoRoot) {
	if (process.env.NEEDLE_REGISTRY_ROOT) return process.env.NEEDLE_REGISTRY_ROOT;
	if (process.env.HAY_REGISTRY_ROOT) return process.env.HAY_REGISTRY_ROOT;
	for (const candidate of [
		join(repoRoot, "needle", "registry_data"),
		join(repoRoot, "registry_data"),
		repoRoot,
	]) {
		if (existsSync(join(candidate, "packages"))) return candidate;
	}
	return repoRoot;
}

async function readRegistryJson(repoRoot, dir, objectId) {
	const path = registryPath(repoRoot, dir, objectId);
	const text = await readFile(path, "utf8");
	return JSON.parse(text);
}

async function loadBackendManifest(repoRoot, backendId) {
	return readRegistryJson(registryRoot(repoRoot), "backends", backendId);
}

function normalizeLauncher(backend, backendId = backend?.id || "<backend>") {
	const launcher = backend?.launcher;
	if (!launcher || typeof launcher !== "object" || Array.isArray(launcher)) {
		throw new Error(`backend ${backendId} requires launcher metadata`);
	}
	let command;
	let extra = "";
	let module = "";
	let args = [];
	if (launcher.kind === "uv-python-module") {
		if (typeof launcher.extra !== "string") {
			throw new Error(`backend ${backendId} launcher.extra must be a string`);
		}
		if (typeof launcher.module !== "string" || !launcher.module) {
			throw new Error(`backend ${backendId} launcher.module must be a non-empty string`);
		}
		args = Array.isArray(launcher.args) ? launcher.args : [];
		if (!args.every((arg) => typeof arg === "string" && arg)) {
			throw new Error(`backend ${backendId} launcher.args must be a string list`);
		}
		extra = launcher.extra;
		module = launcher.module;
		command = launcherCommand({ kind: launcher.kind, extra, module, args });
	} else if (launcher.kind === "needle-cli") {
		command = Array.isArray(launcher.command) ? launcher.command : [];
		if (!command.every((arg) => typeof arg === "string" && arg) || command.length === 0) {
			throw new Error(`backend ${backendId} launcher.command must be a non-empty string list`);
		}
		args = command.slice(1);
	} else {
		throw new Error(`backend ${backendId} launcher.kind must be needle-cli or uv-python-module`);
	}
	const env = launcher.env && typeof launcher.env === "object" && !Array.isArray(launcher.env)
		? launcher.env
		: {};
	for (const [key, value] of Object.entries(env)) {
		if (!key || typeof value !== "string") {
			throw new Error(`backend ${backendId} launcher.env must map strings to strings`);
		}
	}
	return {
		kind: launcher.kind,
		extra,
		module,
		args: [...args],
		command: [...command],
		env: { ...env },
	};
}

function launcherCommand(launcher) {
	if (Array.isArray(launcher.command)) return [...launcher.command];
	const command = ["uv", "run"];
	if (launcher.extra) command.push("--extra", launcher.extra);
	command.push("-m", launcher.module, ...launcher.args);
	return command;
}

function registryPath(repoRoot, dir, objectId) {
	if (objectId.startsWith(`${dir}/`)) return join(repoRoot, `${objectId}.yaml`);
	return join(repoRoot, dir, `${objectId}.yaml`);
}

async function registryObjectIds(repoRoot, dir) {
	const root = join(repoRoot, dir);
	const out = [];
	async function visit(current, prefix = "") {
		let entries;
		try {
			entries = await readdir(current, { withFileTypes: true });
		} catch {
			return;
		}
		for (const entry of entries) {
			const path = join(current, entry.name);
			const idPart = prefix ? `${prefix}/${entry.name}` : entry.name;
			if (entry.isDirectory()) {
				await visit(path, idPart);
			} else if (entry.isFile() && entry.name.endsWith(".yaml")) {
				out.push(idPart.slice(0, -".yaml".length));
			}
		}
	}
	await visit(root);
	return out;
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
	const plan = options.launchPlan || await runtimeLaunchPlan(repoRoot, { hostBinding: PI_HOST_BINDING });
	const command = plan.command;
	const env = {
		...process.env,
		...plan.env,
	};
	const spawnFn = options.spawn || spawn;
	const child = spawnFn(command[0], command.slice(1), {
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
	const runtimeRoot = join(repoRoot, "needle", "runtime");
	const files = (await walkPython(runtimeRoot)).sort((a, b) =>
		relative(runtimeRoot, a).localeCompare(relative(runtimeRoot, b)),
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
