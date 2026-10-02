#!/bin/sh
set -eu

cd "$(dirname "$0")"
LC_ALL=C
export LC_ALL

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "Root permission is required for installation. Install sudo or run this installer as root." >&2
        return 1
    fi
}

sh ./run_server.sh install

# Use the same literal .env parser and environment precedence as run_server.sh.
# Only validated port numbers are split into shell arguments; never source .env.
ports=$(python3 -c 'from config import Settings; s = Settings(); print(s.http, s.https, s.ssh)')
bind_address=$(python3 -c 'from config import Settings; print(Settings().bind)')

if [ "$(uname -s)" = Linux ]; then
    if ! command -v setcap >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
            echo "Installing libcap2-bin for HTTP/HTTPS port permissions..."
            as_root apt-get install -y libcap2-bin
        else
            echo "setcap is required. Install your distribution's libcap package and rerun sh install.sh." >&2
            exit 1
        fi
        if ! command -v setcap >/dev/null 2>&1; then
            echo "setcap is still unavailable after installing libcap2-bin." >&2
            exit 1
        fi
    fi

    sish_binary=$(python3 -c 'from config import Settings; from install_sish import ensure_binary; print(ensure_binary(Settings()))')
    echo "Granting sish permission to bind HTTP/HTTPS ports..."
    as_root setcap 'cap_net_bind_service=+ep' "$sish_binary"
    echo "CAP_NET_BIND_SERVICE enabled for: $sish_binary"

    firewall_configured=false
    if command -v ufw >/dev/null 2>&1; then
        echo "Adding UFW inbound TCP rules for: $ports"
        for port in $ports; do
            as_root ufw allow "$port/tcp"
        done
        ufw_status=$(as_root ufw status)
        printf '%s\n' "$ufw_status"
        case "$ufw_status" in
            *"Status: inactive"*)
                echo "UFW is inactive. Rules are saved for future use; UFW remains inactive."
                ;;
        esac
        firewall_configured=true
    fi

    if command -v firewall-cmd >/dev/null 2>&1 && as_root firewall-cmd --state >/dev/null 2>&1; then
        # Cover interface/source zones as well as the default zone. Apply runtime
        # and persistent rules individually, without reloading unrelated rules.
        active_zones=$(as_root firewall-cmd --get-active-zones)
        zones=$(printf '%s\n' "$active_zones" | awk '/^[^[:space:]]/ {printf "%s ", $1}')
        default_zone=$(as_root firewall-cmd --get-default-zone)
        case " $zones " in
            *" $default_zone "*) ;;
            *) zones="$zones $default_zone" ;;
        esac
        for zone in $zones; do
            echo "Adding firewalld inbound TCP rules in zone $zone for: $ports"
            for port in $ports; do
                if as_root firewall-cmd --zone="$zone" --query-port="$port/tcp" >/dev/null; then
                    :
                else
                    query_result=$?
                    if [ "$query_result" -ne 1 ]; then
                        echo "Could not query firewalld rules in zone $zone (exit $query_result)." >&2
                        exit "$query_result"
                    fi
                    as_root firewall-cmd --zone="$zone" --add-port="$port/tcp"
                fi
                if as_root firewall-cmd --permanent --zone="$zone" --query-port="$port/tcp" >/dev/null; then
                    :
                else
                    query_result=$?
                    if [ "$query_result" -ne 1 ]; then
                        echo "Could not query permanent firewalld rules in zone $zone (exit $query_result)." >&2
                        exit "$query_result"
                    fi
                    as_root firewall-cmd --permanent --zone="$zone" --add-port="$port/tcp"
                fi
            done
        done
        firewall_configured=true
    fi

    if [ "$firewall_configured" = false ]; then
        echo "No UFW or running firewalld found."
        echo "If you use custom nftables/iptables rules, allow inbound TCP ports: $ports"
    fi
else
    echo "Linux firewall and port permission setup skipped on $(uname -s)."
fi

echo "sish listen address: $bind_address; HTTP/HTTPS/SSH TCP ports: $ports"
case "$bind_address" in
    127.*|::1)
        echo "WARNING: This is a loopback address. Remote service Docker containers cannot connect."
        echo "For a public VPS, set SISH_BIND_ADDRESS=0.0.0.0 in .env and restart sish."
        ;;
esac
echo "Cloud firewall check: if this VPS has an attached Vultr Firewall Group,"
echo "allow inbound TCP ports $ports there as well (IPv4 source 0.0.0.0/0; IPv6 source ::/0 if used)."
echo "Vultr Firewall is managed in your Vultr account, separately from this VPS firewall."
echo "Local installation complete. Start the server with: sh run_server.sh"
