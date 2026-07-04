// Thin NDJSON client for the Needle daemon socket.
//
// One connection per operation: the daemon answers control ops while prunes
// are in flight, so there is nothing to gain from connection reuse here, and
// per-op connections cannot leak wedged state between calls.

import { execFileSync, spawn } from "node:child_process";
import net from "node:net";

let cachedPaths = null;

export function needleHome() {
	return needlePaths().home;
}

export function socketPath() {
	if (process.env.NEEDLE_SOCKET) return process.env.NEEDLE_SOCKET;
	return needlePaths().socket;
}

export function needleBinary() {
	return process.env.NEEDLE_BIN || "needle";
}

export function needlePaths() {
	if (cachedPaths) return cachedPaths;
	const output = execFileSync(needleBinary(), ["paths", "--json"], {
		encoding: "utf8",
		timeout: 5000,
	});
	const paths = JSON.parse(output);
	if (
		!paths ||
		typeof paths.home !== "string" ||
		typeof paths.socket !== "string" ||
		typeof paths.config !== "string"
	) {
		throw new Error("needle paths --json returned invalid paths");
	}
	cachedPaths = paths;
	return cachedPaths;
}

export function request(op, fields = {}, { timeoutMs = 5000 } = {}) {
	return new Promise((resolve, reject) => {
		const socket = net.createConnection(socketPath());
		let settled = false;
		const timer = setTimeout(() => {
			settle(false, new Error("timeout"), { destroy: true });
		}, timeoutMs);
		let buffer = "";

		function ignoreLateError() {}

		function cleanup() {
			clearTimeout(timer);
			socket.off("connect", onConnect);
			socket.off("data", onData);
			socket.off("error", onError);
			socket.off("end", onEnd);
			socket.off("close", onClose);
			socket.on("error", ignoreLateError);
		}

		function settle(ok, value, { destroy = false } = {}) {
			if (settled) return;
			settled = true;
			cleanup();
			if (destroy) socket.destroy();
			else socket.end();
			if (ok) resolve(value);
			else reject(value);
		}

		function onConnect() {
			socket.write(`${JSON.stringify({ op, ...fields })}\n`);
		}

		function onData(chunk) {
			buffer += chunk.toString("utf8");
			const newline = buffer.indexOf("\n");
			if (newline === -1) return;
			try {
				settle(true, JSON.parse(buffer.slice(0, newline)));
			} catch (error) {
				settle(false, error);
			}
		}

		function onError(error) {
			settle(false, error, { destroy: true });
		}

		function onEnd() {
			settle(false, new Error("connection ended before response"));
		}

		function onClose() {
			settle(false, new Error("connection closed before response"));
		}

		socket.on("connect", onConnect);
		socket.on("data", onData);
		socket.on("error", onError);
		socket.on("end", onEnd);
		socket.on("close", onClose);
	});
}

/// Spawn `needle daemon` detached and wait for its socket to answer.
export async function ensureDaemon({ waitMs = 10_000 } = {}) {
	if (await answers()) return true;
	const child = spawn(needleBinary(), ["daemon"], {
		detached: true,
		stdio: "ignore",
	});
	child.unref();
	const deadline = Date.now() + waitMs;
	while (Date.now() < deadline) {
		if (await answers()) return true;
		await sleep(100);
	}
	return false;
}

async function answers() {
	try {
		const response = await request("status", {}, { timeoutMs: 1000 });
		return response?.ok === true;
	} catch {
		return false;
	}
}

function sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}
