#!/bin/sh
set -eu

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "기존 터널 서버와 같이 Python 3가 필요합니다." >&2
    exit 1
fi

umask 077
exec python3 ./server.py "$@"
