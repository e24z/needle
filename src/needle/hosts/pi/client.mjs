import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { createConnection } from "node:net";
import { basename, dirname, join, normalize, relative } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { chmod, lstat, mkdir, readdir, readFile, stat } from "node:fs/promises";
import { promisify } from "node:util";

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_RESPONSE_BYTES = 2_500_000;
const execFileAsync = promisify(execFile);
const PI_HOST_BINDING = "pi/native-tools";
const DEFAULT_PACKAGE_ID = "e24z/mlx-pi-soft-lamr";
// Transitional mirror of needle.runtime.naming while the Pi adapter runs in Node.
const PACKAGE_ALIASES = new Map([
	["e24z/pi-local-mac", "e24z/mlx-pi-reference"],
	["e24z/pi-local-mac-soft-lamr", "e24z/mlx-pi-soft-lamr"],
	["e24z/mcp-bash-local", "e24z/mlx-mcp-bash-reference"],
]);
const BOOLEAN_RUNTIME_ENV_KEYS = new Set([
	"NEEDLE_REPAIR",
	"NEEDLE_MLX_LIGHT",
	"NEEDLE_PROFILE_MLX",
	"NEEDLE_MLX_CLEAR_CACHE_AFTER_PRUNE",
]);
const BOOLEAN_RUNTIME_ENV_VALUES = new Set(["0", "1", "false", "true", "no", "yes", "off", "on"]);
const POSITIVE_INT_RUNTIME_ENV_KEYS = new Set([
	"NEEDLE_MLX_MAX_LENGTH",
	"NEEDLE_MAX_LENGTH",
	"NEEDLE_MLX_MAX_BATCH_SIZE",
	"NEEDLE_MLX_MAX_BATCH_TOKENS",
	"NEEDLE_MLX_CACHE_LIMIT_MB",
	"NEEDLE_MLX_WIRED_LIMIT_MB",
	"NEEDLE_MLX_ADAPTIVE_SINGLE_CHUNK_UNTIL_TOKENS",
	"NEEDLE_MLX_ADAPTIVE_SMALL_MAX_LENGTH",
	"NEEDLE_MLX_ADAPTIVE_LARGE_MAX_LENGTH",
]);
const NON_NEGATIVE_INT_RUNTIME_ENV_KEYS = new Set(["NEEDLE_CHUNK_OVERLAP_TOKENS"]);
const FLOAT_0_TO_1_RUNTIME_ENV_KEYS = new Set(["NEEDLE_THRESHOLD"]);
const MIN_FLOAT_RUNTIME_ENV_KEYS = new Map([["NEEDLE_MLX_MAX_LENGTH_RATIO", 1.0]]);
const ENUM_RUNTIME_ENV_VALUES = new Map([
	["NEEDLE_MLX_PROFILE", new Set(["local_adaptive", "local-mlx-adaptive", "local_mlx_adaptive"])],
]);
const KNOWN_RUNTIME_PROFILE_ENV_KEYS = new Set([
	...BOOLEAN_RUNTIME_ENV_KEYS,
	...POSITIVE_INT_RUNTIME_ENV_KEYS,
	...NON_NEGATIVE_INT_RUNTIME_ENV_KEYS,
	...FLOAT_0_TO_1_RUNTIME_ENV_KEYS,
	...MIN_FLOAT_RUNTIME_ENV_KEYS.keys(),
	...ENUM_RUNTIME_ENV_VALUES.keys(),
]);

export function canonicalPackageId(packageId) {
	return PACKAGE_ALIASES.get(packageId) || packageId;
}

function samePath(left, right) {
	return normalize(String(left)) === normalize(String(right));
}

export function expandUserPath(pathValue) {
	if (pathValue === undefined || pathValue === null) return pathValue;
	const value = String(pathValue);
	if (value === "~") return homePath();
	if (value.startsWith("~/") || value.startsWith("~\\")) return join(homePath(), value.slice(2));
	return value;
}

function homePath() {
	return process.env.HOME || homedir();
}

function envPath(primary, legacy, fallback) {
	const value = process.env[primary] || process.env[legacy];
	return value ? expandUserPath(value) : fallback;
}

function currentUid() {
	return typeof process.getuid === "function" ? process.getuid() : null;
}

export function appName() {
	return process.env.NEEDLE_APP_NAME || process.env.HAY_APP_NAME || "needle";
}

export function appHome() {
	return envPath("NEEDLE_HOME", "HAY_HOME", join(homePath(), `.${appName()}`));
}

export function packageConfigPath() {
	return envPath("NEEDLE_CONFIG", "HAY_CONFIG", join(appHome(), "config.json"));
}

