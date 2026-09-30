"""Regression tests for races and failures that can take down live tunnels."""

import atexit
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from _support import TEST_DIR as _TEST_DIR

import tunnel
import tunnel_cleanup as cleanup
from tunnel_common import (
    LOCK_FILE, ROUTES_PATH, LockTimeout, RouteError, file_lock, normalize_host,
    read_routes, write_routes,
)


def route(host="one.example.com", port=9001, owner="sirtunnel-old"):
    return {
        "@id": f"{host}-{port}", "group": owner,
        "match": [{"host": [host]}],
        "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f":{port}"}]}],
    }


class AdminAPI:
    def __init__(self):
        self.routes = []
        self.version = 0
        self.writes = 0
        self.read_error = None
        self.mutate_before_write = None
        self.reject_stream_close_delay = False
        self.stream_rejections = 0
        self.mutex = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, etag=False):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                if etag:
                    self.send_header("Etag", f'"{ROUTES_PATH} {api.version}"')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with api.mutex:
                    if self.path == ROUTES_PATH:
                        if api.read_error:
                            self.reply(*api.read_error)
                        else:
                            self.reply(200, api.routes, etag=True)
                    elif self.path.startswith("/id/"):
                        found = [r for r in api.routes if r.get("@id") == self.path[4:]]
                        self.reply(200, found[-1]) if found else self.reply(500, "unknown object ID")
                    else:
                        self.reply(200, [":443"])

            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with api.mutex:
                    if api.mutate_before_write:
                        api.routes.append(api.mutate_before_write)
                        api.mutate_before_write = None
                        api.version += 1
                    if self.headers.get("If-Match") != f'"{ROUTES_PATH} {api.version}"':
                        self.reply(412, "configuration changed")
                        return
                    if api.reject_stream_close_delay and any(
                        "stream_close_delay" in handle
                        for entry in body for handle in entry.get("handle", [])
                    ):
                        api.stream_rejections += 1
                        self.reply(400, {"error": 'json: unknown field "stream_close_delay"'})
                        return
                    api.routes = body
                    api.version += 1
                    api.writes += 1
                    self.reply(200, "")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class TunnelFailures(unittest.TestCase):
    def setUp(self):
        self.api = AdminAPI()
        self.temp = tempfile.TemporaryDirectory(dir=_TEST_DIR.name)
        self.state_file = str(Path(self.temp.name) / "reaper.json")
        self.live = {9001, 9002}
        self.port_patch = patch("tunnel.check_port_alive", side_effect=lambda port: port in self.live)
        self.port_patch.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client._registered = False
            atexit.unregister(client._cleanup)
        self.port_patch.stop()
        self.api.close()
        self.temp.cleanup()

    def client(self, host="one.example.com", port=9001):
        with patch("tunnel.signal.signal"), patch("tunnel.atexit.register"):
            client = tunnel.TunnelClient(host, port, self.api.url)
        client._owns_port_lease = True
        self.clients.append(client)
        return client

    def test_parallel_registration_keeps_all_services(self):
        clients = [self.client(f"service{i}.example.com", 9001 + i) for i in range(8)]
        self.live.update(range(9001, 9009))
        errors = []
        def register(client):
            try:
                client._claim_host()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=register, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual({r["@id"] for r in self.api.routes}, {c.tunnel_id for c in clients})

    def test_repeated_claim_does_not_reload_or_reorder_routes(self):
        client = self.client()
        client._claim_host()
        self.api.routes.append(route("two.example.com", 9002))
        original = deepcopy(self.api.routes)
        client._claim_host()
        self.assertEqual(self.api.writes, 1)
        self.assertEqual(self.api.routes, original)

    def test_legacy_caddy_keeps_registration_and_other_routes(self):
        self.api.reject_stream_close_delay = True
        self.api.routes = [route("two.example.com", 9002)]
        original = deepcopy(self.api.routes)
        client = self.client()
        self.assertTrue(client._claim_host())
        self.assertEqual(self.api.stream_rejections, 1)
        self.assertEqual(self.api.writes, 1)
        self.assertEqual(self.api.routes, original + [client._get_route_config()])
        self.assertNotIn("stream_close_delay", self.api.routes[-1]["handle"][0])
        client._claim_host()
        self.assertEqual(self.api.stream_rejections, 1)
        self.assertEqual(self.api.writes, 1)

    def test_remote_signals_exit_with_failure_for_autossh(self):
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                client = self.client()
                client.running = True
                client._signal_handler(signum, None)
                self.assertFalse(client.running)
                self.assertTrue(client._stop_event.is_set())
                self.assertEqual(client.exit_code, 128 + signum)

    def test_live_hostname_conflict_preserves_existing_service(self):
        self.api.routes = [route()]
        original = deepcopy(self.api.routes)
        with self.assertRaises(tunnel.HostConflict):
            self.client(port=9002)._claim_host()
        self.assertEqual(self.api.routes, original)
        self.assertEqual(self.api.writes, 0)

    def test_dead_hostname_and_reused_port_are_replaced_atomically(self):
        self.live.remove(9002)
        self.api.routes = [route(port=9002), route("previous.example.com"), {"handle": []}]
        client = self.client()
        client._claim_host()
        self.assertEqual(self.api.routes, [{"handle": []}, client._get_route_config()])
        self.assertEqual(self.api.writes, 1)

    def test_api_failures_cannot_create_duplicate_routes(self):
        self.api.routes = [route()]
        for error in [(500, "internal error"), (200, "not JSON"), (200, {"wrong": "schema"})]:
            with self.subTest(error=error):
                self.api.read_error = error
                with self.assertRaises(RouteError):
                    self.client()._claim_host()
                self.assertEqual(self.api.writes, 0)
                self.assertEqual(len(self.api.routes), 1)

    def test_external_config_change_is_protected_by_etag(self):
        self.api.mutate_before_write = route("external.example.com", 9050)
        with self.assertRaises(RouteError):
            self.client()._claim_host()
        self.assertEqual([r["@id"] for r in self.api.routes], ["external.example.com-9050"])

    def test_old_cleanup_cannot_remove_new_owner(self):
        old = self.client()
        old._claim_host()
        new = self.client()
        new._claim_host()
        old._cleanup()
        self.assertEqual(self.api.routes, [new._get_route_config()])
        self.assertTrue(old._cleaned_up)

    def test_cleanup_failure_remains_retryable(self):
        client = self.client()
        client._claim_host()
        with patch("tunnel.file_lock", side_effect=LockTimeout("busy")):
            client._cleanup()
        self.assertFalse(client._cleaned_up)
        self.assertEqual(len(self.api.routes), 1)
        client._cleanup()
        self.assertTrue(client._cleaned_up)
        self.assertEqual(self.api.routes, [])

    def test_health_rejects_wrong_host_or_port_and_malformed_json(self):
        client = self.client()
        good = client._get_route_config()
        self.api.routes = [good]
        self.assertEqual(client._check_tunnel_health(), tunnel.HealthStatus.OK)
        changed = deepcopy(good)
        changed["handle"][0]["upstreams"][0]["dial"] = ":9999"
        self.api.routes = [changed]
        self.assertEqual(client._check_tunnel_health(), tunnel.HealthStatus.MISSING)
        changed = deepcopy(good)
        changed["match"][0]["host"] = ["wrong.example.com"]
        self.api.routes = [changed]
        self.assertEqual(client._check_tunnel_health(), tunnel.HealthStatus.MISSING)
        with patch.object(client, "_make_request", return_value=tunnel.ApiResult(True, "oops", 200)):
            self.assertEqual(client._check_tunnel_health(), tunnel.HealthStatus.UNKNOWN)

    def test_dead_ssh_listener_exits_even_with_existing_route(self):
        client = self.client()
        client._claim_host()
        self.live.clear()
        client.running = True
        with patch("tunnel.HEALTH_CHECK_INTERVAL", 0.001), patch("tunnel.HEALTH_CHECK_JITTER", 0):
            client._health_check_loop()
        self.assertFalse(client.running)
        self.assertTrue(client._stop_event.is_set())
        self.assertEqual(client.exit_code, 1)  # autossh must treat this as failure

    def test_reaper_requires_strikes_and_dry_run_never_changes_state(self):
        self.api.routes = [route()]
        with patch("tunnel_cleanup.check_port_alive", return_value=False):
            self.assertEqual(cleanup.reap(self.api.url, state_file=self.state_file), 0)
            before = Path(self.state_file).read_bytes()
            for _ in range(4):
                cleanup.reap(self.api.url, state_file=self.state_file, dry_run=True)
            self.assertEqual(Path(self.state_file).read_bytes(), before)
            self.assertEqual(cleanup.reap(self.api.url, state_file=self.state_file), 0)
            self.assertEqual(cleanup.reap(self.api.url, state_file=self.state_file), 1)
        self.assertEqual(self.api.routes, [])

    def test_new_owner_cannot_inherit_old_strikes(self):
        self.api.routes = [route()]
        cleanup.save_state(self.state_file, {cleanup.state_key(self.api.routes[0]): 2})
        self.api.routes = [route(owner="sirtunnel-new")]
        with patch("tunnel_cleanup.check_port_alive", return_value=False):
            self.assertEqual(cleanup.reap(self.api.url, state_file=self.state_file), 0)
        self.assertEqual(list(cleanup.load_state(self.state_file).values()), [1])

    def test_route_changed_during_probe_cannot_be_reaped(self):
        old = route()
        self.api.routes = [old]
        cleanup.save_state(self.state_file, {cleanup.state_key(old): 2})
        new = route(owner="sirtunnel-new")
        def probe(port):
            with self.api.mutex:
                self.api.routes = [new]
                self.api.version += 1
            return False
        with patch("tunnel_cleanup.check_port_alive", side_effect=probe):
            self.assertEqual(cleanup.reap(self.api.url, state_file=self.state_file), 0)
        self.assertEqual(self.api.routes, [new])

    def test_empty_routes_clear_state_but_api_failure_preserves_it(self):
        cleanup.save_state(self.state_file, {"old": 2})
        self.api.read_error = (500, "unavailable")
        with self.assertRaises(RouteError):
            cleanup.reap(self.api.url, state_file=self.state_file)
        self.assertEqual(cleanup.load_state(self.state_file), {"old": 2})
        self.api.read_error = None
        cleanup.reap(self.api.url, state_file=self.state_file)
        self.assertEqual(cleanup.load_state(self.state_file), {})

    def test_only_one_reaper_and_port_owner_can_run(self):
        with file_lock(cleanup.REAPER_LOCK_FILE, timeout=0):
            with self.assertRaises(LockTimeout):
                cleanup.reap(self.api.url, state_file=self.state_file)
        with file_lock(f"{LOCK_FILE}.port-9001", timeout=0):
            with self.assertRaises(LockTimeout):
                with file_lock(f"{LOCK_FILE}.port-9001", timeout=0):
                    self.fail("Duplicate port owner acquired the lease")

    def test_dedupe_retains_current_owner_and_unmanaged_routes(self):
        unmanaged = {"@id": "custom", "handle": []}
        self.api.routes = [route(), unmanaged, route(owner="sirtunnel-new"), unmanaged]
        self.assertEqual(cleanup.dedupe(self.api.url), 1)
        self.assertEqual(self.api.routes, [unmanaged, route(owner="sirtunnel-new"), unmanaged])

    def test_host_normalization_and_invalid_labels(self):
        self.assertEqual(normalize_host("Service.Example.COM."), "service.example.com")
        for host in ("a..com", "-a.com", "a-.com", "a/b", "a_foo.com", "a" * 64 + ".com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                normalize_host(host)


if __name__ == "__main__":
    unittest.main()
