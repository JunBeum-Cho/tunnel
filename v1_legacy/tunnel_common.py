"""Shared Caddy API and locks for every SirTunnel route writer."""

import fcntl
import json
import os
import re
import socket
import time
from contextlib import contextmanager, suppress
from typing import NamedTuple, Optional
from urllib import request
from urllib.error import HTTPError, URLError

DEFAULT_CADDY_API = "http://127.0.0.1:2019"
SERVER_NAME = "sirtunnel"
ROUTES_PATH = f"/config/apps/http/servers/{SERVER_NAME}/routes"
LOCK_FILE = os.environ.get("SIRTUNNEL_LOCK_FILE", "/var/tmp/sirtunnel-routes.lock")
REQUEST_TIMEOUT = 10
PORT_CHECK_TIMEOUT = 2


class RouteError(RuntimeError):
    pass


class LockTimeout(RouteError):
    pass


class ApiResult(NamedTuple):
    ok: bool
    body: str = ""
    status: Optional[int] = None
    etag: str = ""

    @property
    def missing(self) -> bool:
        return not self.ok and (
            self.status == 404
            or (self.status == 500 and "unknown object ID" in self.body)
        )

    @property
    def error(self) -> str:
        if self.status is None:
            return self.body or "no response"
        return f"HTTP {self.status}: {self.body}"


def api_request(method, url, data=None, timeout=REQUEST_TIMEOUT, etag=""):
    headers = {"Content-Type": "application/json"}
    if etag:
        headers["If-Match"] = etag
    body = json.dumps(data).encode("utf-8") if data is not None else None
    try:
        req = request.Request(url, data=body, headers=headers, method=method)
        # Local admin calls must never go through HTTP_PROXY from a client shell.
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as response:
            return ApiResult(
                True, response.read().decode("utf-8"), response.status,
                response.headers.get("Etag", ""),
            )
    except HTTPError as exc:
        error_body = ""
        with suppress(Exception):
            error_body = exc.read().decode("utf-8")
        return ApiResult(False, error_body, exc.code)
    except URLError as exc:
        return ApiResult(False, f"Connection failed: {exc.reason}")
    except Exception as exc:
        return ApiResult(False, str(exc))


@contextmanager
def file_lock(path=LOCK_FILE, timeout=5):
    """Never proceed without a lock. Never unlink a lock's inode."""
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        # Linux protected_regular forbids O_CREAT on another user's file in
        # sticky /var/tmp, even when the file is intentionally shared.
        fd = os.open(path, flags)
    try:
        # The server reaper and SSH commands may run as different trusted users.
        if os.fstat(fd).st_uid == os.geteuid():
            os.fchmod(fd, 0o666)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"Timed out waiting for lock {path}")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield fd
    finally:
        os.close(fd)


def normalize_host(host):
    host = host.lower().rstrip(".")
    if not host or len(host) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.split(".")
    ):
        raise ValueError(f"Invalid host: {host!r}")
    return host


def check_port_alive(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=PORT_CHECK_TIMEOUT):
            return True
    except OSError:
        return False


def route_port(route):
    for handle in route.get("handle", []):
        if not isinstance(handle, dict) or handle.get("handler") != "reverse_proxy":
            continue
        for upstream in handle.get("upstreams", []):
            if not isinstance(upstream, dict):
                continue
            dial = upstream.get("dial", "")
            if not isinstance(dial, str):
                continue
            for prefix in (":", "127.0.0.1:", "localhost:"):
                if dial.startswith(prefix):
                    value = dial[len(prefix):]
                    if value.isdigit() and 1 <= int(value) <= 65535:
                        return int(value)
    return None


def route_hosts(route):
    hosts = []
    for match in route.get("match", []):
        if not isinstance(match, dict):
            continue
        for host in match.get("host", []):
            if isinstance(host, str):
                hosts.append(host.lower().rstrip("."))
    return hosts


def is_tunnel_route(route):
    port = route_port(route)
    return port is not None and any(
        route.get("@id") == f"{host}-{port}" for host in route_hosts(route)
    )


def read_routes(caddy_api):
    res = api_request("GET", f"{caddy_api.rstrip('/')}{ROUTES_PATH}", timeout=5)
    if not res.ok:
        raise RouteError(f"Cannot read Caddy routes: {res.error}")
    try:
        routes = json.loads(res.body)
    except (ValueError, TypeError) as exc:
        raise RouteError("Caddy returned malformed route JSON") from exc
    if routes is None:
        routes = []
    if not isinstance(routes, list) or any(not isinstance(r, dict) for r in routes):
        raise RouteError("Caddy routes must be an array of objects")
    for route in routes:
        if not isinstance(route.get("handle", []), list) or not isinstance(
            route.get("match", []), list
        ):
            raise RouteError("Caddy returned an invalid route schema")
    return routes, res.etag


def write_routes(caddy_api, routes, etag=""):
    res = api_request("PATCH", f"{caddy_api.rstrip('/')}{ROUTES_PATH}", routes, etag=etag)
    if not res.ok:
        raise RouteError(f"Cannot update Caddy routes: {res.error}")
