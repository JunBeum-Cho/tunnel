#!/usr/bin/env python3
"""Wait for HTTP, the HTTPS listener and the SSH banner without issuing a cert."""

import argparse
import http.client
import socket
import sys
import time


def address(published):
    host, port = published.strip().splitlines()[0].rsplit(":", 1)
    host = host.strip("[]")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1" if host != "::" else "::1"
    return host, int(port)


def probe(http_address, https_address, ssh_address):
    connection = http.client.HTTPConnection(*http_address, timeout=1)
    try:
        connection.request("GET", "/", headers={"Host": "localhost"})
        response = connection.getresponse()
        if response.status >= 500:
            raise OSError("HTTP returned {}".format(response.status))
    finally:
        connection.close()

    with socket.create_connection(https_address, timeout=1):
        pass
    with socket.create_connection(ssh_address, timeout=1) as connection:
        if not connection.recv(255).startswith(b"SSH-2.0-"):
            raise OSError("SSH banner is not ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("http_address")
    parser.add_argument("https_address")
    parser.add_argument("ssh_address")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        addresses = [address(value) for value in (
            args.http_address, args.https_address, args.ssh_address,
        )]
    except (ValueError, IndexError) as error:
        parser.error("Cannot determine published ports: {}".format(error))

    deadline = time.monotonic() + args.timeout
    error = None
    while time.monotonic() < deadline:
        try:
            probe(*addresses)
            print("sish 준비 완료: HTTP {}, HTTPS {}, SSH {}".format(
                addresses[0][1], addresses[1][1], addresses[2][1],
            ))
            return 0
        except (OSError, http.client.HTTPException) as failure:
            error = failure
            time.sleep(0.25)
    print("sish 시작 확인 실패: {}. ./run_server.sh logs로 확인하세요.".format(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