export function modelRoot() {
	return envPath("NEEDLE_MODEL_ROOT", "HAY_MODEL_ROOT", join(appHome(), "models"));
}

export function managerSocketPath() {
	return envPath("NEEDLE_MANAGER_SOCKET", "HAY_MANAGER_SOCKET", join(appHome(), "manager.sock"));
}

export function managerTokenPath(socketPath = undefined) {
	const override = process.env.NEEDLE_MANAGER_TOKEN_FILE || process.env.HAY_MANAGER_TOKEN_FILE;
	if (override) return expandUserPath(override);
	const sock = expandUserPath(socketPath || managerSocketPath());
	const defaultSocket = join(appHome(), "manager.sock");
	if (samePath(sock, defaultSocket)) return join(appHome(), "manager.token");
	return join(dirname(sock), `${basename(sock)}.token`);
}

export async function readManagerToken(socketPath = undefined) {
	const tokenPath = managerTokenPath(socketPath);
	await assertSafeTokenFile(tokenPath);
	try {
		await chmod(tokenPath, 0o600);
	} catch {
		// Best-effort parity with Python's token reader; inability to chmod should
		// not hide the clearer read/empty-token diagnostics above and below.
	}
	const text = await readFile(tokenPath, "utf8");
	const token = text.trim();
	if (!token) {
		const err = new Error(
			`manager token is empty at ${tokenPath}; the live manager is unusable until Needle is restarted`,
		);
		err.code = "NEEDLE_MANAGER_TOKEN_EMPTY";
		err.tokenPath = tokenPath;
		throw err;
	}
	return token;
}

async function assertSafeTokenFile(tokenPath) {
	let info;
	try {
		info = await lstat(tokenPath);
	} catch {
		const err = new Error(
			`manager token is missing at ${tokenPath}; the live manager is unusable until Needle is restarted`,
		);
		err.code = "NEEDLE_MANAGER_TOKEN_MISSING";
		err.tokenPath = tokenPath;
		throw err;
	}
	if (info.isSymbolicLink()) {
		throwRuntimePathError("NEEDLE_MANAGER_TOKEN_UNSAFE", `manager token must not be a symlink: ${tokenPath}`, tokenPath);
	}
	if (!info.isFile()) {
		throwRuntimePathError("NEEDLE_MANAGER_TOKEN_UNSAFE", `manager token must be a regular file: ${tokenPath}`, tokenPath);
	}
	const uid = currentUid();
	if (uid !== null && info.uid !== uid) {
		throwRuntimePathError(
			"NEEDLE_MANAGER_TOKEN_UNSAFE",
			`manager token is not owned by the current user: ${tokenPath}`,
			tokenPath,
		);
	}
	await assertSafeRuntimeParent(dirname(tokenPath), tokenPath);
}

async function assertSafeRuntimeParent(parentPath, childPath) {
	const uid = currentUid();
	if (uid === null) return;
	try {
		const parent = await stat(parentPath);
		const otherWritable = Boolean(parent.mode & 0o002);
		const sticky = Boolean(parent.mode & 0o1000);
		if (otherWritable && !sticky) {
			throwRuntimePathError(
				"NEEDLE_RUNTIME_PARENT_UNSAFE",
				`runtime parent is other-writable without sticky bit: ${parentPath}`,
				childPath,
			);
		}
	} catch (err) {
		if (err?.code?.startsWith?.("NEEDLE_")) throw err;
	}
}

function throwRuntimePathError(code, message, path) {
	const err = new Error(message);
	err.code = code;
	err.path = path;
	throw err;
}

export function eventsPath() {
	return envPath("NEEDLE_EVENTS", "HAY_EVENTS", join(appHome(), "events.jsonl"));
}

export function repoRootFromModuleUrl(moduleUrl) {
	const here = dirname(fileURLToPath(moduleUrl));
	const sourceRoot = join(here, "..", "..", "..", "..");
	if (existsSync(join(sourceRoot, "pyproject.toml")) || existsSync(join(sourceRoot, ".git"))) {
		return sourceRoot;
	}
	return join(here, "..", "..", "..");
}

export function pathFromModuleUrl(moduleUrl) {
	return fileURLToPath(moduleUrl);
}

