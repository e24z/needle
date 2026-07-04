// Integration seam: pi/client.mjs against the real needle daemon binary
// (with a fake needle_worker), verifying the JSON contract end to end.
//
// Run: cargo build && NEEDLE_BIN=target/debug/needle node tests/test_pi_client_daemon.mjs

import assert from "node:assert/strict";
import { mkdirSync, writeFileSync, existsSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FAKE_WORKER = readFileSync(path.join(HERE, "fixtures", "fake_worker.py"), "utf8");

const scratch = path.join(os.tmpdir(), `needle-pi-client-${process.pid}`);
const pythonPath = path.join(scratch, "pythonpath");
mkdirSync(path.join(pythonPath, "needle_worker"), { recursive: true });
writeFileSync(path.join(pythonPath, "needle_worker", "__init__.py"), "");
writeFileSync(path.join(pythonPath, "needle_worker", "__main__.py"), FAKE_WORKER);

process.env.NEEDLE_SOCKET = path.join(scratch, "needle.sock");
process.env.NEEDLE_BIN = process.env.NEEDLE_BIN || "target/debug/needle";
process.env.NEEDLE_COLD_LOAD_MIN_AVAILABLE_MB = "0";
process.env.FAKE_WORKER_MODE = "normal";
process.env.PYTHONPATH = pythonPath;

const { ensureDaemon, request } = await import("../pi/client.mjs");

async function main() {
	assert.ok(await ensureDaemon(), "daemon starts and answers");

	const enabled = await request("enable", { session: "pi-test" }, { timeoutMs: 30_000 });
	assert.equal(enabled.ok, true, `enable: ${JSON.stringify(enabled)}`);
	assert.equal(enabled.backend_status, "resident");

	const pruned = await request(
		"prune",
		{ session: "pi-test", text: "keep drop ".repeat(30), query: "keep relevant code" },
		{ timeoutMs: 30_000 },
	);
	assert.equal(pruned.ok, true);
	assert.equal(pruned.decision, "pruned");
	assert.equal(pruned.reason, "model");
	assert.ok(!pruned.text.includes(" drop"));

	const status = await request("status", {});
	assert.equal(status.mode, "on");
	assert.equal(status.sessions, 1);

	const original = await request("original", { session: "pi-test" });
	assert.ok(original.text.includes("keep drop"), "original recoverable");

	const disabled = await request("disable", { session: "pi-test" });
	assert.equal(disabled.shutdown, true, "last lease puts the campfire out");

	// The daemon exits and removes its socket.
	const deadline = Date.now() + 5000;
	while (existsSync(process.env.NEEDLE_SOCKET) && Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, 50));
	}
	assert.ok(!existsSync(process.env.NEEDLE_SOCKET), "socket removed after shutdown");

	rmSync(scratch, { recursive: true, force: true });
	console.log("test_pi_client_daemon OK");
}

main().then(
	() => process.exit(0),
	(error) => {
		console.error(error);
		process.exit(1);
	},
);
