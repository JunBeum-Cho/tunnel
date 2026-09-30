"""Opt-in tests with a real Caddy binary on isolated, unprivileged ports."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from server import Settings, control
from tunnel import TunnelClient
from tunnel_common import api_request

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("SIRTUNNEL_TEST_CADDY_BIN")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
        os.kill(status["caddy_pid"], signal.SIGSTOP)
        recovered = self.wait_ready(status["caddy_pid"])
        self.assertTrue(recovered["ready"])
        self.assertIn("Caddy remained unresponsive; restarting", self.logs())


if __name__ == "__main__":
    unittest.main()
