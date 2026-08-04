#!/usr/bin/env python3
"""
Tunnel Cleanup Utility
Inspect and reap tunnel routes in Caddy.

`reap` is the server-side replacement for the per-client orphan sweep that
used to run inside tunnel.py. Running it once per server instead of once per
tunnel means a route is probed by a single process on a single timer, and a
route is only removed after it has failed several consecutive probes -- so a
momentarily slow probe can no longer delete a healthy tunnel.

Kept deliberately standalone (no import from tunnel.py) so it can be dropped
onto a server on its own.
"""

import os
import sys
import json
import time
import socket
import argparse
from collections import Counter
from typing import NamedTuple, Optional
from contextlib import suppress
from urllib import request
from urllib.error import URLError, HTTPError


DEFAULT_CADDY_API = "http://127.0.0.1:2019"
SERVER_NAME = "sirtunnel"
ROUTES_PATH = f"/config/apps/http/servers/{SERVER_NAME}/routes"
DEFAULT_STATE_FILE = os.environ.get(
    "SIRTUNNEL_STATE_FILE", "/var/tmp/sirtunnel-reaper.json"
)
DEFAULT_STRIKES = 3
DEFAULT_INTERVAL = 30  # seconds
PORT_CHECK_TIMEOUT = 2  # seconds
REQUEST_TIMEOUT = 10  # seconds


class ApiResult(NamedTuple):
    """Outcome of a Caddy admin API call.

    `status` is None when no response arrived at all. "Caddy says it is gone"
    and "Caddy did not answer" must stay distinguishable: only the first one
    means there is nothing left to delete.
    """

    ok: bool
    body: str = ""
    status: Optional[int] = None

    @property
    def missing(self) -> bool:
        """Caddy reports an unknown @id as `500 unknown object ID`, not 404."""
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


def api_request(
    method: str,
    url: str,
    data: Optional[object] = None,
    timeout: int = REQUEST_TIMEOUT,
) -> ApiResult:
    """Make a request against the Caddy admin API."""
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
        return ApiResult(False, error_body, e.code)
    except URLError as e:
        return ApiResult(False, f"Connection failed: {e.reason}")
    except Exception as e:
        return ApiResult(False, str(e))


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


def route_hosts(route: dict) -> list:
    hosts = []
    for match in route.get("match", []):
        hosts.extend(match.get("host", []))
    return hosts


def get_all_routes(caddy_api: str) -> list:
    """Get all routes from Caddy."""
    res = api_request("GET", f"{caddy_api}{ROUTES_PATH}")
    if res.missing:
        return []
    if not res.ok:
        print(f"Error fetching routes: {res.error}")
        return []
    try:
        return json.loads(res.body) or []
    except json.JSONDecodeError:
        print("Error fetching routes: malformed JSON")
        return []


def delete_route(caddy_api: str, route_id: str) -> bool:
    """Delete a route by ID. A route that is already gone counts as success."""
    res = api_request("DELETE", f"{caddy_api}/id/{route_id}")
    if res.ok or res.missing:
        return True
    print(f"Error deleting route {route_id}: {res.error}")
    return False


# --------------------------------------------------------------------------
# reaper state
# --------------------------------------------------------------------------


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        print(f"Warning: could not read state file {path}: {e}")
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"Warning: could not write state file {path}: {e}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def list_routes(caddy_api: str) -> None:
    """List all tunnel routes."""
    routes = get_all_routes(caddy_api)

    if not routes:
        print("No tunnel routes found.")
        return

    duplicates = {
        rid for rid, n in Counter(r.get("@id") for r in routes).items() if rid and n > 1
    }

    print(f"Found {len(routes)} tunnel route(s):\n")
    for i, route in enumerate(routes, 1):
        route_id = route.get("@id", "unknown")
        port = route_port(route)
        hosts = route_hosts(route)

        flags = []
        if route_id in duplicates:
            flags.append("DUPLICATE")
        if port is not None:
            flags.append("up" if check_port_alive(port) else "DEAD PORT")

        host_str = ", ".join(hosts) if hosts else "N/A"
        port_str = str(port) if port is not None else "N/A"

        print(f"  {i}. ID: {route_id}" + (f"  [{', '.join(flags)}]" if flags else ""))
        print(f"     Host: {host_str}")
        print(f"     Port: {port_str}")
        if route.get("group"):
            print(f"     Owner: {route['group']}")
        print()

    if duplicates:
        print(f"{len(duplicates)} duplicated ID(s). Run `dedupe` to collapse them.")


def dedupe(caddy_api: str, dry_run: bool = False, quiet: bool = False) -> int:
    """Collapse routes that share an @id, keeping the most recent one.

    The array is rewritten in one PATCH rather than deleted entry by entry:
    `DELETE /id/<id>` always resolves to the *last* occurrence, so deleting
    repeatedly would keep the oldest copy -- which is the stale one that
    shadows the live route and serves the 502s.
    """
    routes = get_all_routes(caddy_api)
    counts = Counter(r.get("@id") for r in routes if r.get("@id"))
    duplicated = {rid for rid, n in counts.items() if n > 1}

    if not duplicated:
        if not quiet:
            print("No duplicated route IDs.")
        return 0

    kept, seen = [], set()
    for route in reversed(routes):
        route_id = route.get("@id")
        if route_id:
            if route_id in seen:
                continue
            seen.add(route_id)
        kept.append(route)
    kept.reverse()

    removed = len(routes) - len(kept)
    for route_id in sorted(duplicated):
        print(f"  {route_id}: {counts[route_id]} copies -> 1")

    if dry_run:
        print(f"[dry-run] would remove {removed} duplicate route(s).")
        return removed

    res = api_request("PATCH", f"{caddy_api}{ROUTES_PATH}", kept)
    if not res.ok:
        print(f"Failed to rewrite routes: {res.error}")
        return 0

    print(f"Removed {removed} duplicate route(s).")
    return removed


