// Thin NDJSON client for the Needle daemon socket.
//
// One connection per operation: the daemon answers control ops while prunes
// are in flight, so there is nothing to gain from connection reuse here, and
// per-op connections cannot leak wedged state between calls.

import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";

export function needleHome() {
	if (process.env.NEEDLE_HOME) return process.env.NEEDLE_HOME;
	if (process.platform === "darwin") {
		return path.join(os.homedir(), "Library", "Application Support", "Needle");
	}
	return path.join(os.homedir(), ".local", "share", "needle");
}

export function socketPath() {
	if (process.env.NEEDLE_SOCKET) return process.env.NEEDLE_SOCKET;
	return path.join(needleHome(), "runtime", "needle.sock");
}

export function needleBinary() {
	return process.env.NEEDLE_BIN || "needle";
}

export function request(op, fields = {}, { timeoutMs = 5000 } = {}) {
	return new Promise((resolve, reject) => {
		const socket = net.createConnection(socketPath());
		const timer = setTimeout(() => {
			socket.destroy();
			reject(new Error("timeout"));
		}, timeoutMs);
		let buffer = "";
		socket.on("connect", () => {
			socket.write(`${JSON.stringify({ op, ...fields })}\n`);
		});
		socket.on("data", (chunk) => {
			buffer += chunk.toString("utf8");
			const newline = buffer.indexOf("\n");
			if (newline === -1) return;
			clearTimeout(timer);
			socket.end();
			try {
				resolve(JSON.parse(buffer.slice(0, newline)));
			} catch (error) {
				reject(error);
			}
		});
		socket.on("error", (error) => {
			clearTimeout(timer);
			reject(error);
		});
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
