#!/usr/bin/env python3
"""Single entry-point supervisor, launched by run_server.sh."""

import argparse
import contextlib
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from tunnel_common import (
    DEFAULT_CADDY_API, SERVER_NAME, LockTimeout, RouteError, api_request, file_lock,
)
from tunnel_cleanup import REAPER_LOCK_FILE, dedupe, reap

ROOT = Path(__file__).resolve().parent


def positive_env(name, default, integer=False):
    value = (int if integer else float)(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


class Settings:
    def __init__(self):
        self.runtime = Path(os.environ.get("SIRTUNNEL_RUNTIME_DIR", ROOT / ".runtime")).resolve()
        self.binary = Path(os.environ.get("SIRTUNNEL_CADDY_BIN", ROOT / "caddy")).resolve()
        self.config = Path(os.environ.get("SIRTUNNEL_CADDY_CONFIG", ROOT / "caddy_config.json")).resolve()
        self.api = os.environ.get("SIRTUNNEL_CADDY_API", DEFAULT_CADDY_API).rstrip("/")
        self.socket = str(self.runtime / "server.sock")
        self.health_interval = positive_env("SIRTUNNEL_HEALTH_INTERVAL", 10)
        self.health_failures = positive_env("SIRTUNNEL_HEALTH_FAILURES", 6, integer=True)
        self.reaper_interval = positive_env("SIRTUNNEL_REAPER_INTERVAL", 30)
        self.reaper_strikes = positive_env("SIRTUNNEL_REAPER_STRIKES", 3, integer=True)
        self.start_timeout = positive_env("SIRTUNNEL_START_TIMEOUT", 30)


def control(settings, command):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(4)
            sock.connect(settings.socket)
            sock.sendall(command.encode() + b"\n")
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return json.loads(b"".join(chunks))
    except (OSError, ValueError):
        return None


class Supervisor:
    def __init__(self, settings, foreground=False):
        self.settings = settings
        self.stop_event = threading.Event()
        self.api_ready = threading.Event()
        self.process = None
        self.restarts = 0
        self.ready = False
        self.error = "starting"
        self.started = time.time()
        self.resumed = False
        self.was_ready = False
        self.force_base = False
        self.log = logging.getLogger("sirtunnel.server")
        self.log.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler = RotatingFileHandler(
            settings.runtime / "server.log", maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8",
        )
        handler.setFormatter(formatter)
        self.log.addHandler(handler)
        if foreground:
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            self.log.addHandler(console)

    def status(self):
        process = self.process
        alive = process is not None and process.poll() is None
        return {
            "supervisor_pid": os.getpid(),
            "caddy_pid": process.pid if alive else None,
            "ready": self.ready and alive,
            "restarts": self.restarts,
            "uptime_seconds": int(time.time() - self.started),
            "error": self.error,
            "log": str(self.settings.runtime / "server.log"),
        }

    def _signal(self, signum, frame):
        self.stop_event.set()

    def _pipe_logs(self, process):
        try:
            for line in process.stdout:
                self.log.info("[caddy %s] %s", process.pid, line.rstrip())
        finally:
            process.stdout.close()

    def _start_caddy(self):
        config_bytes = self.settings.config.read_bytes()
        config = json.loads(config_bytes)
        base_server = config["apps"]["http"]["servers"][SERVER_NAME]
        base_hash = hashlib.sha256(config_bytes).hexdigest()
        self.base_hash = base_hash
        env = os.environ.copy()
        # Separate autosave from other Caddy instances. Preserve the existing
        # data directory so certificate storage and renewal are unchanged.
        env["XDG_CONFIG_HOME"] = str(self.settings.runtime / "config")
        info = subprocess.run(
            [str(self.settings.binary), "environ"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        self.autosave = None
        for line in info.stdout.splitlines():
            if line.startswith("caddy.ConfigAutosavePath="):
                candidate = Path(line.split("=", 1)[1]).resolve()
                if self.settings.runtime in candidate.parents:
                    self.autosave = candidate
        fingerprint = self.settings.runtime / "config.sha256"
        self.resumed = False
        if not self.force_base and self.autosave and self.autosave.exists() and fingerprint.exists():
            try:
                saved = json.loads(self.autosave.read_text())
                saved_server = saved["apps"]["http"]["servers"][SERVER_NAME]
                self.resumed = (
                    fingerprint.read_text().strip() == base_hash
                    and saved_server.get("listen") == base_server.get("listen")
                )
            except (ValueError, OSError, KeyError, TypeError):
                self.log.warning("Invalid autosave; starting from the base configuration")
        args = [str(self.settings.binary), "run", "--config", str(self.settings.config)]
        if self.resumed:
            args.append("--resume")
        self.process = subprocess.Popen(
            args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", start_new_session=True,
        )
        threading.Thread(target=self._pipe_logs, args=(self.process,), daemon=True).start()
        self.log.info("Started Caddy pid=%s, resume=%s", self.process.pid, self.resumed)
        self.spawned_at = time.monotonic()
        self.ready = False
        self.was_ready = False
        self.error = "waiting for Caddy"

    def _stop_caddy(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.log.warning("Caddy did not stop gracefully; sending SIGKILL")
            self.process.kill()
            self.process.wait(timeout=5)

    def _reaper_loop(self):
        state_file = str(self.settings.runtime / "reaper.json")
        while not self.stop_event.is_set():
            if self.api_ready.is_set():
                output = io.StringIO()
                try:
                    with contextlib.redirect_stdout(output):
                        dedupe(self.settings.api, quiet=True)
                        reap(
                            self.settings.api, self.settings.reaper_strikes,
                            state_file, lease_held=True,
                        )
                except Exception as exc:
                    self.log.warning("Reaper pass deferred: %s", exc)
                for line in output.getvalue().splitlines():
                    self.log.info("[reaper] %s", line)
            self.stop_event.wait(self.settings.reaper_interval)

    def _serve_control(self, listener):
        while not self.stop_event.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    command = conn.recv(128).strip()
                    result = self.status()
                    if command == b"stop":
                        self.stop_event.set()
                    elif command != b"status":
                        result = {"error": "unknown command"}
                    conn.sendall(json.dumps(result).encode())
                except OSError:
                    pass

    def run(self):
        with file_lock(str(self.settings.runtime / "server.lock"), timeout=0), file_lock(REAPER_LOCK_FILE, timeout=0):
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, self._signal)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            # Refuse to compete with an existing unmanaged Caddy instance.
            probe = api_request("GET", f"{self.settings.api}/config/", timeout=2)
            if probe.status is not None:
                raise RouteError("A Caddy admin API is already running; stop the old run_server.sh/Caddy before starting this supervisor")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.settings.socket)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(self.settings.socket)
            os.chmod(self.settings.socket, 0o600)
            listener.listen(5)
            listener.settimeout(0.5)
            control_thread = threading.Thread(target=self._serve_control, args=(listener,), daemon=True)
            control_thread.start()
            reaper_thread = threading.Thread(target=self._reaper_loop, daemon=True)
            reaper_thread.start()
            retry_delay = 1
            retry_at = next_check = 0
            failures = 0
            try:
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if self.process is not None and self.process.poll() is not None:
                        self.log.error("Caddy exited with code %s", self.process.returncode)
                        if self.resumed and not self.was_ready:
                            self.force_base = True
                            self.log.warning("Resume failed; next attempt uses the base configuration")
                        if now - self.spawned_at > 60:
                            retry_delay = 1
                        self.process = None
                        self.ready = False
                        self.api_ready.clear()
                        self.error = "Caddy exited; restarting"
                        self.restarts += 1
                        retry_at = now + retry_delay
                        retry_delay = min(retry_delay * 2, 30)
                    if self.process is None and now >= retry_at:
                        try:
                            self._start_caddy()
                            next_check = now
                            failures = 0
                        except Exception as exc:
                            self.log.exception("Could not start Caddy: %s", exc)
                            self.error = str(exc)
                            retry_at = now + retry_delay
                            retry_delay = min(retry_delay * 2, 30)
                    if self.process is not None and now >= next_check:
                        res = api_request(
                            "GET", f"{self.settings.api}/config/apps/http/servers/{SERVER_NAME}/listen",
                            timeout=3,
                        )
                        next_check = time.monotonic() + self.settings.health_interval
                        if res.ok and self.process.poll() is None:
                            if not self.ready:
                                self.log.info("Caddy is ready")
                                try:
                                    (self.settings.runtime / "config.sha256").write_text(self.base_hash)
                                except OSError as exc:
                                    self.log.warning("Could not save config fingerprint: %s", exc)
                            self.ready = True
                            self.was_ready = True
                            self.error = ""
                            self.api_ready.set()
                            failures = 0
                            self.force_base = False
                        else:
                            failures += 1
                            self.ready = False
                            self.api_ready.clear()
                            self.error = res.error
                            self.log.warning("Caddy health check failed (%s/%s): %s", failures, self.settings.health_failures, res.error)
                            if failures >= self.settings.health_failures:
                                self.ready = False
                                self.api_ready.clear()
                                self.log.error("Caddy remained unresponsive; restarting")
                                self._stop_caddy()
                    self.stop_event.wait(0.2)
            finally:
                self.stop_event.set()
                self.api_ready.clear()
                reaper_thread.join(timeout=15)
                self._stop_caddy()
                listener.close()
                control_thread.join(timeout=3)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.settings.socket)
                self.log.info("Server stopped")


def stop(settings):
    result = control(settings, "stop")
    if result is None:
        # A timeout while holding the server lock is not evidence of shutdown.
        try:
            with file_lock(str(settings.runtime / "server.lock"), timeout=0):
                print("Server is already stopped.")
                return 0
        except LockTimeout:
            print("Supervisor is running but its control socket did not answer.", file=sys.stderr)
            return 1
    try:
        with file_lock(str(settings.runtime / "server.lock"), timeout=45):
            print("Server stopped.")
            return 0
    except LockTimeout:
        print("Shutdown is still in progress; inspect server.log.", file=sys.stderr)
        return 1


def start(settings):
    result = control(settings, "status")
    if result:
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 1
    if not os.access(settings.binary, os.X_OK):
        raise RouteError(f"Caddy binary not found: {settings.binary}. Run ./install.sh first.")
    json.loads(settings.config.read_text())
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--supervise"], cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + settings.start_timeout
    while time.monotonic() < deadline:
        result = control(settings, "status")
        if result and result["ready"]:
            print(f"Server is running (supervisor {result['supervisor_pid']}, Caddy {result['caddy_pid']}).")
            print(f"Log: {result['log']}")
            return 0
        if process.poll() is not None:
            print(f"Server could not start. Inspect {settings.runtime / 'server.log'}", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print(f"Caddy is not ready yet; supervisor continues retrying. Inspect {settings.runtime / 'server.log'}", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="start", choices=("start", "status", "stop", "restart"))
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    supervisor = None
    try:
        settings = Settings()
        settings.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.command == "status":
            result = control(settings, "status")
            print(json.dumps(result, indent=2) if result else "Server is not responding/running.")
            return 0 if result and result["ready"] else 1
        if args.command in ("stop", "restart"):
            code = stop(settings)
            if code or args.command == "stop":
                return code
        if args.foreground or args.supervise:
            supervisor = Supervisor(settings, args.foreground)
            supervisor.run()
            return 0
        return start(settings)
    except (RouteError, OSError, ValueError, KeyError) as exc:
        if supervisor:
            supervisor.log.error("Server startup failed: %s", exc)
        print(f"Server failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
