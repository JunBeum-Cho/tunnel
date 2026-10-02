"""Native sish configuration. No shell execution or dotenv dependency."""

import ipaddress
import math
import os
from pathlib import Path
import shlex


ROOT = Path(__file__).resolve().parent


def read_env(path):
    values = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ValueError(".env:{}: Use KEY=value format.".format(number))
        try:
            tokens = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ValueError(".env:{}: Check for unmatched quotes.".format(number)) from None
        if len(tokens) > 1:
            raise ValueError(".env:{}: Quote values containing spaces.".format(number))
        values[key] = tokens[0] if tokens else ""
    return values


class Settings:
    def __init__(self, env=None, root=ROOT):
        self.root = Path(root).resolve()
        values = read_env(self.root / ".env")
        values.update(os.environ if env is None else env)
        self.values = values
        self.runtime = Path(values.get("SISH_RUNTIME_DIR", self.root / ".runtime")).resolve()
        self.password = values.get("SSH_PASSWORD", "")
        self.domain = values.get("SISH_DOMAIN", "ourmemories.kr")
        self.bind = values.get("SISH_BIND_ADDRESS", "0.0.0.0").strip("[]")
        if self.bind == "localhost":
            self.bind = "127.0.0.1"
        ipaddress.ip_address(self.bind)
        self.http = self.port("SISH_HTTP_PORT", 80)
        self.https = self.port("SISH_HTTPS_PORT", 443)
        self.ssh = self.port("SISH_SSH_PORT", 2222)
        if len({self.http, self.https, self.ssh}) != 3:
            raise ValueError("HTTP, HTTPS and SSH ports must be different.")
        self.start_timeout = self.positive("SISH_START_TIMEOUT", 30)
        self.health_interval = self.positive("SISH_HEALTH_INTERVAL", 5)
        self.health_failures = int(values.get("SISH_HEALTH_FAILURES", 3))
        if self.health_failures <= 0:
            raise ValueError("SISH_HEALTH_FAILURES must be positive.")
        self.ondemand = self.boolean("SISH_HTTPS_ONDEMAND", "true")
        self.verify_dns = self.boolean("SISH_VERIFY_DNS", "true")
        self.binary = Path(values["SISH_BINARY"]).resolve() if values.get("SISH_BINARY") else None
        self.socket = str(self.runtime / "server.sock")

    def port(self, name, default):
        port = int(self.values.get(name, default))
        if not 1 <= port <= 65535:
            raise ValueError("{} must be between 1 and 65535.".format(name))
        return port

    def positive(self, name, default):
        value = float(self.values.get(name, default))
        if value <= 0 or not math.isfinite(value):
            raise ValueError("{} must be a positive finite number.".format(name))
        return value

    def boolean(self, name, default):
        value = self.values.get(name, default).lower()
        if value not in ("true", "false"):
            raise ValueError("{} must be true or false.".format(name))
        return value

    def validate(self):
        if not self.password:
            raise ValueError("Run 'cp .env.example .env', then set SSH_PASSWORD to the same value used by your service Docker container.")
        if not self.domain or any(char.isspace() for char in self.domain) or "/" in self.domain:
            raise ValueError("Set SISH_DOMAIN to a valid domain name.")
        if self.binary and (not self.binary.is_file() or not os.access(self.binary, os.X_OK)):
            raise ValueError("SISH_BINARY must point to an executable sish file.")

    def addresses(self):
        host = "::1" if self.bind == "::" else "127.0.0.1" if self.bind == "0.0.0.0" else self.bind
        return [(host, port) for port in (self.http, self.https, self.ssh)]

    def flags(self):
        host = "[{}]".format(self.bind) if ":" in self.bind else self.bind
        flags = {
            "http-address": "{}:{}".format(host, self.http),
            "https-address": "{}:{}".format(host, self.https),
            "ssh-address": "{}:{}".format(host, self.ssh),
            "domain": self.domain,
            "redirect-root": "false",
            "bind-any-host": "true",
            "bind-root-domain": "true",
            "bind-random-subdomains": "false",
            "force-requested-subdomains": "true",
            "http-load-balancer": "false",
            "authentication": "true",
            "authentication-password": self.password,
            "authentication-keys-directory": str(self.runtime / "pubkeys"),
            "private-keys-directory": str(self.runtime / "keys"),
            "https": "true",
            "force-all-https": "true",
            "https-port-override": str(self.https),
            "https-certificate-directory": str(self.runtime / "ssl"),
            "https-ondemand-certificate": self.ondemand,
            "https-ondemand-certificate-accept-terms": "true",
            "https-ondemand-certificate-email": self.values.get("SISH_CERTIFICATE_EMAIL", ""),
            "idle-connection": "false",
            "verify-dns": self.verify_dns,
        }
        return ["--{}={}".format(key, value) for key, value in flags.items()]
