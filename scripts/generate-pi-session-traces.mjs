#!/usr/bin/env node
// Generate small, reviewable Pi-session-shaped Needle traces.
//
// This is not a benchmark runner. It drives the Pi extension harness directly:
// native read/bash tool output -> Needle extension -> daemon/model -> transcript
// record. The default backend is real, so set NEEDLE_MODEL_DIR and use a Python
// interpreter with the worker dependencies installed.

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_OUT = path.join(ROOT, "examples", "traces", "pi-sessions.jsonl");

const CASES = [
	{
		id: "pi-read-batch-guardrail",
		description: "A read observation pruned to the batch-budget splitting logic.",
		tool: "read",
		fixture: "examples/fixtures/batch_guardrail.py",
		params: {
			path: "examples/fixtures/batch_guardrail.py",
			context_focus_question: "how does the batch guardrail split oversized batches?",
		},
		user: "Read the batch guardrail fixture and explain how oversized batches are split.",
		mustContain: [
			"class BatchBudgetResult",
			"def split_batches_by_padded_token_budget",
			"max_padded_tokens",
		],
		mustNotContain: ["unrelated_formatter"],
	},
	{
		id: "pi-bash-noisy-sentinel",
		description: "A noisy bash-style observation keeps the sentinel line.",
		tool: "bash",
		fixture: "examples/fixtures/noisy_sentinel.txt",
		params: {
			command: "synthetic sentinel smoke",
			context_focus_question: "Find the one line that says NEEDLE_SENTINEL and ignore the noise.",
		},
		user: "Run a noisy command and find the NEEDLE_SENTINEL line.",
		mustContain: ["NEEDLE_SENTINEL: pruning works"],
		mustNotContain: ["noise before 001", "noise after 080"],
	},
	{
		id: "pi-read-late-statusline-cost",
		description: "Relevant statusline cost logic near the end of a file is retained.",
		tool: "read",
		fixture: "examples/fixtures/chunked_statusline.py",
		params: {
			path: "examples/fixtures/chunked_statusline.py",
			context_focus_question: "where is the statusline cost range formatted?",
		},
		user: "Read the statusline fixture and find where cost ranges are formatted.",
		mustContain: ["def cost_range_values", "def format_statusline_cost", "est input avoided"],
		mustNotContain: ["noise_helper_00", "noise_helper_11"],
	},
];

const TRACE_WORKER = String.raw`
import json
import re
import sys

loaded = False
BACKEND = "trace-worker"
STOPWORDS = {"about", "after", "before", "does", "find", "from", "ignore", "line", "noise", "that", "the", "this", "what", "when", "where", "which", "with"}

def write(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)

def tokens(query):
    raw = re.findall(r"[A-Za-z0-9_]+", query.lower())
    out = set()
    for token in raw:
        if len(token) <= 3 or token in STOPWORDS:
            continue
        out.add(token)
        out.update(part for part in token.split("_") if len(part) > 3)
    return out

def keep_line(line, query_tokens):
    lowered = line.lower()
    if "needle_sentinel" in lowered:
        return True
    if "[showing lines" in lowered or "command exited with code" in lowered:
        return True
    return any(token in lowered for token in query_tokens)

def prune(text, query):
    query_tokens = tokens(query)
    kept = []
    dropped_run = False
    kept_lines = 0
    dropped_lines = 0
    for line in text.splitlines():
        if keep_line(line, query_tokens):
            if dropped_run:
                kept.append("[pruned]")
                dropped_run = False
            kept.append(line)
            kept_lines += 1
        else:
            dropped_lines += 1
            dropped_run = True
    if dropped_run and kept:
        kept.append("[pruned]")
    output = "\n".join(kept).strip() or text.strip()
    if text.endswith("\n") and output:
        output += "\n"
    decision = "pruned" if output != text else "unchanged"
    reason = "model" if decision == "pruned" else "no-lines-removed"
    return output, decision, reason, kept_lines, dropped_lines

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")
    if op == "status":
        response = {"id": request_id, "ok": True, "status": "resident" if loaded else "cold"}
        if loaded:
            response["backend"] = BACKEND
    elif op == "load":
        loaded = True
        response = {"id": request_id, "ok": True, "status": "resident", "backend": BACKEND}
    elif op == "prune":
        loaded = True
        text = request["text"]
        output, decision, reason, kept_lines, dropped_lines = prune(text, request["query"])
        response = {
            "id": request_id,
            "ok": True,
            "status": "resident",
            "backend": BACKEND,
            "decision": decision,
            "reason": reason,
            "text": output,
            "stats": {
                "decision": decision,
                "reason": reason,
                "input_chars": len(text),
                "output_chars": len(output),
                "saved_chars": max(0, len(text) - len(output)),
                "trace_worker_kept_lines": kept_lines,
                "trace_worker_dropped_lines": dropped_lines,
            },
        }
    elif op in {"unload", "exit"}:
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
        write(response)
        if op == "exit":
            break
        continue
    else:
        response = {"id": request_id, "ok": False, "status": "failed", "error": f"unknown op: {op}"}
    write(response)
`;

