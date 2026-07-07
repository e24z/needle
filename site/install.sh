#!/usr/bin/env bash
set -euo pipefail

repo="e24z/needle"
version="latest"
prefix="${HOME}/.local"
archive_url=""
dry_run=0
run_setup=1

usage() {
	cat <<'USAGE'
Install Needle.

Usage:
  install.sh [--version latest|vX.Y.Z] [--prefix DIR] [--archive-url URL] [--dry-run] [--no-setup]

Defaults:
  --version latest
  --prefix  ~/.local

Examples:
  curl -fsSL https://e24z.github.io/needle/install.sh | bash
  curl -fsSL https://e24z.github.io/needle/install.sh | bash -s -- --prefix /opt/homebrew
  curl -fsSL https://e24z.github.io/needle/install.sh | bash -s -- --no-setup
USAGE
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--version)
			version="$2"
			shift 2
			;;
		--prefix)
			prefix="$2"
			shift 2
			;;
		--archive-url)
			archive_url="$2"
			shift 2
			;;
		--dry-run)
			dry_run=1
			shift
			;;
		--no-setup)
			run_setup=0
			shift
			;;
		--help|-h)
			usage
			exit 0
			;;
		*)
			echo "unknown option: $1" >&2
			usage >&2
			exit 2
			;;
	esac
done

case "$(uname -s)-$(uname -m)" in
	Darwin-arm64) host="aarch64-apple-darwin" ;;
	*)
		echo "Needle currently ships an Apple Silicon macOS artifact only." >&2
		echo "Unsupported host: $(uname -s)-$(uname -m)" >&2
		exit 1
		;;
esac

asset="needle-${host}.tar.gz"
if [[ -z "$archive_url" ]]; then
	if [[ "$version" == "latest" ]]; then
		archive_url="https://github.com/${repo}/releases/latest/download/${asset}"
	else
		archive_url="https://github.com/${repo}/releases/download/${version}/${asset}"
	fi
fi

echo "Needle installer"
echo "  artifact: ${archive_url}"
echo "  prefix:   ${prefix}"

if [[ "$dry_run" == 1 ]]; then
	echo "dry run: no changes made"
	exit 0
fi

tmp="$(mktemp -d)"
cleanup() {
	rm -rf "$tmp"
}
trap cleanup EXIT

archive="${tmp}/${asset}"
curl -fsSL "$archive_url" -o "$archive"
tar -xzf "$archive" -C "$tmp"

root="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d -name 'needle-*' | head -1)"
if [[ -z "$root" || ! -x "$root/bin/needle" ]]; then
	echo "downloaded artifact did not contain bin/needle" >&2
	exit 1
fi

mkdir -p "$prefix/bin" "$prefix/share"
cp "$root/bin/needle" "$prefix/bin/needle"
rm -rf "$prefix/share/needle"
cp -R "$root/share/needle" "$prefix/share/needle"

echo "installed: ${prefix}/bin/needle"
if [[ ":${PATH}:" != *":${prefix}/bin:"* ]]; then
	echo "note: ${prefix}/bin is not on PATH"
fi

installed_needle="${prefix}/bin/needle"
if command -v needle >/dev/null 2>&1; then
	path_needle="$(command -v needle)"
	path_dir="$(cd "$(dirname "$path_needle")" && pwd -P)"
	installed_dir="$(cd "$(dirname "$installed_needle")" && pwd -P)"
	path_needle_resolved="${path_dir}/$(basename "$path_needle")"
	installed_needle_resolved="${installed_dir}/$(basename "$installed_needle")"
	if [[ "$path_needle_resolved" != "$installed_needle_resolved" ]]; then
		echo "warning: 'needle' on PATH resolves to ${path_needle_resolved}"
		echo "         use ${installed_needle} or put ${prefix}/bin earlier on PATH"
	fi
fi

if [[ "$run_setup" == 0 ]]; then
	echo "next: ${installed_needle} setup --force"
	exit 0
fi

if [[ -t 1 && -r /dev/tty && -w /dev/tty ]]; then
	echo "starting setup wizard..."
	"$installed_needle" setup --force < /dev/tty
else
	echo "setup wizard not started: no interactive terminal detected"
	echo "next: ${installed_needle} setup --force"
fi
