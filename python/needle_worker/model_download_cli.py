"""CLI entrypoint for model provisioning, invoked by the Rust setup wizard.

Rust decides where models live (NEEDLE_HOME / NEEDLE_MODEL_ROOT are already in
the environment when this runs); this process only performs the Hugging Face
download into that location and reports the result as one JSON line.

Run: python -m needle_worker.model_download_cli [--repo R] [--revision REV]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .model_download import download_model_snapshot

DEFAULT_REPO = "ayanami-kitasan/code-pruner"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Needle's model snapshot.")
    parser.add_argument("--repo", default=os.environ.get("NEEDLE_MODEL", DEFAULT_REPO))
    parser.add_argument(
        "--revision", default=os.environ.get("NEEDLE_MODEL_REVISION") or None
    )
    args = parser.parse_args(argv)

    try:
        result = download_model_snapshot(
            repo=args.repo,
            revision=args.revision,
            caller="needle-setup",
            force=False,
        )
    except Exception as exc:  # noqa: BLE001 - the wizard needs the error text.
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)
        print()
        return 1

    json.dump(
        {
            "ok": True,
            "path": result.path,
            "repo": result.repo,
            "resolved_revision": result.resolved_revision,
            "downloaded": result.downloaded,
        },
        sys.stdout,
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