def reap(
    caddy_api: str,
    strikes: int = DEFAULT_STRIKES,
    state_file: str = DEFAULT_STATE_FILE,
    dry_run: bool = False,
) -> int:
    """Remove routes whose upstream port has been unreachable `strikes` times.

    Requiring consecutive failures is the point: a single 2s TCP probe missing
    its window is normal under load, and acting on one used to delete healthy
    tunnels.
    """
    routes = get_all_routes(caddy_api)
    if not routes:
        return 0

    state = load_state(state_file)
    fresh: dict = {}
    removed = 0

    for route in routes:
        route_id = route.get("@id")
        port = route_port(route)
        if not route_id or port is None:
            continue

        key = f"{route_id}:{port}"

        if check_port_alive(port):
            continue

        count = state.get(key, 0) + 1
        if count < strikes:
            fresh[key] = count
            print(f"  {route_id}: port {port} unreachable ({count}/{strikes})")
            continue

        if dry_run:
            print(f"  [dry-run] would remove {route_id} (port {port} dead)")
            fresh[key] = count
            continue

        print(f"Removing orphan tunnel: {route_id} (port {port} is dead)")
        if delete_route(caddy_api, route_id):
            removed += 1
        else:
            # Keep the strike count so the next pass retries immediately.
            fresh[key] = count

    save_state(state_file, fresh)
    return removed


def cleanup_all(caddy_api: str, force: bool = False) -> None:
    """Remove all tunnel routes."""
    routes = get_all_routes(caddy_api)

    if not routes:
        print("No tunnel routes to clean up.")
        return

    print(f"Found {len(routes)} tunnel route(s) to remove.")

    if not force:
        confirm = input("Are you sure you want to remove all routes? [y/N]: ")
        if confirm.lower() != "y":
            print("Aborted.")
            return

    success = 0
    failed = 0

    for route in routes:
        route_id = route.get("@id")
        if not route_id:
            continue

        if delete_route(caddy_api, route_id):
            print(f"  Deleted: {route_id}")
            success += 1
        else:
            print(f"  Failed: {route_id}")
            failed += 1

    print(f"\nCleanup complete. Deleted: {success}, Failed: {failed}")


def cleanup_by_id(caddy_api: str, route_id: str) -> None:
    """Remove a specific tunnel route by ID."""
    if delete_route(caddy_api, route_id):
        print(f"Successfully deleted route: {route_id}")
    else:
        print(f"Failed to delete route: {route_id}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Tunnel Cleanup Utility - Manage orphaned tunnel routes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s list                    # List all tunnel routes
  %(prog)s reap                    # One reaper pass (for cron)
  %(prog)s reap --watch 30         # Run continuously (for systemd)
  %(prog)s dedupe                  # Collapse routes sharing an @id
  %(prog)s cleanup                 # Remove all routes (with confirmation)
  %(prog)s cleanup --force         # Remove all routes without confirmation
  %(prog)s delete <route-id>       # Delete a specific route
        """,
    )

    parser.add_argument(
        "--caddy-api",
        default=DEFAULT_CADDY_API,
        help=f"Caddy admin API URL (default: {DEFAULT_CADDY_API})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("list", help="List all tunnel routes")

    reap_parser = subparsers.add_parser(
        "reap", help="Remove routes whose port stays unreachable"
    )
    reap_parser.add_argument(
        "--strikes",
        type=int,
        default=DEFAULT_STRIKES,
        help=f"Consecutive failed probes before removal (default: {DEFAULT_STRIKES})",
    )
    reap_parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"Where strike counts are kept (default: {DEFAULT_STATE_FILE})",
    )
    reap_parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Keep running, one pass every SECONDS",
    )
    reap_parser.add_argument(
        "--no-dedupe", action="store_true", help="Skip the duplicate-ID sweep"
    )
    reap_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Report without deleting"
    )

    dedupe_parser = subparsers.add_parser(
        "dedupe", help="Collapse routes that share an @id"
    )
    dedupe_parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Report without rewriting"
    )

    cleanup_parser = subparsers.add_parser("cleanup", help="Remove all tunnel routes")
    cleanup_parser.add_argument(
        "-f", "--force", action="store_true", help="Skip confirmation"
    )

    delete_parser = subparsers.add_parser("delete", help="Delete a specific route")
    delete_parser.add_argument("route_id", help="Route ID to delete")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Cheap reachability probe. Deliberately not `GET /config/`, which
    # serialises the entire config under a lock on every call.
    probe = api_request(
        "GET", f"{args.caddy_api}/config/apps/http/servers/{SERVER_NAME}/listen", timeout=5
    )
    if not probe.ok:
        print(f"Cannot connect to Caddy API at {args.caddy_api}")
        print(f"Error: {probe.error}")
        sys.exit(1)

    if args.command == "list":
        list_routes(args.caddy_api)
    elif args.command == "dedupe":
        dedupe(args.caddy_api, args.dry_run)
    elif args.command == "reap":
        run_reaper(args)
    elif args.command == "cleanup":
        cleanup_all(args.caddy_api, args.force)
    elif args.command == "delete":
        cleanup_by_id(args.caddy_api, args.route_id)


def run_reaper(args) -> None:
    def one_pass() -> None:
        if not args.no_dedupe:
            dedupe(args.caddy_api, args.dry_run, quiet=True)
        reap(args.caddy_api, args.strikes, args.state_file, args.dry_run)

    if not args.watch:
        one_pass()
        return

    print(f"Reaping every {args.watch}s (strikes={args.strikes})")
    try:
        while True:
            one_pass()
            sys.stdout.flush()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
