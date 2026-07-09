export const STATUS_MODES = ["chars", "tokens", "cost", "compact"];

export function parseStatusMode(value) {
	const mode = String(value || "").trim().toLowerCase();
	return STATUS_MODES.includes(mode) ? mode : null;
}

export function cycleStatusMode(state) {
	const current = parseStatusMode(state.statusMode) || "chars";
	const next = STATUS_MODES[(STATUS_MODES.indexOf(current) + 1) % STATUS_MODES.length];
	state.statusMode = next;
	return next;
}

export function statusModeLabel(mode) {
	if (mode === "tokens") return "estimated tokens";
	if (mode === "cost") return "estimated cost";
	if (mode === "compact") return "compact";
	return "chars";
}
