#!/bin/bash
set -eu

cd "$(dirname "$0")"

usage() {
    echo "Usage: $0 [start|status|stop|restart|logs|check|--foreground]" >&2
}

if [ "$#" -gt 1 ]; then
    usage
    exit 2
fi

action="${1:-start}"
case "$action" in
    start|status|stop|restart|logs|check|--foreground) ;;
    *) usage; exit 2 ;;
esac

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Docker와 Docker Compose v2 이상이 필요합니다." >&2
    exit 1
fi

compose() {
    docker compose -f compose.yml "$@"
}

if [ ! -f .env ] && [ -z "${SSH_PASSWORD:-}" ]; then
    echo "cp .env.example .env 후 서비스 Docker와 같은 SSH_PASSWORD를 설정하세요." >&2
    exit 1
fi

case "$action" in
    start|restart|--foreground)
        if ! command -v python3 >/dev/null 2>&1; then
            echo "시작 상태 확인에 Python 3가 필요합니다." >&2
            exit 1
        fi
        umask 077
        mkdir -p .runtime/{ssl,keys,pubkeys}
        ;;
esac

case "$action" in
    start|restart)
        compose config --quiet
        if [ "$action" = restart ]; then
            compose up -d --force-recreate sish
        else
            compose up -d sish
        fi
        python3 ./wait_ready.py \
            "$(compose port sish 80)" \
            "$(compose port sish 443)" \
            "$(compose port sish 2222)"
        ;;
    status) exec docker compose -f compose.yml ps -a sish ;;
    stop) exec docker compose -f compose.yml stop sish ;;
    logs) exec docker compose -f compose.yml logs --tail 100 -f sish ;;
    check)
        compose config --quiet
        echo "sish Compose 설정이 유효합니다."
        ;;
    --foreground)
        compose config --quiet
        exec docker compose -f compose.yml up sish
        ;;
esac

