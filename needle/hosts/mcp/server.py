"""Needle MCP stdio server.

The server intentionally exposes one bash observation tool. Host-native tools
remain responsible for edit/write/apply-patch mutation.
"""

from __future__ import annotations

from .bash import needle_bash_observation


def _build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised by installed users.
        raise RuntimeError(
            "Needle's MCP server requires the `mcp` Python package. "
            "Install Needle with MCP support or use a Homebrew/wheel build that includes it."
        ) from exc

    mcp = FastMCP(
        "Needle MCP Bash",
        instructions=(
            "Use needle_bash for observation through shell commands. "
            "Provide context_focus_question for large outputs when there is a clear, "
            "self-contained intent. Use host-native edit/write/apply-patch tools for mutation."
        ),
    )

    @mcp.tool()
    def needle_bash(command: str, context_focus_question: str | None = None) -> str:
        """Execute one bash command and optionally prune large output.

        Omit context_focus_question for raw output or small commands. When supplied,
        the question must be complete and self-contained; Needle does not invent
        hidden goal hints.
        """
        return needle_bash_observation(command, context_focus_question)

    return mcp


def main() -> None:
    _build_server().run(transport="stdio")


if __name__ == "__main__":
    main()