function parseArgs() {
	const args = {
		backend: "real",
		out: DEFAULT_OUT,
		needleBin: process.env.NEEDLE_BIN || path.join(ROOT, "target", "debug", "needle"),
		home: process.env.NEEDLE_HOME || path.join(os.tmpdir(), `needle-pi-traces-${process.pid}`),
		keepHome: false,
	};
	for (let i = 2; i < process.argv.length; i += 1) {
		const arg = process.argv[i];
		if (arg === "--backend") args.backend = process.argv[++i];
		else if (arg === "--out") args.out = path.resolve(process.argv[++i]);
		else if (arg === "--needle-bin") args.needleBin = path.resolve(process.argv[++i]);
		else if (arg === "--home") args.home = path.resolve(process.argv[++i]);
		else if (arg === "--keep-home") args.keepHome = true;
		else throw new Error(`unknown argument: ${arg}`);
	}
	if (!["real", "trace"].includes(args.backend)) throw new Error("--backend must be real or trace");
	return args;
}

function sha256(text) {
	return createHash("sha256").update(text).digest("hex");
}

function extractText(content) {
	if (!Array.isArray(content)) return "";
	return content.map((part) => (part?.type === "text" ? String(part.text ?? "") : "")).join("");
}

function fakePi() {
	return {
		tools: new Map(),
		handlers: new Map(),
		commands: new Map(),
		entries: [],
		messages: [],
		notifications: [],
		statuses: [],
		shortcuts: new Map(),
		registerTool(definition) {
			this.tools.set(definition.name, definition);
		},
		registerCommand(name, definition) {
			this.commands.set(name, definition);
		},
		registerShortcut(shortcut, definition) {
			this.shortcuts.set(shortcut, definition);
		},
		on(event, handler) {
			this.handlers.set(event, handler);
		},
		appendEntry(type, data) {
			this.entries.push({ type, data });
		},
		sendMessage(message) {
			this.messages.push(message);
		},
	};
}

function fixtureTextByPath() {
	const byPath = new Map();
	for (const caseDef of CASES) {
		const text = readFileSync(path.join(ROOT, caseDef.fixture), "utf8");
		byPath.set(caseDef.params.path, text);
		byPath.set(caseDef.fixture, text);
	}
	return byPath;
}

function readToolFactory(textByPath) {
	return () => ({
		label: "read",
		description: "Read file contents",
		parameters: {
			type: "object",
			properties: { path: { type: "string" } },
			required: ["path"],
		},
		execute: async (_toolCallId, params) => ({
			content: [{ type: "text", text: textByPath.get(params.path) || "" }],
		}),
	});
}

function bashToolFactory(textByCommand) {
	return () => ({
		label: "bash",
		description: "Execute bash commands",
		parameters: {
			type: "object",
			properties: { command: { type: "string" } },
			required: ["command"],
		},
		execute: async (_toolCallId, params) => ({
			content: [{ type: "text", text: textByCommand.get(params.command) || "" }],
		}),
	});
}

function withFakeIntervals(fn) {
	const originalSetInterval = globalThis.setInterval;
	const originalClearInterval = globalThis.clearInterval;
	const timers = [];
	globalThis.setInterval = (callback, delay, ...intervalArgs) => {
		const timer = { callback, delay, args: intervalArgs, cleared: false };
		timers.push(timer);
		return timer;
	};
	globalThis.clearInterval = (timer) => {
		if (timer) timer.cleared = true;
	};
	return Promise.resolve()
		.then(() => fn(timers))
		.finally(() => {
			globalThis.setInterval = originalSetInterval;
			globalThis.clearInterval = originalClearInterval;
		});
}

function writeTraceWorker(home) {
	const pythonPath = path.join(home, "trace-pythonpath");
	const packageDir = path.join(pythonPath, "needle_worker");
	mkdirSync(packageDir, { recursive: true });
	writeFileSync(path.join(packageDir, "__init__.py"), "");
	writeFileSync(path.join(packageDir, "__main__.py"), `${TRACE_WORKER.trim()}\n`);
	return pythonPath;
}

function setupEnvironment(args) {
	if (!existsSync(args.needleBin)) {
		throw new Error(`needle binary not found: ${args.needleBin}`);
	}
	if (!args.keepHome) rmSync(args.home, { recursive: true, force: true });
	mkdirSync(args.home, { recursive: true });

	process.env.NEEDLE_BIN = args.needleBin;
	process.env.NEEDLE_HOME = args.home;
	process.env.NEEDLE_WORKER_OP_TIMEOUT_SECS = process.env.NEEDLE_WORKER_OP_TIMEOUT_SECS || "90";
	if (args.backend === "trace") {
		process.env.PYTHONPATH = writeTraceWorker(args.home);
		process.env.NEEDLE_COLD_LOAD_MIN_AVAILABLE_MB = "0";
		delete process.env.NEEDLE_PYTHON;
	} else {
		const repoPython = path.join(ROOT, "python");
		process.env.PYTHONPATH = process.env.PYTHONPATH
			? `${repoPython}${path.delimiter}${process.env.PYTHONPATH}`
			: repoPython;
		if (!process.env.NEEDLE_MODEL_DIR) {
			throw new Error("NEEDLE_MODEL_DIR is required for --backend real");
		}
	}
}

