#!/usr/bin/env python3
"""
SirTunnel - Stable Tunnel Client
A robust tunneling solution with automatic reconnection and proper cleanup.

Each client manages exactly one route: its own. Reaping routes whose ports
have died is the job of `tunnel_cleanup.py reap`, which runs once per server
rather than once per client.
"""

import re
import sys
import json
import uuid
import random
import signal
import atexit
import socket
import logging
import threading
from enum import Enum
from urllib import request
from urllib.error import URLError, HTTPError
from http.client import RemoteDisconnected
from typing import NamedTuple, Optional
from contextlib import suppress

# Configuration
DEFAULT_CADDY_API = "http://127.0.0.1:2019"
SERVER_NAME = "sirtunnel"
ROUTES_PATH = f"/config/apps/http/servers/{SERVER_NAME}/routes"
HEALTH_CHECK_INTERVAL = 5  # seconds
HEALTH_CHECK_JITTER = 2  # seconds, keeps N clients from polling in lockstep
INITIAL_RECONNECT_DELAY = 1  # seconds
MAX_RECONNECT_DELAY = 60  # seconds
REQUEST_TIMEOUT = 10  # seconds
HEALTH_REQUEST_TIMEOUT = 5  # seconds
PORT_CHECK_TIMEOUT = 2  # seconds
CLEANUP_LOCK_TIMEOUT = 5  # seconds
MAX_CLEANUP_PASSES = 10
PORT_WAIT_ATTEMPTS = 5  # 1s apart, waiting for sshd to bind the forwarded port

# host and port arrive as arguments of an SSH command line and end up inside
# the Caddy admin API URL path, so they are validated rather than trusted.
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


