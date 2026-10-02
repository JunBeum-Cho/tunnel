#!/bin/sh
set -eu

cd "$(dirname "$0")"

sh ./run_server.sh install

if [ "$(uname -s)" = Linux ]; then
    if ! command -v setcap >/dev/null 2>&1; then
        echo "setcap is required. On Debian/Ubuntu, run: sudo apt-get install libcap2-bin" >&2
        exit 1
    fi

    sish_binary=$(python3 -c 'from config import Settings; from install_sish import ensure_binary; print(ensure_binary(Settings()))')
    echo "Granting sish permission to bind HTTP/HTTPS ports..."
    if [ "$(id -u)" -eq 0 ]; then
        setcap 'cap_net_bind_service=+ep' "$sish_binary"
    else
        sudo setcap 'cap_net_bind_service=+ep' "$sish_binary"
    fi
    echo "CAP_NET_BIND_SERVICE enabled for: $sish_binary"
fi

echo "Installation complete. Start the server with: sh run_server.sh"
