import { execFileSync } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export async function loadPiTools() {
	const mod = await importPiSdk();
	if (typeof mod.createReadTool !== "function") {
		throw new Error("Needle Pi extension requires createReadTool from @mariozechner/pi-coding-agent");
	}
	return {
		createReadTool: mod.createReadTool,
		createBashTool: typeof mod.createBashTool === "function" ? mod.createBashTool : undefined,
	};
}

async function importPiSdk() {
	try {
		return await import("@mariozechner/pi-coding-agent");
	} catch (error) {
		return importPiSdkFromCli(error);
	}
}

async function importPiSdkFromCli(cause) {
	const candidates = [];
	if (process.argv[1]) candidates.push(process.argv[1]);
	try {
		const piPath = execFileSync("which", ["pi"], { encoding: "utf8" }).trim();
		if (piPath) candidates.push(piPath);
	} catch {
		// Fall through to the clearer package import error below.
	}
	for (const candidate of candidates) {
		try {
			const real = realpathSync(candidate);
			const indexPath = resolve(dirname(dirname(real)), "dist/index.js");
			if (existsSync(indexPath)) return import(pathToFileURL(indexPath).href);
		} catch {
			// Try the next candidate.
		}
	}
	throw cause;
}