export async function request(req, options = {}) {
	const socketPath = expandUserPath(options.socketPath || managerSocketPath());
	const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
	const maxResponseBytes = options.maxResponseBytes ?? DEFAULT_MAX_RESPONSE_BYTES;
	const signal = options.signal;
	if (signal?.aborted) throw new Error("aborted");
	await assertSafeManagerSocket(socketPath);
	const wireReq = { ...req, token: await readManagerToken(socketPath) };
	return new Promise((resolve, reject) => {
		let settled = false;
		let buf = "";
		let responseBytes = 0;
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
		socket.on("connect", () => socket.write(`${JSON.stringify(wireReq)}\n`));
		socket.on("data", (chunk) => {
			responseBytes += Buffer.byteLength(chunk, "utf8");
			if (responseBytes > maxResponseBytes) {
				const err = new Error(`manager response exceeded ${maxResponseBytes} bytes`);
				err.code = "NEEDLE_MANAGER_RESPONSE_TOO_LARGE";
				finish(err);
				return;
			}
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
	return request(
		{
			op: "lease",
			session,
			version,
			...leaseIdentity(options),
		},
		{ timeoutMs: 5_000, ...options },
	);
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
		join(repoRoot, "src", "needle", "hosts", "pi", "package.json"),
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
		const runtimeProfile = normalizeRuntimeProfile(pkg, packageId);
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
			runtimeProfile: runtimeProfile.id,
			runtimeProfileEnv: runtimeProfile.env,
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
		hostBinding: pkg.hostBinding,
		launcher,
		command: launcherCommand(launcher),
		env: { ...launcher.env, ...pkg.runtimeProfileEnv },
		runtimeProfile: pkg.runtimeProfile || "",
	};
}

function leaseIdentity(options = {}) {
	const explicit = options.runtimeIdentity;
	if (explicit && typeof explicit === "object") {
		return normalizeLeaseIdentity(explicit);
	}
	return runtimeIdentityFromLaunchPlan(options.launchPlan);
}

function runtimeIdentityFromLaunchPlan(plan) {
	if (!plan || typeof plan !== "object") return {};
	return normalizeLeaseIdentity({
		package_id: plan.packageId,
		host_binding: plan.hostBinding,
		backend_id: plan.backendId,
		runtime_profile: plan.runtimeProfile,
	});
}

function normalizeLeaseIdentity(identity) {
	const out = {};
	for (const [target, source] of [
		["package_id", identity.package_id ?? identity.packageId],
		["host_binding", identity.host_binding ?? identity.hostBinding],
		["backend_id", identity.backend_id ?? identity.backendId],
		["runtime_profile", identity.runtime_profile ?? identity.runtimeProfile],
	]) {
		if (source !== undefined && source !== null) out[target] = String(source);
	}
	return out;
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
		join(repoRoot, "src", "needle", "registry_data"),
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

function normalizeRuntimeProfile(pkg, packageId = pkg?.id || "<package>") {
	const profile = pkg?.runtime_profile;
	if (profile === undefined || profile === null) return { id: null, env: {} };
	if (!profile || typeof profile !== "object" || Array.isArray(profile)) {
		throw new Error(`package ${packageId} runtime_profile must be an object`);
	}
	if (typeof profile.id !== "string" || !profile.id) {
		throw new Error(`package ${packageId} runtime_profile.id must be a non-empty string`);
	}
	const id = profile.id;
	const env = profile.env && typeof profile.env === "object" && !Array.isArray(profile.env)
		? profile.env
		: {};
	const out = {};
	for (const [key, value] of Object.entries(env)) {
		if (!key.startsWith("NEEDLE_") || typeof value !== "string") {
			throw new Error(`package ${packageId} runtime_profile.env must map NEEDLE_* keys to strings`);
		}
		validateRuntimeProfileEnvValue(packageId, key, value);
		out[key] = value;
	}
	return { id, env: out };
}

function validateRuntimeProfileEnvValue(packageId, key, value) {
	if (!KNOWN_RUNTIME_PROFILE_ENV_KEYS.has(key)) {
		throw new Error(`package ${packageId} runtime_profile.env key ${key} is unknown`);
	}
	if (BOOLEAN_RUNTIME_ENV_KEYS.has(key)) {
		if (!BOOLEAN_RUNTIME_ENV_VALUES.has(value.trim().toLowerCase())) {
			throw new Error(`package ${packageId} runtime_profile.env ${key} must be boolean-like`);
		}
		return;
	}
	if (POSITIVE_INT_RUNTIME_ENV_KEYS.has(key)) {
		validateIntRuntimeEnv(packageId, key, value, 1);
		return;
	}
	if (NON_NEGATIVE_INT_RUNTIME_ENV_KEYS.has(key)) {
		validateIntRuntimeEnv(packageId, key, value, 0);
		return;
	}
	if (FLOAT_0_TO_1_RUNTIME_ENV_KEYS.has(key)) {
		const numeric = validateFloatRuntimeEnv(packageId, key, value);
		if (numeric < 0 || numeric > 1) {
			throw new Error(`package ${packageId} runtime_profile.env ${key} must be between 0 and 1`);
		}
		return;
	}
	if (MIN_FLOAT_RUNTIME_ENV_KEYS.has(key)) {
		const numeric = validateFloatRuntimeEnv(packageId, key, value);
		const minimum = MIN_FLOAT_RUNTIME_ENV_KEYS.get(key);
		if (numeric < minimum) {
			throw new Error(`package ${packageId} runtime_profile.env ${key} must be at least ${minimum}`);
		}
		return;
	}
	const allowed = ENUM_RUNTIME_ENV_VALUES.get(key);
	if (allowed && !allowed.has(value)) {
		throw new Error(
			`package ${packageId} runtime_profile.env ${key} must be one of ${Array.from(allowed).sort().join(", ")}`
		);
	}
}

function validateIntRuntimeEnv(packageId, key, value, minimum) {
	if (!/^-?\d+$/.test(value.trim())) {
		throw new Error(`package ${packageId} runtime_profile.env ${key} must be an integer`);
	}
	const numeric = Number.parseInt(value, 10);
	if (`${numeric}` !== value.trim()) {
		throw new Error(`package ${packageId} runtime_profile.env ${key} must be an integer`);
	}
	if (numeric < minimum) {
		throw new Error(
			`package ${packageId} runtime_profile.env ${key} must be ${
				minimum === 1 ? "a positive integer" : "a non-negative integer"
			}`
		);
	}
}

function validateFloatRuntimeEnv(packageId, key, value) {
	const trimmed = value.trim();
	if (!trimmed) {
		throw new Error(`package ${packageId} runtime_profile.env ${key} must be a number`);
	}
	const numeric = Number(trimmed);
	if (!Number.isFinite(numeric)) {
		throw new Error(`package ${packageId} runtime_profile.env ${key} must be a finite number`);
	}
	return numeric;
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
	return socketAcceptsConnection(expandUserPath(socketPath), 500);
}

export async function ensureManager(options = {}) {
	const socketPath = expandUserPath(options.socketPath || managerSocketPath());
	const repoRoot = options.repoRoot;
	const plan = options.launchPlan || (repoRoot
		? await runtimeLaunchPlan(repoRoot, { hostBinding: PI_HOST_BINDING })
		: null);
	const expectedVersion = options.expectedVersion || options.version || (repoRoot ? await codeVersion(repoRoot) : "");
	if (await socketIsLive(socketPath)) {
		const usability = await managerUsability(socketPath, { launchPlan: plan, expectedVersion });
		return usability.usable;
	}
	if (!repoRoot) throw new Error("repoRoot is required to spawn the manager");
	if (!plan) throw new Error("launchPlan is required to spawn the manager");
	const command = managerCommandWithContext(plan.command, {
		packageId: plan.packageId,
		hostBinding: plan.hostBinding || PI_HOST_BINDING,
	});
	const env = {
		...process.env,
		...plan.env,
	};
	const spawnFn = options.spawn || spawn;
	let child;
	try {
		child = spawnFn(command[0], command.slice(1), {
			cwd: repoRoot,
			env,
			detached: true,
			stdio: "ignore",
		});
	} catch {
		return false;
	}
	if (!child || typeof child.unref !== "function") return false;
	child.unref();
	const timeoutMs = options.timeoutMs ?? 10_000;
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await socketIsLive(socketPath)) {
			const usability = await managerUsability(socketPath, { launchPlan: plan, expectedVersion });
			if (usability.usable) return true;
			if (usability.reason !== "connect" && usability.reason !== "timeout") return false;
		}
		await sleep(100);
	}
	return false;
}

export async function codeVersion(repoRoot) {
	const packageRoot = existsSync(join(repoRoot, "src", "needle"))
		? join(repoRoot, "src", "needle")
		: join(repoRoot, "needle");
	const files = (await codeVersionFiles(packageRoot)).sort((a, b) => {
		const left = relative(packageRoot, a);
		const right = relative(packageRoot, b);
		return left < right ? -1 : left > right ? 1 : 0;
	});
	const hash = createHash("sha1");
	for (const file of files) {
		hash.update(relative(packageRoot, file));
		hash.update("\0");
		hash.update(await readFile(file));
	}
	return hash.digest("hex").slice(0, 12);
}

function managerCommandWithContext(command, { packageId = "", hostBinding = "" } = {}) {
	const out = [...command];
	if (packageId && !out.includes("--package")) out.push("--package", packageId);
	if (hostBinding && !out.includes("--host-binding")) out.push("--host-binding", hostBinding);
	return out;
}

export async function acquireLease(sessionId, version, options = {}) {
	const attempts = options.attempts ?? 4;
	const plan = options.launchPlan || (options.repoRoot
		? await runtimeLaunchPlan(options.repoRoot, { hostBinding: PI_HOST_BINDING })
		: null);
	const leaseOptions = plan ? { ...options, launchPlan: plan, expectedVersion: version } : { ...options, expectedVersion: version };
	for (let i = 0; i < attempts; i++) {
		try {
			const resp = await lease(sessionId, version, leaseOptions);
			if (resp.ok) return true;
			if (resp.stale) {
				const down = await waitForSocketDown(options.socketPath || managerSocketPath(), 10_000);
				if (!down) continue;
				if (!(await ensureManager(leaseOptions))) return false;
				continue;
			}
			return false;
		} catch {
			if (!(await ensureManager(leaseOptions))) return false;
		}
	}
	return false;
}

async function socketAcceptsConnection(socketPath, timeoutMs) {
	if (!(await managerSocketPathIsSafe(socketPath))) return false;
	return new Promise((resolve) => {
		let settled = false;
		const socket = createConnection(socketPath);
		const finish = (value) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			socket.destroy();
			resolve(value);
		};
		const timer = setTimeout(() => finish(false), timeoutMs);
		socket.on("connect", () => finish(true));
		socket.on("error", () => finish(false));
	});
}

async function managerUsability(socketPath, options = {}) {
	let resp;
	try {
		resp = await stats({ socketPath, timeoutMs: options.timeoutMs ?? 500 });
	} catch (err) {
		return { usable: false, reason: err?.code || err?.message || "request failed" };
	}
	if (!resp?.ok) {
		return { usable: false, reason: resp?.error || "stats refused", response: resp };
	}
	const mismatches = runtimeIdentityMismatches(resp, options.launchPlan, options.expectedVersion);
	if (Object.keys(mismatches).length) {
		return { usable: false, reason: "identity mismatch", mismatches, response: resp };
	}
	return { usable: true, response: resp };
}

function runtimeIdentityMismatches(statsResp, plan, expectedVersion = "") {
	const requested = runtimeIdentityFromLaunchPlan(plan);
	const mismatches = {};
	for (const [field, requestedValue] of Object.entries(requested)) {
		if (!requestedValue) continue;
		const actualValue = statsResp?.[field] === undefined || statsResp?.[field] === null
			? ""
			: String(statsResp[field]);
		if (String(requestedValue) !== actualValue) {
			mismatches[field] = { requested: String(requestedValue), actual: actualValue };
		}
	}
	if (expectedVersion) {
		const actualVersion = statsResp?.version === undefined || statsResp?.version === null
			? ""
			: String(statsResp.version);
		if (actualVersion && String(expectedVersion) !== actualVersion) {
			mismatches.version = { requested: String(expectedVersion), actual: actualVersion };
		}
	}
	return mismatches;
}

async function assertSafeManagerSocket(socketPath) {
	if (!(await managerSocketPathIsSafe(socketPath))) {
		throwRuntimePathError("NEEDLE_MANAGER_SOCKET_UNSAFE", `manager socket is not safe to contact: ${socketPath}`, socketPath);
	}
}

async function managerSocketPathIsSafe(socketPath) {
	let info;
	try {
		info = await lstat(socketPath);
	} catch {
		return false;
	}
	if (info.isSymbolicLink() || !info.isSocket()) return false;
	const uid = currentUid();
	return uid === null || info.uid === uid;
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

async function codeVersionFiles(packageRoot) {
	const files = [];
	for (const rel of [
		"runtime",
		"backends",
	]) {
		files.push(...await walkPython(join(packageRoot, rel)));
	}
	for (const rel of [
		"registry.py",
		join("registry_data", "packages"),
		join("registry_data", "backends"),
	]) {
		const path = join(packageRoot, rel);
		if (rel.endsWith(".py")) {
			if (existsSync(path)) files.push(path);
		} else {
			files.push(...await walkFiles(path, [".yaml", ".json"]));
		}
	}
	return files;
}

async function walkFiles(root, suffixes) {
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
			} else if (entry.isFile() && suffixes.some((suffix) => entry.name.endsWith(suffix))) {
				out.push(path);
			} else if (entry.isSymbolicLink()) {
				try {
					const s = await stat(path);
					if (s.isFile() && suffixes.some((suffix) => entry.name.endsWith(suffix))) out.push(path);
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
