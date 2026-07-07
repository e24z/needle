#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

version="$(sed -n 's/^version = "\(.*\)"/\1/p' crates/needle-manager/Cargo.toml | head -1)"
host="$(rustc -vV | sed -n 's/^host: //p')"
dist="dist"
python_bin=""

while [[ $# -gt 0 ]]; do
	case "$1" in
		--version)
			version="$2"
			shift 2
			;;
		--host)
			host="$2"
			shift 2
			;;
		--dist)
			dist="$2"
			shift 2
			;;
		--python)
			python_bin="$2"
			shift 2
			;;
		*)
			echo "unknown option: $1" >&2
			exit 2
			;;
	esac
done

if [[ -z "$python_bin" ]]; then
	if command -v python3.13 >/dev/null 2>&1; then
		python_bin=python3.13
	else
		python_bin=python3
	fi
fi

package="needle-${version}-${host}"
asset="needle-${host}.tar.gz"
stage="${dist}/${package}"

rm -rf "$stage"
mkdir -p "$stage/bin" "$stage/share/needle/wheels" "$stage/share/needle/pi"

cargo build --release --locked
rm -rf "$root/python/build" "$root/python/needle_worker.egg-info"
"$python_bin" -m pip wheel --no-deps --wheel-dir "$stage/share/needle/wheels" "$root/python"

cp target/release/needle "$stage/bin/"
cp -R pi/. "$stage/share/needle/pi/"
rm -rf "$stage/share/needle/pi/node_modules"
(cd "$stage/share/needle/pi" && npm ci --omit=dev)
cp README.md "$stage/"

COPYFILE_DISABLE=1 tar -C "$dist" -czf "${dist}/${asset}" "$package"
(
	cd "$dist"
	shasum -a 256 "$asset" > "${asset}.sha256"
)
echo "${dist}/${asset}"
