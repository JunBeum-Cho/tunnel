#!/bin/bash
# Install Caddy once, then operate the server through ./run_server.sh.
set -eu
cd "$(dirname "$0")"

caddyVersion=${CADDY_VERSION:-2.11.4}
case "$(uname -s)" in
    Linux) caddyOS=linux ;;
    Darwin) caddyOS=mac ;;
    *) echo "Unsupported operating system" >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64|amd64) caddyArch=amd64 ;;
    aarch64|arm64) caddyArch=arm64 ;;
    *) echo "Unsupported architecture" >&2; exit 1 ;;
esac
caddyGz=caddy_${caddyVersion}_${caddyOS}_${caddyArch}.tar.gz
taskInstallDir=$(mktemp -d ./.caddy-install.XXXXXX)
trap 'rm -rf "$taskInstallDir"' EXIT
releaseURL=https://github.com/caddyserver/caddy/releases/download/v${caddyVersion}

echo "Downloading Caddy ${caddyVersion} (${caddyOS}/${caddyArch})"
curl --fail --silent --show-error --location --retry 3 \
    -o "$taskInstallDir/$caddyGz" "$releaseURL/$caddyGz"
curl --fail --silent --show-error --location --retry 3 \
    -o "$taskInstallDir/checksums.txt" "$releaseURL/caddy_${caddyVersion}_checksums.txt"
python3 - "$taskInstallDir/$caddyGz" "$taskInstallDir/checksums.txt" <<'PY'
import hashlib
from pathlib import Path
import sys
archive = Path(sys.argv[1])
checksums = dict(line.split(maxsplit=1)[::-1] for line in Path(sys.argv[2]).read_text().splitlines() if line.strip())
expected = checksums.get(archive.name) or checksums.get('*' + archive.name)
# Caddy releases use SHA-512 today; older manifests may use SHA-256.
if not expected or len(expected) not in (64, 128):
    sys.exit('Caddy archive is missing from the release checksum manifest')
digest = hashlib.sha512 if len(expected) == 128 else hashlib.sha256
actual = digest(archive.read_bytes()).hexdigest()
if expected != actual:
    sys.exit('Caddy download checksum mismatch; installation aborted')
PY

# Extract only the binary; keep this repository's LICENSE and README.
tar -xzf "$taskInstallDir/$caddyGz" -C "$taskInstallDir" caddy
chmod +x "$taskInstallDir/caddy"
if [ "$caddyOS" = linux ]; then
    echo "Enabling Caddy to bind low ports"
    sudo setcap 'cap_net_bind_service=+ep' "$taskInstallDir/caddy"
fi
# Replace atomically so an interrupted download cannot break the next restart.
mv -f "$taskInstallDir/caddy" ./caddy
echo "Done. Start the server with ./run_server.sh"
