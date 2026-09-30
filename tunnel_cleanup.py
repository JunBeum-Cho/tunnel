#!/usr/bin/env python3
"""Inspect and safely reap SirTunnel routes; also used by run_server.sh."""

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter

from tunnel_common import (
    DEFAULT_CADDY_API, LOCK_FILE, LockTimeout, RouteError, check_port_alive,
    file_lock, is_tunnel_route, read_routes, route_hosts, route_port, write_routes,
)

DEFAULT_STATE_FILE = os.environ.get("SIRTUNNEL_STATE_FILE", "/var/tmp/sirtunnel-reaper.json")
DEFAULT_STRIKES = 3
DEFAULT_INTERVAL = 30
REAPER_LOCK_FILE = f"{LOCK_FILE}.reaper"


def get_all_routes(caddy_api):
    # Failure is an exception, never an empty list.
    return read_routes(caddy_api)[0]


def load_state(path):
    try:
        with open(path) as handle:
            state = json.load(handle)
        return {
            key: value for key, value in state.items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        } if isinstance(state, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path, state):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".reaper-", dir=parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def state_key(route):
    return json.dumps([route.get("@id"), route.get("group", ""), route_port(route)])


def list_routes(caddy_api):
    routes = get_all_routes(caddy_api)
    counts = Counter(r.get("@id") for r in routes if r.get("@id"))
    if not routes:
        print("No tunnel routes found.")
        return
    for route in routes:
        port = route_port(route)
        flags = []
        if counts[route.get("@id")] > 1:
            flags.append("DUPLICATE")
        if port is not None:
            flags.append("up" if check_port_alive(port) else "DEAD PORT")
        print(f"{route.get('@id', '(unmanaged)')}: {', '.join(route_hosts(route))} -> {port} [{', '.join(flags)}]")
        print(f"  Owner: {route.get('group', '(legacy)')}")


def dedupe(caddy_api, dry_run=False, quiet=False):
    with file_lock():
        routes, etag = read_routes(caddy_api)
        kept, seen = [], set()
        for route in reversed(routes):
            if is_tunnel_route(route):
                route_id = route["@id"]
                if route_id in seen:
                    continue
                seen.add(route_id)
            kept.append(route)
        kept.reverse()
        removed = len(routes) - len(kept)
        if removed and not dry_run:
            write_routes(caddy_api, kept, etag)
        if removed or not quiet:
            print(f"{'[dry-run] would remove' if dry_run else 'Removed'} {removed} duplicate tunnel route(s).")
        return removed


def reap(caddy_api, strikes=DEFAULT_STRIKES, state_file=DEFAULT_STATE_FILE,
         dry_run=False, lease_held=False):
    if strikes < 1:
        raise ValueError("strikes must be positive")
    if not lease_held:
        with file_lock(REAPER_LOCK_FILE, timeout=0):
            return reap(caddy_api, strikes, state_file, dry_run, lease_held=True)
    # Network probes don't hold the global mutation lock. Re-read and compare
    # each route before applying their results so a new owner cannot be reaped.
    routes = get_all_routes(caddy_api)
    probes = {
        state_key(route): (route, check_port_alive(route_port(route)))
        for route in routes if is_tunnel_route(route)
    }
    with file_lock():
        current, etag = read_routes(caddy_api)
        state = load_state(state_file)
        fresh, kept = {}, []
        removed = 0
        for route in current:
            if not is_tunnel_route(route):
                kept.append(route)
                continue
            key = state_key(route)
            probe = probes.get(key)
            if probe is None or probe[0] != route or probe[1]:
                kept.append(route)
                continue
            count = state.get(key, 0) + 1
            if count < strikes:
                fresh[key] = count
                kept.append(route)
                print(f"  {route['@id']}: port {route_port(route)} unreachable ({count}/{strikes})")
                continue
            # Confirm the port is still dead at the point of deletion.
            if check_port_alive(route_port(route)):
                kept.append(route)
                continue
            if dry_run:
                kept.append(route)
                print(f"  [dry-run] would remove {route['@id']} (dead port)")
            else:
                removed += 1
                print(f"Removing orphan tunnel: {route['@id']} (dead port)")
        if removed:
            write_routes(caddy_api, kept, etag)
        if not dry_run:
            # Also clears stale strikes when there are zero routes. A failed
            # GET/PATCH never reaches this point and preserves prior state.
            save_state(state_file, fresh)
        return removed


def cleanup_all(caddy_api, force=False):
    if not force and input("Remove all SirTunnel routes? [y/N]: ").lower() != "y":
        print("Aborted.")
        return
    with file_lock():
        routes, etag = read_routes(caddy_api)
        kept = [route for route in routes if not is_tunnel_route(route)]
        removed = len(routes) - len(kept)
        if removed:
            write_routes(caddy_api, kept, etag)
        print(f"Removed {removed} tunnel route(s).")


def cleanup_by_id(caddy_api, route_id):
    with file_lock():
        routes, etag = read_routes(caddy_api)
        kept = [route for route in routes if not (
            route.get("@id") == route_id and is_tunnel_route(route)
        )]
        if len(kept) != len(routes):
            write_routes(caddy_api, kept, etag)
        print(f"Removed {len(routes) - len(kept)} route(s) with ID {route_id}.")


def run_reaper(args):
    # Held for the watcher's lifetime, including sleeps, not just each pass.
    with file_lock(REAPER_LOCK_FILE, timeout=0):
        while True:
            try:
                if not args.no_dedupe:
                    dedupe(args.caddy_api, args.dry_run, quiet=True)
                reap(args.caddy_api, args.strikes, args.state_file, args.dry_run, lease_held=True)
            except (RouteError, OSError) as exc:
                if not args.watch:
                    raise
                print(f"Reaper pass deferred: {exc}", file=sys.stderr)
            if not args.watch:
                return
            sys.stdout.flush()
            time.sleep(args.watch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy-api", default=DEFAULT_CADDY_API)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    reap_parser = commands.add_parser("reap")
    reap_parser.add_argument("--strikes", type=int, default=DEFAULT_STRIKES)
    reap_parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    reap_parser.add_argument("--watch", type=int)
    reap_parser.add_argument("--no-dedupe", action="store_true")
    reap_parser.add_argument("-n", "--dry-run", action="store_true")
    dedupe_parser = commands.add_parser("dedupe")
    dedupe_parser.add_argument("-n", "--dry-run", action="store_true")
    cleanup_parser = commands.add_parser("cleanup")
    cleanup_parser.add_argument("-f", "--force", action="store_true")
    delete_parser = commands.add_parser("delete")
    delete_parser.add_argument("route_id")
    args = parser.parse_args()
    if args.command == "reap" and (
        args.strikes < 1 or (args.watch is not None and args.watch < 1)
    ):
        parser.error("--strikes and --watch must be positive")
    try:
        if args.command == "list":
            list_routes(args.caddy_api)
        elif args.command == "dedupe":
            dedupe(args.caddy_api, args.dry_run)
        elif args.command == "reap":
            run_reaper(args)
        elif args.command == "cleanup":
            cleanup_all(args.caddy_api, args.force)
        else:
            cleanup_by_id(args.caddy_api, args.route_id)
        return 0
    except LockTimeout as exc:
        print(f"Another route writer/reaper is busy: {exc}", file=sys.stderr)
        return 1
    except (RouteError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
