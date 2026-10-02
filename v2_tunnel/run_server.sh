#!/bin/sh
set -eu

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required, as with the existing tunnel server." >&2
    exit 1
fi

umask 077
exec python3 ./server.py "$@"
