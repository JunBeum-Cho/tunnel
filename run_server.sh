#!/bin/bash

set -eu

cd "$(dirname "$0")"

if [ ! -x ./caddy ]; then
    echo "caddy binary not found in $(pwd). Run ./install.sh first." >&2
    exit 1
fi

exec ./caddy run --config caddy_config.json
