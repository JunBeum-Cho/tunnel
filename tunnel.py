#!/usr/bin/env python3
"""
SirTunnel - Stable Tunnel Client
A robust tunneling solution with automatic reconnection and proper cleanup.
"""

import sys
import json
import time
import signal
import atexit
import socket
import logging
import threading
from urllib import request
from urllib.error import URLError, HTTPError
from http.client import RemoteDisconnected
from typing import Optional
from contextlib import suppress

# Configuration
DEFAULT_CADDY_API = "http://127.0.0.1:2019"
HEALTH_CHECK_INTERVAL = 5  # seconds
ORPHAN_CHECK_INTERVAL = 30  # seconds
MAX_RECONNECT_ATTEMPTS = 0  # 0 = infinite
INITIAL_RECONNECT_DELAY = 1  # seconds
MAX_RECONNECT_DELAY = 60  # seconds
REQUEST_TIMEOUT = 10  # seconds
PORT_CHECK_TIMEOUT = 2  # seconds


def check_port_alive(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is open and accepting connections."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(PORT_CHECK_TIMEOUT)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


class TunnelClient:
    """Robust tunnel client with automatic reconnection and cleanup."""

    def __init__(
        self,
        host: str,
        port: str,
        caddy_api: str = DEFAULT_CADDY_API,
        verbose: bool = False,
    ):
        self.host = host
        self.port = port
        self.caddy_api = caddy_api.rstrip("/")
        self.tunnel_id = f"{host}-{port}"
        self.running = False
        self.connected = False
        self.reconnect_delay = INITIAL_RECONNECT_DELAY
        self.lock = threading.Lock()
        self.health_thread: Optional[threading.Thread] = None
        self.orphan_thread: Optional[threading.Thread] = None

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

    def _make_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> tuple[bool, Optional[str]]:
        """Make HTTP request with proper error handling."""
        try:
            headers = {"Content-Type": "application/json"}
            body = json.dumps(data).encode("utf-8") if data else None
            req = request.Request(method=method, url=url, headers=headers)
            with request.urlopen(req, body, timeout=timeout) as response:
                return True, response.read().decode("utf-8")
        except HTTPError as e:
            error_body = ""
            with suppress(Exception):
                error_body = e.read().decode("utf-8")
            self.logger.debug(f"HTTP {e.code}: {error_body}")
            return False, f"HTTP {e.code}: {error_body}"
        except URLError as e:
            self.logger.debug(f"URL Error: {e.reason}")
            return False, f"Connection failed: {e.reason}"
        except RemoteDisconnected:
            self.logger.debug("Remote disconnected")
            return False, "Remote disconnected"
        except TimeoutError:
            self.logger.debug("Request timeout")
            return False, "Request timeout"
        except Exception as e:
            self.logger.debug(f"Unexpected error: {e}")
            return False, str(e)

    def _check_caddy_available(self) -> bool:
        """Check if Caddy API is available."""
        success, _ = self._make_request("GET", f"{self.caddy_api}/config/", timeout=5)
        return success

    def _get_route_config(self) -> dict:
        """Generate Caddy route configuration."""
        return {
            "@id": self.tunnel_id,
            "match": [{"host": [self.host]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f":{self.port}"}],
                }
            ],
        }

    def _delete_tunnel_by_id(self, tunnel_id: str) -> bool:
        """Delete tunnel route from Caddy by ID."""
        url = f"{self.caddy_api}/id/{tunnel_id}"
        success, error = self._make_request("DELETE", url)
        if success:
            self.logger.info(f"Tunnel {tunnel_id} deleted successfully")
            return True

        # 404 means it's already gone, which is fine
        if "404" in str(error):
            self.logger.debug(f"Tunnel {tunnel_id} was already removed")
            return True

        self.logger.warning(f"Failed to delete tunnel {tunnel_id}: {error}")
        return False

    def _find_tunnels_by_host(self, host: str) -> list[dict]:
        """Find all tunnel routes matching the given host."""
        routes = self._get_all_routes()
        matching = []

        for route in routes:
            route_id = route.get("@id", "")
            if not route_id:
                continue

            for match in route.get("match", []):
                hosts = match.get("host", [])
                if host in hosts:
                    matching.append(route)
                    break

        return matching

    def _delete_tunnels_by_host(self, host: str) -> None:
        """Delete all tunnel routes matching the given host."""
        max_iterations = 50  # Prevent infinite loop
        iteration = 0

        while iteration < max_iterations:
            matching = self._find_tunnels_by_host(host)
            if not matching:
                break

            iteration += 1
            route = matching[0]
            route_id = route.get("@id", "")
            self.logger.info(f"Removing existing tunnel for {host}: {route_id} ({iteration}/{len(matching)} remaining)")
            if not self._delete_tunnel_by_id(route_id):
                self.logger.warning(f"Failed to delete tunnel {route_id}, skipping remaining")
                break

        if iteration >= max_iterations:
            self.logger.warning(f"Reached max iterations ({max_iterations}) cleaning up tunnels for {host}")

    def _check_host_taken_by_other(self, host: str) -> bool:
        """Check if another tunnel (different ID) is using this host."""
        matching = self._find_tunnels_by_host(host)

        for route in matching:
            route_id = route.get("@id", "")
            if route_id and route_id != self.tunnel_id:
                return True

        return False

    def _create_tunnel(self) -> bool:
        """Create tunnel route in Caddy. Replaces existing if same host."""
        # First, try to delete our own tunnel by direct ID (fast path)
        self._delete_tunnel_by_id(self.tunnel_id)

        # Then clean up any remaining tunnels with the same host
        self.logger.debug(f"Checking for existing tunnels with host: {self.host}")
        self._delete_tunnels_by_host(self.host)

        url = f"{self.caddy_api}/config/apps/http/servers/sirtunnel/routes"
        config = self._get_route_config()

        success, error = self._make_request("POST", url, config)
        if success:
            self.logger.info(f"Tunnel created: https://{self.host} -> localhost:{self.port}")
            return True

        self.logger.error(f"Failed to create tunnel: {error}")
        return False

    def _delete_tunnel(self) -> bool:
        """Delete this tunnel route from Caddy."""
        return self._delete_tunnel_by_id(self.tunnel_id)

    def _check_tunnel_health(self) -> bool:
        """Check if tunnel route exists in Caddy."""
        url = f"{self.caddy_api}/id/{self.tunnel_id}"
        success, _ = self._make_request("GET", url, timeout=5)
        return success

    def _get_all_routes(self) -> list:
        """Get all routes from Caddy."""
        url = f"{self.caddy_api}/config/apps/http/servers/sirtunnel/routes"
        success, response = self._make_request("GET", url, timeout=5)
        if success and response:
            try:
                return json.loads(response)
            except json.JSONDecodeError:
                return []
        return []

    def _cleanup_orphan_tunnels(self) -> None:
        """Remove tunnels whose ports are no longer listening."""
        routes = self._get_all_routes()
        if not routes:
            return

        for route in routes:
            route_id = route.get("@id", "")
            if not route_id:
                continue

            # Skip our own tunnel
            if route_id == self.tunnel_id:
                continue

            # Extract port from route
            port = None
            for handle in route.get("handle", []):
                for upstream in handle.get("upstreams", []):
                    dial = upstream.get("dial", "")
                    if dial.startswith(":"):
                        try:
                            port = int(dial[1:])
                        except ValueError:
                            pass
                        break

            if port and not check_port_alive(port):
                self.logger.info(f"Removing orphan tunnel: {route_id} (port {port} is dead)")
                self._delete_tunnel_by_id(route_id)

    def _orphan_check_loop(self) -> None:
        """Background thread for cleaning up orphan tunnels."""
        while self.running:
            time.sleep(ORPHAN_CHECK_INTERVAL)

            if not self.running:
                break

            with self.lock:
                try:
                    self._cleanup_orphan_tunnels()
                except Exception as e:
                    self.logger.debug(f"Orphan cleanup error: {e}")

    def _health_check_loop(self) -> None:
        """Background thread for health monitoring and reconnection."""
        consecutive_failures = 0

        while self.running:
            time.sleep(HEALTH_CHECK_INTERVAL)

            if not self.running:
                break

            with self.lock:
                if self._check_tunnel_health():
                    if not self.connected:
                        self.logger.info("Tunnel connection restored")
                        self.connected = True
                    consecutive_failures = 0
                    self.reconnect_delay = INITIAL_RECONNECT_DELAY
                else:
                    consecutive_failures += 1
                    self.connected = False
                    self.logger.warning(
                        f"Health check failed (attempt {consecutive_failures})"
                    )

                    # Attempt reconnection
                    if self._reconnect():
                        consecutive_failures = 0
                        self.connected = True
                    else:
                        # Exponential backoff
                        self.reconnect_delay = min(
                            self.reconnect_delay * 2, MAX_RECONNECT_DELAY
                        )

    def _reconnect(self) -> bool:
        """Attempt to reconnect the tunnel."""
        self.logger.info(f"Attempting reconnection in {self.reconnect_delay}s...")
        time.sleep(self.reconnect_delay)

        if not self._check_caddy_available():
            self.logger.error("Caddy API not available")
            return False

        # Check if another tunnel has taken over this host
        if self._check_host_taken_by_other(self.host):
            self.logger.info(f"Another tunnel has taken over {self.host}, shutting down...")
            self.running = False
            return False

        return self._create_tunnel_without_delete()

    def _create_tunnel_without_delete(self) -> bool:
        """Create tunnel route without deleting existing (for reconnect)."""
        url = f"{self.caddy_api}/config/apps/http/servers/sirtunnel/routes"
        config = self._get_route_config()

        success, error = self._make_request("POST", url, config)
        if success:
            self.logger.info(f"Tunnel recreated: https://{self.host} -> localhost:{self.port}")
            return True

        self.logger.error(f"Failed to recreate tunnel: {error}")
        return False

    def _cleanup(self) -> None:
        """Cleanup tunnel on exit."""
        if self.connected:
            self.logger.info("Cleaning up tunnel...")
            with self.lock:
                self._delete_tunnel()
                self.connected = False

    def start(self) -> None:
        """Start the tunnel client."""
        self.logger.info(f"Starting tunnel: {self.host}:{self.port}")
        self.logger.info(f"Tunnel ID: {self.tunnel_id}")

        # Check Caddy availability
        if not self._check_caddy_available():
            self.logger.error(
                f"Cannot connect to Caddy API at {self.caddy_api}"
            )
            self.logger.error("Make sure Caddy is running with the admin API enabled")
            sys.exit(1)

        # Create initial tunnel (will replace existing if same ID)
        if not self._create_tunnel():
            self.logger.error("Failed to create initial tunnel")
            sys.exit(1)

        self.running = True
        self.connected = True

        # Start health check thread
        self.health_thread = threading.Thread(
            target=self._health_check_loop, daemon=True
        )
        self.health_thread.start()

        # Start orphan cleanup thread
        self.orphan_thread = threading.Thread(
            target=self._orphan_check_loop, daemon=True
        )
        self.orphan_thread.start()

        self.logger.info("Tunnel is active. Press Ctrl+C to stop.")
        self.logger.info(f"Forwarding: https://{self.host} -> http://localhost:{self.port}")

        # Main loop
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the tunnel client."""
        if not self.running:
            return

        self.running = False
        self._cleanup()
        self.logger.info("Tunnel stopped")


def main():
    """Main entry point."""
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <host> <port>")
        print(f"Example: {sys.argv[0]} tunnel.example.com 8899")
        sys.exit(1)

    host = sys.argv[1]
    port = sys.argv[2]

    client = TunnelClient(host=host, port=port)
    client.start()


if __name__ == "__main__":
    main()