function sanitize(text, args) {
	return String(text || "")
		.replaceAll(args.home, "$NEEDLE_HOME")
		.replaceAll(args.needleBin, "$NEEDLE_BIN");
}

function stripAnsi(text) {
	return String(text || "").replace(/\x1b\[[0-9;]*m/g, "");
}

function stableStringify(value) {
	if (Array.isArray(value)) {
		return `[${value.map((item) => stableStringify(item)).join(",")}]`;
	}
	if (value && typeof value === "object") {
		return `{${Object.keys(value)
			.sort()
			.map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
			.join(",")}}`;
	}
	return JSON.stringify(value);
}

async function runCase({ caseDef, args, pi, ctx }) {
	const fixturePath = path.join(ROOT, caseDef.fixture);
	const original = readFileSync(fixturePath, "utf8");
	const messageStart = pi.messages.length;
	const result = await pi.tools.get(caseDef.tool).execute("tool-call-1", caseDef.params, null, null, ctx);
	await pi.commands.get("needle").handler("statusline chars", ctx);
	await pi.commands.get("needle").handler("status", ctx);
	await pi.commands.get("needle").handler("original", ctx);

	const output = extractText(result.content);
	const newMessages = pi.messages.slice(messageStart);
	const statusMessage = newMessages.find((message) => message.customType === "needle-status");
	const originalMessage = newMessages.find((message) => message.customType === "needle-original");
	const counters = pi.entries.at(-1)?.data?.counters || {};
	return {
		schema_version: 1,
		id: caseDef.id,
		kind: "pi-session-trace",
		backend: args.backend,
		description: caseDef.description,
		session: {
			user: caseDef.user,
			assistant_tool_call: {
				tool: caseDef.tool,
				params: caseDef.params,
			},
			tool_result: {
				chars: output.length,
				sha256: sha256(output),
				text: output,
			},
			needle_statusline: ctx.statuses.at(-1)?.value || "",
			needle_status_message: sanitize(statusMessage?.content || "", args),
			needle_original: {
				chars: String(originalMessage?.content || "").length,
				sha256: sha256(String(originalMessage?.content || "")),
				matches_fixture: originalMessage?.content === original,
			},
		},
		input: {
			fixture: caseDef.fixture,
			chars: original.length,
			sha256: sha256(original),
		},
		needle: {
			details: result.details?.needle || {},
			counters,
		},
		assertions: {
			must_contain: caseDef.mustContain,
			must_not_contain: caseDef.mustNotContain,
		},
	};
}

async function main() {
	const args = parseArgs();
	setupEnvironment(args);
	const extension = await import(pathToFileURL(path.join(ROOT, "pi", "extension.js")).href);
	const client = await import(pathToFileURL(path.join(ROOT, "pi", "client.mjs")).href);
	const records = await withFakeIntervals(async () => {
		const pi = fakePi();
		const textByPath = fixtureTextByPath();
		const textByCommand = new Map();
		for (const caseDef of CASES) {
			if (caseDef.tool === "bash") {
				textByCommand.set(caseDef.params.command, readFileSync(path.join(ROOT, caseDef.fixture), "utf8"));
			}
		}
		const ctx = {
			cwd: ROOT,
			statuses: [],
			model: {
				id: "trace-model",
				cost: { input: 0.00001, cacheRead: 0.000001, output: 0, cacheWrite: 0 },
			},
			sessionManager: {
				getSessionId: () => "pi-example-traces",
				getEntries: () => [],
			},
			ui: {
				setStatus(name, value) {
					ctx.statuses.push({ name, value: stripAnsi(sanitize(value, args)) });
				},
				notify(message, level = "info") {
					pi.notifications.push({ level, message: sanitize(message, args) });
				},
			},
		};
		extension.installNeedlePiExtension(pi, {
			createReadTool: readToolFactory(textByPath),
			createBashTool: bashToolFactory(textByCommand),
			requestFn: client.request,
			ensureDaemonFn: client.ensureDaemon,
			nowFn: () => 0,
		});
		await pi.handlers.get("session_start")(null, ctx);
		const out = [];
		try {
			for (const caseDef of CASES) {
				out.push(await runCase({ caseDef, args, pi, ctx }));
			}
		} finally {
			await pi.handlers.get("session_shutdown")(null, ctx);
		}
		return out;
	});

	mkdirSync(path.dirname(args.out), { recursive: true });
	writeFileSync(
		args.out,
		records.map((record) => stableStringify(record)).join("\n") + "\n",
	);
	console.log(`wrote ${records.length} Pi session traces to ${args.out}`);
}

main().catch((error) => {
	console.error(error?.stack || error);
	process.exit(1);
});
