#!/usr/bin/env python3
"""Register and maintain one SSH remote forward in Caddy."""

import atexit
import json
import logging
import random
import signal
import sys
import threading
import uuid
from enum import Enum
from typing import Optional

from tunnel_common import (
    DEFAULT_CADDY_API, LOCK_FILE, ROUTES_PATH, SERVER_NAME,
    ApiResult, RouteError, api_request, check_port_alive, file_lock,
    is_tunnel_route, normalize_host, read_routes, route_hosts, route_port,
    write_routes,
)

HEALTH_CHECK_INTERVAL = 5
HEALTH_CHECK_JITTER = 2
INITIAL_RECONNECT_DELAY = 1
MAX_RECONNECT_DELAY = 30
HEALTH_REQUEST_TIMEOUT = 5
CLEANUP_LOCK_TIMEOUT = 5
PORT_WAIT_ATTEMPTS = 5
PORT_FAILURE_LIMIT = 3
# Caddy durations are nanoseconds in JSON. Retain upgraded connections briefly
# when a different tunnel causes the shared HTTP configuration to be reloaded.
STREAM_CLOSE_DELAY = 300 * 1_000_000_000


class HostConflict(RouteError):
    pass


class HealthStatus(Enum):
    OK = "ok"
    MISSING = "missing"
    TAKEN_OVER = "taken_over"
    UNKNOWN = "unknown"