def check_port_alive(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is open and accepting connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(PORT_CHECK_TIMEOUT)
            return sock.connect_ex((host, port)) == 0
    except Exception:
        return False


def route_port(route: dict) -> Optional[int]:
    """Return the upstream port a tunnel route dials, if it has one."""
    for handle in route.get("handle", []):
        for upstream in handle.get("upstreams", []):
            dial = upstream.get("dial", "")
            if dial.startswith(":"):
                try:
                    return int(dial[1:])
                except ValueError:
                    return None
    return None


class ApiResult(NamedTuple):
    """Outcome of a Caddy admin API call.

    `status` is the HTTP status code, or None when no response ever arrived
    (timeout, connection refused, ...). That distinction is load bearing:
    "Caddy says the route is gone" and "Caddy did not answer" must never be
    collapsed into a single boolean, because the reaction to them is opposite.
    """

    ok: bool
    body: str = ""
    status: Optional[int] = None

    @property
    def missing(self) -> bool:
        """True when Caddy positively reports the object does not exist.

        Caddy answers an unknown @id with `500 unknown object ID`, not 404, so
        testing for 404 alone misreads every "already gone" reply as a hard
        failure.
        """
        if self.ok:
            return False
        if self.status == 404:
            return True
        return self.status == 500 and "unknown object ID" in self.body

    @property
    def error(self) -> str:
        if self.status is None:
            return self.body or "no response"
        return f"HTTP {self.status}: {self.body}"


class DeleteResult(Enum):
    DELETED = "deleted"
    ABSENT = "absent"
    FAILED = "failed"


class HealthStatus(Enum):
    OK = "ok"
    MISSING = "missing"
    TAKEN_OVER = "taken_over"
    UNKNOWN = "unknown"


class TunnelClient:
    """Robust tunnel client with automatic reconnection and cleanup."""

    def __init__(
        self,
        host: str,
        port: int,
        caddy_api: str = DEFAULT_CADDY_API,
        verbose: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.caddy_api = caddy_api.rstrip("/")
        self.tunnel_id = f"{host}-{port}"
        # Identifies this process's route. Two sessions can legitimately share
        # a tunnel_id (host + port) while an old sshd has not yet noticed its
        # client is gone; the owner tag is what lets the loser stand down
        # instead of deleting the route the winner is serving.
        self.owner = f"sirtunnel-{uuid.uuid4().hex[:12]}"
        self.running = False
        self.connected = False
        self.reconnect_delay = INITIAL_RECONNECT_DELAY
        self.lock = threading.Lock()
        self.health_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._cleaned_up = False

        # Setup logging
        log_level = logging.DEBUG if verbose else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.logger = logging.getLogger("sirtunnel")

        # Register cleanup handlers
        atexit.register(self._cleanup)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        # SIGHUP is not available on Windows
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._signal_handler)

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle termination signals gracefully."""
        sig_name = signal.Signals(signum).name
        self.logger.info(f"Received {sig_name}, shutting down...")
        self.stop()

    def _sleep(self, seconds: float) -> bool:
        """Sleep, waking early on shutdown. Returns False if we should stop."""
        return not self._stop_event.wait(seconds)

    def _make_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> ApiResult:
        """Make HTTP request with proper error handling."""
        try:
            headers = {"Content-Type": "application/json"}
            body = json.dumps(data).encode("utf-8") if data is not None else None
            req = request.Request(method=method, url=url, headers=headers)
            with request.urlopen(req, body, timeout=timeout) as response:
                return ApiResult(True, response.read().decode("utf-8"), response.status)
        except HTTPError as e:
            error_body = ""
            with suppress(Exception):
                error_body = e.read().decode("utf-8")
            self.logger.debug(f"HTTP {e.code}: {error_body}")
            return ApiResult(False, error_body, e.code)
        except URLError as e:
            self.logger.debug(f"URL Error: {e.reason}")
            return ApiResult(False, f"Connection failed: {e.reason}")
        except RemoteDisconnected:
            self.logger.debug("Remote disconnected")
            return ApiResult(False, "Remote disconnected")
        except TimeoutError:
            self.logger.debug("Request timeout")
            return ApiResult(False, "Request timeout")
        except Exception as e:
            self.logger.debug(f"Unexpected error: {e}")
            return ApiResult(False, str(e))

    def _check_caddy_available(self) -> ApiResult:
        """Cheap liveness probe for the admin API.

        Deliberately not `GET /config/`: that serialises the whole config under
        a read lock, and every client doing it on a timer is a large part of
        why the admin API gets slow enough to look like an outage.
        """
        url = f"{self.caddy_api}/config/apps/http/servers/{SERVER_NAME}/listen"
        return self._make_request("GET", url, timeout=HEALTH_REQUEST_TIMEOUT)

    def _get_route_config(self) -> dict:
        """Generate Caddy route configuration.

        `group` carries the owner tag. It is a real field of Caddy's route
        schema (unlike an invented `@owner` key, which strict config decoding
        would reject), and a group containing a single route does not change
        matching behaviour.
        """
        return {
            "@id": self.tunnel_id,
            "group": self.owner,
            "match": [{"host": [self.host]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f":{self.port}"}],
                }
            ],
        }

    def _delete_tunnel_by_id(self, tunnel_id: str) -> DeleteResult:
        """Delete tunnel route from Caddy by ID."""
        url = f"{self.caddy_api}/id/{tunnel_id}"
        res = self._make_request("DELETE", url)
        if res.ok:
            self.logger.info(f"Tunnel {tunnel_id} deleted successfully")
            return DeleteResult.DELETED

        if res.missing:
            self.logger.debug(f"Tunnel {tunnel_id} was already removed")
            return DeleteResult.ABSENT

        self.logger.warning(f"Failed to delete tunnel {tunnel_id}: {res.error}")
        return DeleteResult.FAILED

    def _get_all_routes(self) -> list:
        """Get all routes from Caddy."""
        res = self._make_request(
            "GET", f"{self.caddy_api}{ROUTES_PATH}", timeout=HEALTH_REQUEST_TIMEOUT
        )
        if res.ok and res.body:
            try:
                # `or []`: Caddy answers with `null`, not `[]`, when the routes
                # array is absent. Returning None here would raise inside every
                # caller that iterates the result.
                return json.loads(res.body) or []
            except json.JSONDecodeError:
                return []
        return []

    def _find_tunnels_by_host(self, host: str) -> list:
        """Find all tunnel routes matching the given host."""
        matching = []

        for route in self._get_all_routes():
            if not route.get("@id"):
                continue

            for match in route.get("match", []):
                if host in match.get("host", []):
                    matching.append(route)
                    break

        return matching

    def _delete_tunnels_by_host(self, host: str, keep_id: Optional[str] = None) -> bool:
        """Delete every route matching `host`, optionally sparing one @id.

        Repeated passes handle duplicated @ids: Caddy rebuilds its @id index
        from the whole config after every change, so deleting one duplicate
        makes the next one reachable by id again.

        A route that cannot be deleted is skipped rather than aborting the
        sweep, so one stuck entry no longer shields every route behind it.
        """
        for _ in range(MAX_CLEANUP_PASSES):
            matching = [
                route
                for route in self._find_tunnels_by_host(host)
                if route.get("@id") != keep_id
            ]
            if not matching:
                return True

            progressed = False
            for route in matching:
                route_id = route["@id"]
                self.logger.info(f"Removing existing tunnel for {host}: {route_id}")
                if self._delete_tunnel_by_id(route_id) is not DeleteResult.FAILED:
                    progressed = True

            if not progressed:
                self.logger.warning(
                    f"Could not remove any remaining route for {host}, giving up"
                )
                return False

        self.logger.warning(
            f"Reached max passes ({MAX_CLEANUP_PASSES}) cleaning up tunnels for {host}"
        )
        return False

    def _remove_dead_routes_for_host(self) -> None:
        """Drop routes for our host whose upstream port is gone.

        Scoped to our own host on purpose. The previous global sweep had every
        client probing and deleting every other client's routes, so a single
        slow probe took down a healthy tunnel belonging to someone else.
        """
        for route in self._find_tunnels_by_host(self.host):
            route_id = route["@id"]
            if route_id == self.tunnel_id:
                continue

            port = route_port(route)
            if port is None or check_port_alive(port):
                continue

            self.logger.info(f"Removing dead route {route_id} shadowing {self.host}")
            self._delete_tunnel_by_id(route_id)

    def _host_taken_by_live_tunnel(self, host: str) -> bool:
        """Check if a *live* tunnel with a different ID is serving this host.

        Liveness matters: a leftover route pointing at a dead port is garbage
        to clean up, not a peer that has taken over. Treating the two the same
        is how a healthy tunnel used to talk itself into shutting down.
        """
        for route in self._find_tunnels_by_host(host):
            if route["@id"] == self.tunnel_id:
                continue

            port = route_port(route)
            if port is not None and check_port_alive(port):
                return True

        return False

    def _upsert_route(self) -> bool:
        """Install our route idempotently.

        `PATCH /id/<id>` replaces the object in place, so a reconnect can never
        append a second route carrying the same @id. Only when Caddy positively
        reports the id is unknown do we append to the array.
        """
        # A reconnect already in flight when shutdown starts must not put the
        # route back after _cleanup deleted it, which would leave an orphan
        # behind for the reaper to find.
        if self._stop_event.is_set():
            return False

        config = self._get_route_config()

        res = self._make_request(
            "PATCH", f"{self.caddy_api}/id/{self.tunnel_id}", config
        )
        if res.ok:
            self.logger.info(
                f"Tunnel refreshed: https://{self.host} -> localhost:{self.port}"
            )
            return True

        if not res.missing:
            self.logger.error(f"Failed to update tunnel: {res.error}")
            return False

        res = self._make_request("POST", f"{self.caddy_api}{ROUTES_PATH}", config)
        if res.ok:
            self.logger.info(
                f"Tunnel created: https://{self.host} -> localhost:{self.port}"
            )
            return True

        self.logger.error(f"Failed to create tunnel: {res.error}")
        return False

    def _claim_host(self) -> bool:
        """Take ownership of the host, replacing whatever was serving it."""
        self._delete_tunnels_by_host(self.host, keep_id=self.tunnel_id)
        return self._upsert_route()

    def _check_tunnel_health(self) -> HealthStatus:
        """Classify the state of our route in Caddy."""
        url = f"{self.caddy_api}/id/{self.tunnel_id}"
        res = self._make_request("GET", url, timeout=HEALTH_REQUEST_TIMEOUT)

        if res.missing:
            return HealthStatus.MISSING

        if not res.ok:
            # Timeout, refused, or an error we do not understand. The route is
            # most likely still fine; rewriting the config on this signal is
            # what turned a slow admin API into a broken one.
            self.logger.debug(f"Health check inconclusive: {res.error}")
            return HealthStatus.UNKNOWN

        owner = ""
        with suppress(Exception):
            owner = (json.loads(res.body) or {}).get("group", "")

        if owner and owner != self.owner:
            return HealthStatus.TAKEN_OVER

        return HealthStatus.OK

    def _reconnect(self) -> bool:
        """Restore our route after Caddy reported it missing."""
        self._remove_dead_routes_for_host()

        if self._host_taken_by_live_tunnel(self.host):
            self.logger.info(
                f"Another live tunnel has taken over {self.host}, shutting down..."
            )
            self.running = False
            self._stop_event.set()
            return False

        return self._upsert_route()

    def _health_check_loop(self) -> None:
        """Background thread for health monitoring and reconnection.

        Every cycle is wrapped: an exception escaping here would kill the
        thread while the process stayed alive, leaving a tunnel that nobody
        monitors and nothing repairs -- and no log line saying so.
        """
        failures = 0

        while self.running:
            interval = HEALTH_CHECK_INTERVAL + random.uniform(0, HEALTH_CHECK_JITTER)
            if not self._sleep(interval):
                return

            try:
                with self.lock:
                    status = self._check_tunnel_health()

                    if status is HealthStatus.OK:
                        if not self.connected:
                            self.logger.info("Tunnel connection restored")
                            self.connected = True
                        failures = 0
                        self.reconnect_delay = INITIAL_RECONNECT_DELAY
                        continue

                    if status is HealthStatus.UNKNOWN:
                        self.logger.warning(
                            "Caddy API did not answer, leaving the route untouched"
                        )
                        continue

                    if status is HealthStatus.TAKEN_OVER:
                        self.logger.info(
                            f"A newer session owns {self.tunnel_id}, shutting down..."
                        )
                        self.connected = False
                        self.running = False
                        self._stop_event.set()
                        return

                    self.connected = False
                    failures += 1
                    self.logger.warning(f"Health check failed (attempt {failures})")

                    # Our own forward first. Re-creating the route while the
                    # port is gone only publishes 502s, and the reaper would
                    # delete it every 30s while we put it back every 5 -- the
                    # tunnel would neither recover nor fail. Retry on the plain
                    # health interval rather than the reconnect backoff: this
                    # is a cheap local check, and the route should come back
                    # promptly once the forward does.
                    if not check_port_alive(self.port):
                        self.logger.warning(
                            f"Port {self.port} is not listening, "
                            "leaving the route absent"
                        )
                        continue

                    restored = self._reconnect()

                # Backoff happens with the lock released, so a shutdown or a
                # cleanup pass is never stuck behind a sleeping reconnect.
                if restored:
                    self.connected = True
                    failures = 0
                    self.reconnect_delay = INITIAL_RECONNECT_DELAY
                elif self.running:
                    delay = self.reconnect_delay
                    self.reconnect_delay = min(delay * 2, MAX_RECONNECT_DELAY)
                    self.logger.info(f"Retrying in {delay}s...")
                    if not self._sleep(delay):
                        return
            except Exception:
                self.logger.exception("Health check cycle failed, retrying")

    def _cleanup(self) -> None:
        """Remove our route on exit, but only while it is still ours.

        A manual reconnect can leave two processes sharing one tunnel_id.
        Without this check the dying process deletes the route the new session
        is serving, which reads to the user as "it dropped again".
        """
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True

        # Serialise against a reconnect in flight, but never wait long for it:
        # sshd will not hold a SIGHUP'd session open while we block on a lock.
        held = self.lock.acquire(timeout=CLEANUP_LOCK_TIMEOUT)
        try:
            url = f"{self.caddy_api}/id/{self.tunnel_id}"
            res = self._make_request("GET", url, timeout=HEALTH_REQUEST_TIMEOUT)

            if res.missing:
                return

            if not res.ok:
                self.logger.warning(f"Skipping cleanup, Caddy unreachable: {res.error}")
                return

            owner = ""
            with suppress(Exception):
                owner = (json.loads(res.body) or {}).get("group", "")

            if owner and owner != self.owner:
                self.logger.info(
                    f"Route {self.tunnel_id} belongs to a newer session, leaving it"
                )
                return

            self.logger.info("Cleaning up tunnel...")
            self._delete_tunnel_by_id(self.tunnel_id)
            self.connected = False
        finally:
            if held:
                self.lock.release()

    def _wait_for_local_port(self) -> bool:
        """Wait for sshd to bind the forwarded port before publishing a route.

        The listener belongs to sshd, so this answers "did the -R forward come
        up", not "is the app behind it ready". Publishing a route without it is
        how a typo in the -R argument turns into a tunnel that serves 502s
        forever instead of failing at startup.
        """
        for attempt in range(PORT_WAIT_ATTEMPTS):
            if check_port_alive(self.port):
                return True
            if attempt == 0:
                self.logger.info(f"Waiting for port {self.port} to come up...")
            if not self._sleep(1):
                return False
        return False

    def start(self) -> None:
        """Start the tunnel client."""
        self.logger.info(f"Starting tunnel: {self.host}:{self.port}")
        self.logger.info(f"Tunnel ID: {self.tunnel_id} (owner {self.owner})")

        res = self._check_caddy_available()
        if not res.ok:
            self.logger.error(
                f"Cannot connect to Caddy API at {self.caddy_api}: {res.error}"
            )
            self.logger.error("Make sure Caddy is running with the admin API enabled")
            sys.exit(1)

        if not self._wait_for_local_port():
            self.logger.error(
                f"Nothing is listening on port {self.port}: the SSH remote forward "
                "is not up. Check the -R argument and add -o ExitOnForwardFailure=yes."
            )
            sys.exit(1)

        if not self._claim_host():
            self.logger.error("Failed to create initial tunnel")
            sys.exit(1)

        self.running = True
        self.connected = True

        self.health_thread = threading.Thread(
            target=self._health_check_loop, daemon=True
        )
        self.health_thread.start()

        self.logger.info("Tunnel is active. Press Ctrl+C to stop.")
        self.logger.info(
            f"Forwarding: https://{self.host} -> http://localhost:{self.port}"
        )

        # Main loop. It also watches the health thread: if that thread ever
        # goes away, nothing is monitoring the route any more, and exiting is
        # far better than lingering as a process that looks healthy while its
        # tunnel silently rots. Whatever supervises the SSH session restarts us.
        try:
            while self.running:
                self._stop_event.wait(HEALTH_CHECK_INTERVAL)
                if not self.running:
                    break
                if self.health_thread and not self.health_thread.is_alive():
                    self.logger.error("Health check thread is gone, shutting down")
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the tunnel client."""
        was_running = self.running
        self.running = False
        self._stop_event.set()
        self._cleanup()
        if was_running:
            self.logger.info("Tunnel stopped")


def main():
    """Main entry point."""
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <host> <port>")
        print(f"Example: {sys.argv[0]} tunnel.example.com 8899")
        sys.exit(1)

    host, port = sys.argv[1], sys.argv[2]

    if not HOST_PATTERN.match(host):
        print(f"Invalid host: {host!r}")
        sys.exit(1)

    if not port.isdigit() or not 1 <= int(port) <= 65535:
        print(f"Invalid port: {port!r}")
        sys.exit(1)

    client = TunnelClient(host=host, port=int(port))
    client.start()


if __name__ == "__main__":
    main()
