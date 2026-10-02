#!/usr/bin/env python3
"""Native sish lifecycle management, invoked through the POSIX shell wrapper."""

import argparse
import contextlib
import fcntl
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from config import ROOT, Settings
from install_sish import ensure_binary, release_name
from wait_ready import probe


def control(settings, command):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(settings.socket)
            connection.sendall(command.encode() + b"\n")
            data = b""
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    return json.loads(data)
                data += chunk
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def lock_file(path, blocking=True):
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def has_bind_capability(binary):
    try:
        data = os.getxattr(binary, "security.capability")
    except (AttributeError, OSError):
        return False
    # Linux file capabilities: effective flag and permitted CAP_NET_BIND_SERVICE.
    return bool(len(data) >= 12 and int.from_bytes(data[:4], "little") & 1
                and int.from_bytes(data[4:8], "little") & (1 << 10))


def free_ports(settings, binary):
    family = socket.AF_INET6 if ":" in settings.bind else socket.AF_INET
    for port in (settings.http, settings.https, settings.ssh):
        try:
            with socket.socket(family) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((settings.bind, port))
        except PermissionError:
            if sys.platform == "linux" and has_bind_capability(binary):
                # The sish executable can bind even when Python cannot.
                continue
            raise ValueError(
                "Permission denied while binding port {}. On Linux, run 'sh install.sh' "
                "once to grant sish CAP_NET_BIND_SERVICE, then run 'sh run_server.sh'."
                .format(port)) from None
        except OSError as error:
            raise ValueError("Port {} is unavailable: {}. Stop the existing server first.".format(port, error)) from None