class TunnelClient:
    def __init__(self, host, port, caddy_api=DEFAULT_CADDY_API, verbose=False):
        self.host = normalize_host(host)
        self.port = int(port)
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        self.caddy_api = caddy_api.rstrip("/")
        self.tunnel_id = f"{self.host}-{self.port}"
        self.owner = f"sirtunnel-{uuid.uuid4().hex}"
        self.running = False
        self.connected = False
        self.exit_code = 0
        self.lock = threading.Lock()
        self.health_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cleaned_up = False
        self._registered = False
        self._owns_port_lease = False
        self._stream_close_delay_supported = True
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        self.logger = logging.getLogger("sirtunnel")
        atexit.register(self._cleanup)
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), self._signal_handler)

    def _signal_handler(self, signum, frame):
        # A signal may arrive while the main thread holds a mutation lock.
        # Cleanup runs after the operation unwinds, never inside the handler.
        self.logger.info("Received %s, shutting down", signal.Signals(signum).name)
        # autossh treats a successful remote exit as an intentional shutdown.
        self.exit_code = 128 + signum
        self.running = False
        self._stop_event.set()

    def _sleep(self, seconds):
        return not self._stop_event.wait(seconds)

    def _make_request(self, method, url, data=None, timeout=10):
        return api_request(method, url, data, timeout)

    def _get_route_config(self):
        proxy = {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": f"127.0.0.1:{self.port}"}],
        }
        if self._stream_close_delay_supported:
            proxy["stream_close_delay"] = STREAM_CLOSE_DELAY
        return {
            "@id": self.tunnel_id,
            "group": self.owner,
            "match": [{"host": [self.host]}],
            "handle": [proxy],
        }

    def _route_is_ours(self, route):
        return (
            isinstance(route, dict)
            and route.get("@id") == self.tunnel_id
            and route.get("group") == self.owner
            and route_hosts(route) == [self.host]
            and route_port(route) == self.port
        )

    def _claim_host(self):
        """Atomically claim a host; live services on another port win."""
        if not self._owns_port_lease:
            raise RouteError("Cannot register without the remote-port lease")
        with self.lock, file_lock():
            if self._stop_event.is_set() or not check_port_alive(self.port):
                return False
            routes, etag = read_routes(self.caddy_api)
            kept = []
            for route in routes:
                same_host = self.host in route_hosts(route)
                same_port = route_port(route) == self.port
                same_id = route.get("@id") == self.tunnel_id
                if not (same_host or same_port or same_id):
                    kept.append(route)
                    continue
                managed = is_tunnel_route(route) or route.get("group") == self.owner
                if not managed:
                    if same_host or same_id:
                        raise HostConflict(f"{self.host} has a route not managed by SirTunnel")
                    kept.append(route)
                    continue
                existing_port = route_port(route)
                if same_host and not same_port and (
                    existing_port is None or check_port_alive(existing_port)
                ):
                    raise HostConflict(
                        f"{self.host} is already served by a live tunnel on port {existing_port}"
                    )
                # Holding the port lease rules out another current client on
                # this port, even when a stale route's port has been reused.
                # Remove old hostname aliases and duplicates in the same PATCH.
            desired = self._get_route_config()
            kept.append(desired)
            if self._stop_event.is_set():
                return False
            # A request can time out after Caddy committed it. Cleanup must
            # still check whether we own a route in that case.
            self._registered = True
            # Retrying a request that already committed must not reload Caddy
            # again or move an unchanged route to the end of the array.
            if len(kept) != len(routes) or desired not in routes:
                try:
                    write_routes(self.caddy_api, kept, etag)
                except RouteError as exc:
                    # Older Caddy releases reject this field before committing
                    # the configuration. Preserve registration compatibility;
                    # timeouts, ETag conflicts and other errors still propagate.
                    error = str(exc).replace('\\"', '"')
                    if not self._stream_close_delay_supported or 'unknown field "stream_close_delay"' not in error:
                        raise
                    self._stream_close_delay_supported = False
                    kept[-1] = self._get_route_config()
                    self.logger.warning("Caddy lacks stream_close_delay; upgrade via ./install.sh to protect WebSockets during route updates")
                    write_routes(self.caddy_api, kept, etag)
            self.logger.info("Tunnel registered: https://%s -> 127.0.0.1:%s", self.host, self.port)
            return True

    def _check_tunnel_health(self):
        res = self._make_request(
            "GET", f"{self.caddy_api}/id/{self.tunnel_id}", timeout=HEALTH_REQUEST_TIMEOUT
        )
        if res.missing:
            return HealthStatus.MISSING
        if not res.ok:
            self.logger.debug("Health check inconclusive: %s", res.error)
            return HealthStatus.UNKNOWN
        try:
            route = json.loads(res.body)
            if not isinstance(route, dict):
                return HealthStatus.UNKNOWN
            if route.get("group") != self.owner:
                return HealthStatus.TAKEN_OVER
            return HealthStatus.OK if self._route_is_ours(route) else HealthStatus.MISSING
        except (ValueError, TypeError, AttributeError):
            return HealthStatus.UNKNOWN

    def _health_check_loop(self):
        port_failures = 0
        delay = INITIAL_RECONNECT_DELAY
        while self.running:
            if not self._sleep(HEALTH_CHECK_INTERVAL + random.uniform(0, HEALTH_CHECK_JITTER)):
                return
            try:
                # Test the SSH listener even when Caddy's route still exists.
                # A dead SSH session must exit so autossh can reconnect it.
                if not check_port_alive(self.port):
                    port_failures += 1
                    self.logger.warning("SSH port %s is gone (%s/%s)", self.port, port_failures, PORT_FAILURE_LIMIT)
                    if port_failures >= PORT_FAILURE_LIMIT:
                        self.exit_code = 1
                        self.running = False
                        self._stop_event.set()
                        return
                    continue
                port_failures = 0
                status = self._check_tunnel_health()
                if status is HealthStatus.OK:
                    self.connected = True
                    delay = INITIAL_RECONNECT_DELAY
                    continue
                if status is HealthStatus.UNKNOWN:
                    self.logger.warning("Caddy API unavailable; retaining the current route")
                    continue
                if status is HealthStatus.TAKEN_OVER:
                    self.logger.error("Route %s belongs to another session; exiting", self.tunnel_id)
                    self.exit_code = 1
                    self.running = False
                    self._stop_event.set()
                    return
                self.connected = False
                if self._claim_host():
                    self.connected = True
                    delay = INITIAL_RECONNECT_DELAY
                elif not self._sleep(delay):
                    return
            except HostConflict as exc:
                self.logger.error("%s; exiting", exc)
                self.exit_code = 1
                self.running = False
                self._stop_event.set()
                return
            except Exception:
                self.logger.exception("Route recovery failed; retrying in %ss", delay)
                if not self._sleep(delay):
                    return
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    def _cleanup(self):
        if self._cleaned_up or not self._registered:
            return
        if not self.lock.acquire(timeout=CLEANUP_LOCK_TIMEOUT):
            self.logger.warning("Cleanup lock busy; leaving cleanup for a retry or the reaper")
            return
        try:
            if self._cleaned_up:
                return
            with file_lock(timeout=CLEANUP_LOCK_TIMEOUT):
                routes, etag = read_routes(self.caddy_api)
                kept = [route for route in routes if not self._route_is_ours(route)]
                if len(kept) != len(routes):
                    write_routes(self.caddy_api, kept, etag)
                    self.logger.info("Removed our route %s", self.tunnel_id)
                self._cleaned_up = True
                self.connected = False
        except Exception as exc:
            self.logger.warning("Cleanup deferred: %s", exc)
        finally:
            self.lock.release()

    def _wait_for_local_port(self):
        for _ in range(PORT_WAIT_ATTEMPTS):
            if check_port_alive(self.port):
                return True
            if not self._sleep(1):
                return False
        return False

    def start(self):
        self.logger.info("Starting %s (owner %s)", self.tunnel_id, self.owner)
        # One process per remote port, held until cleanup completes. Unlike
        # the route owner, the lease survives Caddy losing its entire config.
        with file_lock(f"{LOCK_FILE}.port-{self.port}", timeout=5):
            self._owns_port_lease = True
            try:
                if not self._wait_for_local_port():
                    raise RouteError(f"SSH port {self.port} is not listening; check -R and ExitOnForwardFailure=yes")
                delay = INITIAL_RECONNECT_DELAY
                while not self._stop_event.is_set():
                    try:
                        if self._claim_host():
                            break
                    except HostConflict:
                        raise
                    except RouteError as exc:
                        self.logger.warning("Registration deferred: %s; retrying in %ss", exc, delay)
                    if not check_port_alive(self.port):
                        raise RouteError(f"SSH port {self.port} disappeared during registration")
                    if not self._sleep(delay):
                        return
                    delay = min(delay * 2, MAX_RECONNECT_DELAY)
                if self._stop_event.is_set():
                    return
                self.running = self.connected = True
                self.health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
                self.health_thread.start()
                while self.running:
                    self._stop_event.wait(HEALTH_CHECK_INTERVAL)
                    if not self.health_thread.is_alive() and self.running:
                        self.logger.error("Health monitoring stopped; exiting")
                        self.exit_code = 1
                        break
            finally:
                self.stop()
                self._owns_port_lease = False

    def stop(self):
        self.running = False
        self._stop_event.set()
        self._cleanup()


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <host> <port>", file=sys.stderr)
        return 1
    try:
        client = TunnelClient(sys.argv[1], int(sys.argv[2]))
        client.start()
        return client.exit_code
    except (ValueError, RouteError, OSError) as exc:
        print(f"Tunnel failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
