#!/usr/bin/env python3
"""
Tunnel Cleanup Utility
Clean up orphaned tunnel routes from Caddy.
"""

import json
import sys
import argparse
from urllib import request
from urllib.error import URLError, HTTPError


DEFAULT_CADDY_API = "http://127.0.0.1:2019"


def get_all_routes(caddy_api: str) -> list:
    """Get all routes from Caddy."""
    url = f"{caddy_api}/config/apps/http/servers/sirtunnel/routes"
    try:
        req = request.Request(method="GET", url=url)
        with request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 404:
            return []
        raise
    except Exception as e:
        print(f"Error fetching routes: {e}")
        return []


def delete_route(caddy_api: str, route_id: str) -> bool:
    """Delete a route by ID."""
    url = f"{caddy_api}/id/{route_id}"
    try:
        req = request.Request(method="DELETE", url=url)
        request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"Error deleting route {route_id}: {e}")
        return False


def list_routes(caddy_api: str) -> None:
    """List all tunnel routes."""
    routes = get_all_routes(caddy_api)

    if not routes:
        print("No tunnel routes found.")
        return

    print(f"Found {len(routes)} tunnel route(s):\n")
    for i, route in enumerate(routes, 1):
        route_id = route.get("@id", "unknown")
        hosts = []
        for match in route.get("match", []):
            hosts.extend(match.get("host", []))

        ports = []
        for handle in route.get("handle", []):
            for upstream in handle.get("upstreams", []):
                dial = upstream.get("dial", "")
                if dial.startswith(":"):
                    ports.append(dial[1:])

        host_str = ", ".join(hosts) if hosts else "N/A"
        port_str = ", ".join(ports) if ports else "N/A"

        print(f"  {i}. ID: {route_id}")
        print(f"     Host: {host_str}")
        print(f"     Port: {port_str}")
        print()


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

    # List command
    subparsers.add_parser("list", help="List all tunnel routes")

    # Cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Remove all tunnel routes")
    cleanup_parser.add_argument(
        "-f", "--force", action="store_true", help="Skip confirmation"
    )

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a specific route")
    delete_parser.add_argument("route_id", help="Route ID to delete")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        # Test connection to Caddy
        test_url = f"{args.caddy_api}/config/"
        req = request.Request(method="GET", url=test_url)
        request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Cannot connect to Caddy API at {args.caddy_api}")
        print(f"Error: {e}")
        sys.exit(1)

    if args.command == "list":
        list_routes(args.caddy_api)
    elif args.command == "cleanup":
        cleanup_all(args.caddy_api, args.force)
    elif args.command == "delete":
        cleanup_by_id(args.caddy_api, args.route_id)


if __name__ == "__main__":
    main()