class Supervisor:
    def __init__(self, settings, foreground=False):
        self.settings = settings
        self.stop_event = threading.Event()
        self.process = None
        self.ready = False
        self.error = "starting"
        self.restarts = 0
        self.started = time.monotonic()
        self.log = logging.getLogger("sish.server")
        self.log.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler = RotatingFileHandler(settings.runtime / "server.log", maxBytes=10 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(formatter)
        self.log.addHandler(handler)
        if foreground:
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            self.log.addHandler(console)

    def status(self):
        alive = self.process is not None and self.process.poll() is None
        return {"supervisor_pid": os.getpid(), "sish_pid": self.process.pid if alive else None,
                "ready": self.ready and alive, "restarts": self.restarts,
                "uptime_seconds": int(time.monotonic() - self.started), "error": self.error,
                "log": str(self.settings.runtime / "server.log")}

    def pipe_logs(self, process):
        try:
            for line in process.stdout:
                self.log.info("[sish %s] %s", process.pid,
                              line.rstrip().replace(self.settings.password, "[redacted]"))
        finally:
            process.stdout.close()

    def start_child(self, binary, temporary):
        for name in ("ssl", "keys", "pubkeys"):
            (self.settings.runtime / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.process = subprocess.Popen(
            [str(binary)] + self.settings.flags(), cwd=binary.parent,
            env=dict(os.environ, TMPDIR=temporary), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", start_new_session=True,
        )
        threading.Thread(target=self.pipe_logs, args=(self.process,), daemon=True).start()
        self.ready = False
        self.was_ready = False
        self.child_started = time.monotonic()
        self.error = "waiting for sish"
        self.log.info("Started sish pid=%s", self.process.pid)

    def stop_child(self):
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def serve_control(self, listener):
        while not self.stop_event.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2)
                try:
                    command = connection.recv(64).strip()
                    if command == b"stop":
                        self.stop_event.set()
                    connection.sendall(json.dumps(self.status()).encode())
                except OSError:
                    continue

    def run(self):
        settings = self.settings
        with lock_file(settings.runtime / "server.lock", blocking=False):
            Path(settings.socket).unlink(missing_ok=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(settings.socket)
                os.chmod(settings.socket, 0o600)
                listener.listen(8)
                listener.settimeout(0.5)
                thread = threading.Thread(target=self.serve_control, args=(listener,), daemon=True)
                thread.start()
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(signum, lambda *_: self.stop_event.set())
                try:
                    binary = ensure_binary(settings)
                    temporary = tempfile.TemporaryDirectory(prefix="sish-", dir="/tmp")
                    try:
                        self.start_child(binary, temporary.name)
                        failures = 0
                        next_probe = 0
                        while not self.stop_event.wait(0.2):
                            if self.process.poll() is not None:
                                self.log.warning("sish exited (%s); restarting", self.process.returncode)
                                self.ready = False
                                self.error = "sish exited; restarting"
                                if self.stop_event.wait(1):
                                    break
                                self.restarts += 1
                                self.start_child(binary, temporary.name)
                                failures = 0
                                next_probe = 0
                            if time.monotonic() < next_probe:
                                continue
                            try:
                                probe(*settings.addresses())
                                self.ready = self.process.poll() is None
                                self.error = "" if self.ready else "sish exited"
                                self.was_ready = self.was_ready or self.ready
                                failures = 0
                            except (OSError, http.client.HTTPException) as error:
                                self.ready = False
                                self.error = str(error)
                                failures += 1
                                startup_grace_elapsed = time.monotonic() - self.child_started >= settings.start_timeout
                                if failures >= settings.health_failures and (self.was_ready or startup_grace_elapsed):
                                    self.log.warning("Readiness failed %s times; restarting sish", failures)
                                    self.stop_child()
                                    self.restarts += 1
                                    self.start_child(binary, temporary.name)
                                    failures = 0
                            next_probe = time.monotonic() + (settings.health_interval if self.ready else 0.5)
                    finally:
                        self.stop_child()
                        temporary.cleanup()
                finally:
                    self.stop_event.set()
                    self.stop_child()
                    thread.join(timeout=3)
                    Path(settings.socket).unlink(missing_ok=True)
                    self.log.info("sish server stopped")


def stop(settings):
    if control(settings, "status") is None:
        print("The sish server is not running.")
        return 0
    control(settings, "stop")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if control(settings, "status") is None:
            # The lifetime lock closes after the listener and child have stopped.
            try:
                with lock_file(settings.runtime / "server.lock", blocking=False):
                    print("The sish server has stopped.")
                    return 0
            except BlockingIOError:
                pass
        time.sleep(0.1)
    print("sish is taking too long to stop. Check the logs.", file=sys.stderr)
    return 1


def start(settings):
    result = control(settings, "status")
    if result:
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 1
    settings.validate()
    print("Preparing the sish executable...", flush=True)
    binary = ensure_binary(settings)
    free_ports(settings, binary)
    print("Starting sish: HTTP {}, HTTPS {}, SSH {}...".format(
        settings.http, settings.https, settings.ssh), flush=True)
    print("Log: {}".format(settings.runtime / "server.log"), flush=True)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--supervise"], cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started = time.monotonic()
    deadline = started + settings.start_timeout
    next_progress = started + 5
    last_status = None
    while time.monotonic() < deadline:
        result = control(settings, "status")
        if result:
            last_status = result
        if result and result["ready"]:
            print("sish is ready: HTTP {}, HTTPS {}, SSH {} (pid={})".format(
                settings.http, settings.https, settings.ssh, result["sish_pid"]), flush=True)
            return 0
        if process.poll() is not None:
            break
        now = time.monotonic()
        if now >= next_progress:
            detail = result["error"] if result else "waiting for the supervisor"
            print("Waiting for sish ({:.0f}/{:g}s): {}".format(
                now - started, settings.start_timeout, detail), flush=True)
            next_progress = now + 5
        time.sleep(0.1)
    # A failed launch must not leave a silently retrying background server.
    if last_status and last_status["error"]:
        print("Last startup status: {}".format(last_status["error"]), file=sys.stderr, flush=True)
    elif process.poll() is not None:
        print("The supervisor exited before sish was ready (exit code {}).".format(
            process.returncode), file=sys.stderr, flush=True)
    stop(settings)
    print("sish failed its startup check. See {}.".format(settings.runtime / "server.log"), file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="start",
                        choices=("start", "status", "stop", "restart", "logs", "check", "install"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--foreground", action="store_true")
    mode.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.command == "check":
            settings.validate()
            if not settings.binary:
                release_name()
            print("sish configuration is valid. Runs directly without Docker.")
            return 0
        settings.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.command == "status":
            result = control(settings, "status")
            print(json.dumps(result, indent=2) if result else "The sish server is not running.")
            return 0 if result and result["ready"] else 1
        if args.command == "logs":
            path = settings.runtime / "server.log"
            if not path.exists():
                print("No server logs yet.")
                return 0
            os.execvp("tail", ["tail", "-n", "100", "-F", str(path)])
        if args.supervise:
            settings.validate()
            Supervisor(settings).run()
            return 0
        if args.foreground:
            settings.validate()
            binary = ensure_binary(settings)
            if control(settings, "status"):
                raise ValueError("The sish server is already running.")
            free_ports(settings, binary)
            # The lifetime lock belongs to Supervisor. Holding command.lock
            # here would prevent a separate stop/restart command from running.
            Supervisor(settings, foreground=True).run()
            return 0
        with lock_file(settings.runtime / "command.lock"):
            if args.command in ("stop", "restart"):
                code = stop(settings)
                if code or args.command == "stop":
                    return code
            if args.command == "install":
                settings.validate()
                print("sish executable: {}".format(ensure_binary(settings)))
                return 0
            return start(settings)
    except (OSError, ValueError) as error:
        print("sish execution failed: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
