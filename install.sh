#!/bin/bash
#
# Download Caddy into this directory and allow it to bind :80/:443.
# Run once on the server, then use ./run_server.sh.

set -eu

caddyVersion=${CADDY_VERSION:-2.1.1}
caddyGz=caddy_${caddyVersion}_linux_amd64.tar.gz

echo "Downloading Caddy ${caddyVersion}"
curl -sf -O -L "https://github.com/caddyserver/caddy/releases/download/v${caddyVersion}/${caddyGz}"

# Extract only the binary. The tarball also carries its own LICENSE and
# README.md at the top level, which would overwrite this repository's copies.
tar xf "${caddyGz}" caddy
rm "${caddyGz}"

echo "Enabling Caddy to bind low ports"
sudo setcap 'cap_net_bind_service=+ep' caddy

echo "Done. Start the server with ./run_server.sh"
