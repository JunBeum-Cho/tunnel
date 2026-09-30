"""Opt-in tests with a real Caddy binary on isolated, unprivileged ports."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from _support import TEST_DIR
from server import Settings, control, positive_env
from tunnel import TunnelClient
from tunnel_common import api_request

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("SIRTUNNEL_TEST_CADDY_BIN")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SettingsValidation(unittest.TestCase):
    def test_nonfinite_intervals_cannot_disable_or_crash_the_watchdog(self):
        for value in ("nan", "inf", "-inf", "0", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {"SIRTUNNEL_HEALTH_INTERVAL": value}):
                with self.assertRaises(ValueError):
                    positive_env("SIRTUNNEL_HEALTH_INTERVAL", 10)


@unittest.skipUnless(BINARY, "Set SIRTUNNEL_TEST_CADDY_BIN to run real Caddy recovery tests")
class ServerRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sirtunnel-server-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.admin, self.https, self.http = free_port(), free_port(), free_port()
        certificate = self.directory / "cert.pem"
        key = self.directory / "key.pem"
        openssl_config = self.directory / "openssl.cnf"
        openssl_config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\n[dn]\nCN=tunnel.test\n[ext]\nsubjectAltName=DNS:tunnel.test\n")
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=tunnel.test", "-config", str(openssl_config), "-keyout", str(key), "-out", str(certificate),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.config = self.directory / "caddy.json"
        self.config.write_text(json.dumps({
            "admin": {"listen": f"127.0.0.1:{self.admin}"},
            "apps": {
                "http": {
                    "http_port": self.http, "https_port": self.https,
                    "servers": {"sirtunnel": {
                        "listen": [f"127.0.0.1:{self.https}"], "routes": [],
                        "automatic_https": {"disable_certificates": True},
                    }},
                },
                "tls": {"certificates": {"load_files": [{"certificate": str(certificate), "key": str(key)}]}},
            },
        }))
        self.env = os.environ.copy()
        self.env.update({
            "SIRTUNNEL_CADDY_BIN": str(Path(BINARY).resolve()),
            "SIRTUNNEL_CADDY_CONFIG": str(self.config),
            "SIRTUNNEL_RUNTIME_DIR": str(self.directory / "runtime"),
            "SIRTUNNEL_CADDY_API": f"http://127.0.0.1:{self.admin}",
            "SIRTUNNEL_HEALTH_INTERVAL": "0.2", "SIRTUNNEL_HEALTH_FAILURES": "3",
            "SIRTUNNEL_REAPER_INTERVAL": "0.2", "SIRTUNNEL_START_TIMEOUT": "15",
            "SIRTUNNEL_LOCK_FILE": str(self.directory / "routes.lock"),
            "XDG_DATA_HOME": str(self.directory / "data"),
        })
        with patch.dict(os.environ, self.env):
            self.settings = Settings()
        self.addCleanup(self.stop)
        result = self.run_server()
        self.assertEqual(result.returncode, 0, self.logs() + result.stderr)

    def logs(self):
        path = self.directory / "runtime" / "server.log"
        return path.read_text() if path.exists() else "No log yet."

    def run_server(self, *args):
        return subprocess.run(
            [str(ROOT / "run_server.sh"), *args], env=self.env, cwd=ROOT,
            capture_output=True, text=True, timeout=55,
        )

    def stop(self):
        self.run_server("stop")

    def wait_ready(self, old_pid, timeout=50):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = control(self.settings, "status")
            if result and result["ready"] and result["caddy_pid"] != old_pid:
                return result
            time.sleep(0.1)
        self.fail("Caddy did not recover:\n" + self.logs())

    def register(self, host, port):
        with patch("tunnel.signal.signal"), patch("tunnel.atexit.register"):
            client = TunnelClient(host, port, self.settings.api)
        client._owns_port_lease = True
        with patch("tunnel.file_lock", side_effect=lambda *a, **kw: __import__("tunnel_common").file_lock(self.env["SIRTUNNEL_LOCK_FILE"], **kw)):
            client._claim_host()
        return client

    def tls_socket(self):
        sock = socket.create_connection(("127.0.0.1", self.https), timeout=3)
        return ssl._create_unverified_context().wrap_socket(sock, server_hostname="tunnel.test")

    def test_websocket_survives_another_tunnel_registration(self):
        class WebSocketApp(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                try:
                    while True:
                        header = self.rfile.read(2)
                        if len(header) != 2 or header[0] != 0x81:
                            return
                        length = header[1] & 0x7f
                        mask = self.rfile.read(4)
                        data = self.rfile.read(length)
                        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
                        self.wfile.write(bytes([0x81, len(payload)]) + payload)
                        self.wfile.flush()
                except OSError:
                    pass

        app = ThreadingHTTPServer(("127.0.0.1", 0), WebSocketApp)
        threading.Thread(target=app.serve_forever, daemon=True).start()
        self.addCleanup(app.server_close)
        self.addCleanup(app.shutdown)
        self.register("tunnel.test", app.server_port)
        with self.tls_socket() as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: tunnel.test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                chunk = sock.recv(1)
                self.assertTrue(chunk, "WebSocket handshake ended early")
                headers += chunk
            self.assertIn(b" 101 ", headers)

            def echo():
                sock.sendall(b"\x81\x84\x00\x00\x00\x00ping")
                response = b""
                while len(response) < 6:
                    chunk = sock.recv(6 - len(response))
                    if not chunk:
                        break
                    response += chunk
                self.assertEqual(response, b"\x81\x04ping", "An unrelated route update closed the WebSocket")

            echo()
            # A second listening port represents a separate SSH forward.
            with socket.socket() as other:
                other.bind(("127.0.0.1", 0))
                other.listen()
                second = self.register("other.test", other.getsockname()[1])
                echo()
                with patch("tunnel.file_lock", side_effect=lambda *a, **kw: __import__("tunnel_common").file_lock(self.env["SIRTUNNEL_LOCK_FILE"], **kw)):
                    second._cleanup()
                echo()

    def test_admin_failure_does_not_restart_a_serving_caddy(self):
        # A proxy injects an admin-only failure while HTTPS remains available.
        actual_api = self.settings.api
        failed = threading.Event()

        class AdminProxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def forward(self):
                if failed.is_set() and self.path.endswith("/sirtunnel/listen"):
                    result = (500, b"admin health endpoint unavailable", None)
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    data = json.loads(self.rfile.read(length)) if length else None
                    response = api_request(self.command, actual_api + self.path, data, timeout=2, etag=self.headers.get("If-Match"))
                    if response.status is None:
                        self.close_connection = True
                        return
                    result = (response.status, response.body.encode(), response.etag)
                status, body, etag = result
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if etag:
                    self.send_header("Etag", etag)
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_PATCH = forward

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), AdminProxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        with socket.socket() as upstream:
            upstream.bind(("127.0.0.1", 0))
            upstream.listen()
            self.register("tunnel.test", upstream.getsockname()[1])
            self.assertEqual(self.run_server("stop").returncode, 0)
            self.env["SIRTUNNEL_CADDY_API"] = f"http://127.0.0.1:{proxy.server_port}"
            with patch.dict(os.environ, self.env):
                self.settings = Settings()
            self.assertEqual(self.run_server().returncode, 0, self.logs())
            original = control(self.settings, "status")
            failed.set()
            deadline = time.monotonic() + 8
            while "health check failed (3/3)" not in self.logs() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertIn("health check failed (3/3)", self.logs())
            time.sleep(0.5)
            status = control(self.settings, "status")
            self.assertEqual(status["caddy_pid"], original["caddy_pid"], self.logs())
            self.assertEqual(status["restarts"], 0, self.logs())
            # An unmatched Host gets Caddy's own response, without asking the app.
            with self.tls_socket() as sock:
                sock.sendall(b"HEAD / HTTP/1.1\r\nHost: health.sirtunnel.invalid\r\nConnection: close\r\n\r\n")
                self.assertTrue(sock.recv(512).startswith(b"HTTP/1.1 "))
            failed.clear()
            self.wait_ready(None, timeout=5)

    def test_detached_single_instance_crash_resume_redirect_and_shutdown(self):
        status = control(self.settings, "status")
        # Launcher already exited; detached supervisor remains responsive.
        self.assertTrue(status["ready"])
        duplicate = self.run_server()
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(control(self.settings, "status")["supervisor_pid"], status["supervisor_pid"])
        os.kill(status["supervisor_pid"], signal.SIGHUP)
        time.sleep(0.2)
        self.assertTrue(control(self.settings, "status")["ready"])

        class App(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"alive\n")
        app = ThreadingHTTPServer(("127.0.0.1", 0), App)
        app_thread = threading.Thread(target=app.serve_forever, daemon=True)
        app_thread.start()
        self.addCleanup(app.server_close)
        self.addCleanup(app.shutdown)
        with patch("tunnel.signal.signal"), patch("tunnel.atexit.register"):
            client = TunnelClient("tunnel.test", app.server_port, self.settings.api)
        client._owns_port_lease = True
        # The test process writes with its own isolated lease/route lock.
        with patch("tunnel.file_lock", side_effect=lambda *a, **kw: __import__("tunnel_common").file_lock(self.env["SIRTUNNEL_LOCK_FILE"], **kw)):
            client._claim_host()
        def request(secure):
            if secure:
                conn = http.client.HTTPConnection("127.0.0.1", self.https, timeout=5)
                conn.connect()
                conn.sock = ssl._create_unverified_context().wrap_socket(conn.sock, server_hostname="tunnel.test")
            else:
                conn = http.client.HTTPConnection("127.0.0.1", self.http, timeout=5)
            try:
                conn.request("GET", "/", headers={"Host": "tunnel.test"})
                response = conn.getresponse()
                return response.status, response.read(), response.getheader("Location")
            finally:
                conn.close()
        self.assertEqual(request(True)[:2], (200, b"alive\n"))
        redirect = request(False)
        self.assertIn(redirect[0], (301, 308))
        self.assertEqual(redirect[2], "https://tunnel.test/")
        os.kill(status["caddy_pid"], signal.SIGKILL)
        recovered = self.wait_ready(status["caddy_pid"])
        self.assertEqual(recovered["restarts"], 1)
        self.assertEqual(request(True)[:2], (200, b"alive\n"))
        self.assertIn("resume=True", self.logs())
        routes = api_request("GET", f"{self.settings.api}/id/{client.tunnel_id}")
        self.assertTrue(routes.ok, routes.error)
        self.assertEqual(json.loads(routes.body)["group"], client.owner)
        self.assertEqual(self.run_server("stop").returncode, 0)
        self.assertIsNone(control(self.settings, "status"))
        self.assertFalse(api_request("GET", f"{self.settings.api}/config/", timeout=1).ok)
        # Restart the supervisor itself and retain dynamic routes as well.
        self.assertEqual(self.run_server().returncode, 0, self.logs())
        self.assertEqual(request(True)[:2], (200, b"alive\n"))

    def test_watchdog_restarts_a_hung_caddy(self):
        status = control(self.settings, "status")
        with socket.socket() as upstream:
            upstream.bind(("127.0.0.1", 0))
            upstream.listen()
            self.register("tunnel.test", upstream.getsockname()[1])
            os.kill(status["caddy_pid"], signal.SIGSTOP)
            recovered = self.wait_ready(status["caddy_pid"])
        self.assertTrue(recovered["ready"])
        self.assertIn("Caddy remained unresponsive; restarting", self.logs())


if __name__ == "__main__":
    unittest.main()
