"""Real sish/SSH/HTTPS checks on isolated loopback ports, without ACME or a VPS.

Set SISH_TEST_BIN to an official sish binary. All tests run the actual
`sh run_server.sh` entry point; no Docker, Compose, ACME or VPS is involved.
"""

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for the tunnel")


class App(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ws":
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            try:
                while True:
                    header = self.rfile.read(2)
                    if len(header) != 2:
                        return
                    mask = self.rfile.read(4)
                    data = self.rfile.read(header[1] & 0x7f)
                    payload = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
                    self.wfile.write(bytes([0x81, len(payload)]) + payload)
                    self.wfile.flush()
            except OSError:
                return
        else:
            if self.path == "/slow":
                time.sleep(6)
            self.reply({
                "app": self.server.app_name,
                "host": self.headers.get("Host"),
                "proto": self.headers.get("X-Forwarded-Proto"),
                "path": self.path,
            })

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.reply({"size": len(body), "sha256": hashlib.sha256(body).hexdigest()})


@unittest.skipUnless(os.environ.get("SISH_TEST_BIN"), "Set SISH_TEST_BIN to run real tunnel tests")
class SishIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ["SISH_TEST_BIN"]).resolve()
        for executable in ("ssh", "openssl"):
            if not shutil.which(executable):
                raise unittest.SkipTest("{} is required".format(executable))
        if not cls.binary.is_file():
            raise ValueError("SISH_TEST_BIN must point to an official sish binary")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sish-v2-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.password = secrets.token_hex(16)
        self.http, self.https, self.ssh = unused_port(), unused_port(), unused_port()
        for name in ("keys", "ssl", "pubkeys"):
            (self.directory / name).mkdir()
        certificate = self.directory / "ssl" / "test.crt"
        key = self.directory / "ssl" / "test.key"
        config = self.directory / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\n"
            "[dn]\nCN=example.test\n[ext]\n"
            "subjectAltName=DNS:example.test,DNS:*.example.test,DNS:*.other.test\n"
        )
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=example.test", "-config", str(config),
            "-keyout", str(key), "-out", str(certificate),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.context = ssl.create_default_context(cafile=str(certificate))
        self.askpass = self.directory / "askpass.sh"
        self.askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$SISH_TEST_PASSWORD"\n')
        self.askpass.chmod(0o700)
        self.processes = []
        self.apps = []
        self.logs = []
        self.addCleanup(self.close)
        self.start_gateway()

    def start_gateway(self):
        self.env = dict(os.environ, SSH_PASSWORD=self.password, SISH_BINARY=str(self.binary),
                        SISH_RUNTIME_DIR=str(self.directory), SISH_BIND_ADDRESS="127.0.0.1",
                        SISH_DOMAIN="example.test", SISH_HTTP_PORT=str(self.http),
                        SISH_HTTPS_PORT=str(self.https), SISH_SSH_PORT=str(self.ssh),
                        SISH_HTTPS_ONDEMAND="false", SISH_VERIFY_DNS="true",
                        SISH_HEALTH_INTERVAL="0.3", SISH_HEALTH_FAILURES="2", SISH_START_TIMEOUT="10")
        log = (self.directory / "sish-{}.log".format(len(self.logs))).open("w+")
        self.logs.append(log)
        self.gateway = subprocess.Popen(
            ["sh", str(ROOT / "run_server.sh"), "--foreground"],
            cwd=ROOT, env=self.env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        self.processes.append(self.gateway)
        result = subprocess.run([
            "python3", str(ROOT / "wait_ready.py"),
            "127.0.0.1:{}".format(self.http),
            "127.0.0.1:{}".format(self.https),
            "127.0.0.1:{}".format(self.ssh), "--timeout", "10",
        ], capture_output=True, text=True)
        if result.returncode:
            log.seek(0)
            self.fail(result.stderr + log.read().replace(self.password, "[password]"))
        wait_until(lambda: self.status())

    def command(self, *args):
        return subprocess.run(["sh", str(ROOT / "run_server.sh"), *args],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=25)

    def status(self):
        result = self.command("status")
        return json.loads(result.stdout) if result.returncode == 0 else None

    def close(self):
        self.command("stop")
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for app in self.apps:
            app.shutdown()
            app.server_close()
        for log in self.logs:
            log.close()

    def app(self, name):
        app = ThreadingHTTPServer(("127.0.0.1", 0), App)
        app.daemon_threads = True
        app.app_name = name
        threading.Thread(target=app.serve_forever, daemon=True).start()
        self.apps.append(app)
        return app

    def forward(self, domain, app, password=None):
        log = (self.directory / "ssh-{}.log".format(len(self.logs))).open("w+")
        self.logs.append(log)
        process = subprocess.Popen([
            "ssh", "-N", "-T", "-p", str(self.ssh),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=1", "-o", "ServerAliveCountMax=3",
            "-R", "{}:80:localhost:{}".format(domain, app.server_port),
            "linuxuser@127.0.0.1",
        ], env=dict(os.environ, SSH_ASKPASS=str(self.askpass), SSH_ASKPASS_REQUIRE="force",
                    DISPLAY="integration-test", SISH_TEST_PASSWORD=password or self.password),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            start_new_session=True)
        self.processes.append(process)
        return process

    def tls_socket(self, domain):
        connection = socket.create_connection(("127.0.0.1", self.https), timeout=10)
        return self.context.wrap_socket(connection, server_hostname=domain)

    def request(self, domain, path="/", method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.https, timeout=10)
        connection.sock = self.tls_socket(domain)
        try:
            connection.request(method, path, body=body, headers={"Host": domain})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def route_ready(self, domain, name):
        def ready():
            status, body = self.request(domain)
            return status == 200 and json.loads(body)["app"] == name
        wait_until(ready)

    def test_domains_route_to_different_apps_and_preserve_headers(self):
        for domain, name in (
            ("first.example.test", "first"),
            ("second.other.test", "second"),
            ("example.test", "root"),
        ):
            self.forward(domain, self.app(name))
            self.route_ready(domain, name)
            status, body = self.request(domain, "/nested/path?x=1")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {
                "app": name, "host": domain, "proto": "https", "path": "/nested/path?x=1",
            })
        connection = http.client.HTTPConnection("127.0.0.1", self.http, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/", headers={"Host": "first.example.test"})
        response = connection.getresponse()
        self.assertIn(response.status, (301, 302, 307, 308))
        self.assertEqual(response.getheader("Location"), "https://first.example.test:{}/".format(self.https))

    def test_duplicate_domain_fails_without_replacing_live_service(self):
        self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        duplicate = self.forward("first.example.test", self.app("replacement"))
        self.assertNotEqual(duplicate.wait(timeout=10), 0)
        self.route_ready("first.example.test", "first")

    def test_disconnect_removes_route_and_reconnect_registers_it(self):
        first = self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        first.terminate()
        first.wait(timeout=3)
        wait_until(lambda: self.request("first.example.test")[0] == 404)
        self.forward("first.example.test", self.app("replacement"))
        self.route_ready("first.example.test", "replacement")

    def test_wrong_password_cannot_register(self):
        process = self.forward("first.example.test", self.app("first"), password="wrong-password")
        self.assertNotEqual(process.wait(timeout=10), 0)
        self.assertEqual(self.request("first.example.test")[0], 404)

    def test_health_checks_keep_real_connection_errors_visible(self):
        log = self.directory / "server.log"
        # Ignore the standalone startup probe, which runs outside the supervisor.
        time.sleep(1)
        offset = log.stat().st_size
        time.sleep(1)
        quiet = log.read_text()[offset:]
        self.assertNotIn("cannot find connection for host: localhost", quiet)
        self.assertNotIn("Accepted SSH connection for:", quiet)
        self.assertNotIn("TLS handshake error", quiet)
        self.assertNotIn("SSH connection could not be established", quiet)

        self.forward("first.example.test", self.app("first"), password="wrong-password").wait(timeout=10)
        wait_until(lambda: "Login attempt:" in log.read_text()[offset:])
        self.assertIn("Accepted SSH connection for:", log.read_text()[offset:])
        context = ssl._create_unverified_context()
        with socket.create_connection(("127.0.0.1", self.https), timeout=3) as connection:
            with self.assertRaises(ssl.SSLError):
                context.wrap_socket(connection, server_hostname="unregistered.invalid")
        wait_until(lambda: "TLS handshake error" in log.read_text()[offset:])
        self.forward("first.example.test", self.apps[0])
        self.route_ready("first.example.test", "first")

    def test_large_upload_and_response_after_five_seconds(self):
        self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        body = b"upload-test" * (1024 * 1024)
        status, result = self.request("first.example.test", "/upload", method="POST", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result), {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        self.assertEqual(self.request("first.example.test", "/slow")[0], 200)

    def test_idle_websocket_survives_other_service_registration_and_cleanup(self):
        self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        with self.tls_socket("first.example.test") as connection:
            connection.sendall(
                b"GET /ws HTTP/1.1\r\nHost: first.example.test\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            headers = b""
            while not headers.endswith(b"\r\n\r\n"):
                chunk = connection.recv(1)
                self.assertTrue(chunk, "WebSocket handshake closed")
                headers += chunk
            self.assertIn(b" 101 ", headers)
            other = self.forward("second.example.test", self.app("second"))
            self.route_ready("second.example.test", "second")
            other.terminate()
            other.wait(timeout=3)
            wait_until(lambda: self.request("second.example.test")[0] == 404)
            time.sleep(6)
            connection.sendall(b"\x81\x84\x00\x00\x00\x00ping")
            response = b""
            while len(response) < 6:
                chunk = connection.recv(6 - len(response))
                self.assertTrue(chunk, "WebSocket closed after another service changed")
                response += chunk
            self.assertEqual(response, b"\x81\x04ping")

    def test_gateway_restart_allows_client_to_register_again(self):
        first = self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        self.gateway.terminate()
        self.gateway.wait(timeout=3)
        self.assertNotEqual(first.wait(timeout=10), 0)
        self.start_gateway()
        self.assertEqual(self.request("first.example.test")[0], 404)
        self.forward("first.example.test", self.apps[0])
        self.route_ready("first.example.test", "first")

    def test_repeated_start_keeps_existing_process_and_live_tunnel(self):
        self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        before = self.status()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status()["sish_pid"], before["sish_pid"])
        self.route_ready("first.example.test", "first")

    def test_detached_start_restart_and_idempotent_stop_preserve_keys(self):
        self.assertEqual(self.command("stop").returncode, 0)
        self.gateway.wait(timeout=5)
        keys = {path.name: path.read_bytes() for path in (self.directory / "keys").iterdir() if path.is_file()}
        self.assertTrue(keys)
        self.addCleanup(lambda: self.command("stop"))
        started = self.command()
        self.assertEqual(started.returncode, 0, started.stderr)
        before = self.status()
        self.assertTrue(before["ready"])
        self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")
        result = self.command("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.status()["sish_pid"], before["sish_pid"])
        self.assertEqual(self.request("first.example.test")[0], 404)
        self.forward("first.example.test", self.apps[0])
        self.route_ready("first.example.test", "first")
        self.assertEqual(keys, {path.name: path.read_bytes() for path in (self.directory / "keys").iterdir() if path.is_file()})
        self.assertEqual(self.command("stop").returncode, 0)
        self.assertEqual(self.command("stop").returncode, 0)
        self.assertIsNone(self.status())

    def test_killed_sish_process_is_restarted(self):
        before = self.status()
        os.kill(before["sish_pid"], signal.SIGKILL)
        def recovered():
            current = self.status()
            return current and current["sish_pid"] != before["sish_pid"] and current["restarts"] >= 1
        wait_until(recovered, timeout=15)
        self.assertEqual(self.request("first.example.test")[0], 404)

    def test_unresponsive_sish_process_is_restarted(self):
        before = self.status()
        os.kill(before["sish_pid"], signal.SIGSTOP)
        def recovered():
            current = self.status()
            return current and current["sish_pid"] != before["sish_pid"] and current["restarts"] >= 1
        wait_until(recovered, timeout=20)
        self.assertEqual(self.request("first.example.test")[0], 404)

    def test_busy_port_fails_without_leaving_a_background_server(self):
        self.assertEqual(self.command("stop").returncode, 0)
        self.gateway.wait(timeout=5)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.http))
            listener.listen()
            result = self.command()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(str(self.http), result.stderr)
            self.assertIsNone(self.status())

    def test_start_creates_missing_key_directories(self):
        self.assertEqual(self.command("stop").returncode, 0)
        self.gateway.wait(timeout=5)
        for name in ("keys", "pubkeys"):
            shutil.rmtree(self.directory / name)
        self.addCleanup(lambda: self.command("stop"))
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.directory / "keys").is_dir())
        self.assertTrue((self.directory / "pubkeys").is_dir())
        self.forward("first.example.test", self.app("first"))
        self.route_ready("first.example.test", "first")


if __name__ == "__main__":
    unittest.main()
