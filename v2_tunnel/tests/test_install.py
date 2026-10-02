"""Exercise the real installer with isolated Linux privilege/firewall commands.

Uses the real configuration parser and binary preparation entry point. All root,
package, capability and firewall commands are PATH stubs inside a temp directory;
no host firewall, privilege or package changes are made.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STUB = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
directory = Path(os.environ["INSTALL_TEST_STATE"])
with (directory / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([name] + args) + "\n")
state_file = directory / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
result = 0
if name == "uname":
    print(os.environ.get("INSTALL_TEST_OS", "Linux"))
elif name == "id":
    print(os.environ.get("INSTALL_TEST_UID", "1000"))
elif name == "sudo":
    sys.exit(subprocess.run(args).returncode)
elif name == "setcap":
    result = int(os.environ.get("INSTALL_TEST_SETCAP_FAIL", "0"))
elif name == "apt-get":
    result = int(os.environ.get("INSTALL_TEST_APT_FAIL", "0"))
    if result == 0:
        (Path(sys.argv[0]).parent / "setcap").symlink_to("dispatcher")
elif name == "ufw":
    result = int(os.environ.get("INSTALL_TEST_UFW_FAIL", "0"))
    if result == 0 and args[0] == "allow":
        rules = state.setdefault("ufw", [])
        if args[1] not in rules:
            rules.append(args[1])
        print("Rule added or already present")
    elif result == 0 and args[0] == "status":
        print("Status: " + os.environ.get("INSTALL_TEST_UFW_STATUS", "active"))
elif name == "firewall-cmd":
    if args == ["--state"]:
        result = 0 if os.environ.get("INSTALL_TEST_FIREWALLD", "running") == "running" else 252
    elif args == ["--get-active-zones"]:
        print("external\n  interfaces: eth0\ninternal\n  sources: 10.0.0.0/8")
    elif args == ["--get-default-zone"]:
        print(os.environ.get("INSTALL_TEST_DEFAULT_ZONE", "external"))
    else:
        zone = next(item.split("=", 1)[1] for item in args if item.startswith("--zone="))
        port = next(item.split("=", 1)[1] for item in args if "-port=" in item)
        key = ("permanent" if "--permanent" in args else "runtime") + ":" + zone
        rules = state.setdefault(key, [])
        if any(item.startswith("--query-port=") for item in args):
            result = int(os.environ.get("INSTALL_TEST_QUERY_FAIL", "0"))
            if not result:
                result = 0 if port in rules else 1
        elif port not in rules:
            rules.append(port)
state_file.write_text(json.dumps(state))
sys.exit(result)
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sish-installer-test-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name).resolve()
        self.project = self.directory / "tunnel with spaces"
        self.project.mkdir()
        for source in list(ROOT.glob("*.py")) + [ROOT / "install.sh", ROOT / "run_server.sh"]:
            shutil.copyfile(source, self.project / source.name)
        self.binary = self.project / "custom sish"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        dispatcher = self.bin / "dispatcher"
        dispatcher.write_text("#!{}\n".format(sys.executable) + STUB)
        dispatcher.chmod(0o700)
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "sh").symlink_to(shutil.which("sh"))
        (self.bin / "awk").symlink_to(shutil.which("awk"))
        (self.bin / "dirname").symlink_to(shutil.which("dirname"))
        for command in ("uname", "id", "sudo", "setcap"):
            self.add_command(command)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("SISH_") and key != "SSH_PASSWORD"}
        self.env.update(PATH=str(self.bin), INSTALL_TEST_STATE=str(self.directory))
        self.write_config()

    def add_command(self, name):
        (self.bin / name).symlink_to("dispatcher")

    def write_config(self, ports=(80, 443, 2222), bind="0.0.0.0"):
        (self.project / ".env").write_text(
            "SSH_PASSWORD='private value $never_execute'\n"
            "SISH_BINARY='{}'\nSISH_BIND_ADDRESS={}\n"
            "SISH_HTTP_PORT={}\nSISH_HTTPS_PORT={}\nSISH_SSH_PORT={}\n".format(
                self.binary, bind, *ports))

    def run_installer(self, success=True):
        result = subprocess.run([str(self.bin / "sh"), str(self.project / "install.sh")],
                                env=self.env, capture_output=True, text=True, timeout=15)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Local installation complete", result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Local installation complete", result.stdout)
        self.assertNotIn("private value", result.stdout + result.stderr)
        (result.stdout + result.stderr).encode("ascii")
        return result

    def calls(self, name=None):
        path = self.directory / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [call for call in calls if name is None or call[0] == name]

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def test_ufw_default_ports_capability_and_repeat_install(self):
        self.add_command("ufw")
        result = self.run_installer()
        self.run_installer()
        self.assertEqual(self.state()["ufw"], ["80/tcp", "443/tcp", "2222/tcp"])
        self.assertEqual(self.calls("setcap"), [["setcap", "cap_net_bind_service=+ep", str(self.binary)]] * 2)
        self.assertTrue(self.calls("sudo"))
        self.assertIn("Vultr Firewall Group", result.stdout)
        self.assertFalse(any(call[1:] in (["enable"], ["reset"], ["disable"]) for call in self.calls("ufw")))

    def test_inactive_ufw_custom_ports_and_environment_precedence(self):
        self.add_command("ufw")
        self.write_config(ports=(8080, 8443, 2223))
        self.env.update(INSTALL_TEST_UFW_STATUS="inactive", SISH_SSH_PORT="2224")
        result = self.run_installer()
        self.assertEqual(self.state()["ufw"], ["8080/tcp", "8443/tcp", "2224/tcp"])
        self.assertIn("UFW remains inactive", result.stdout)
        self.assertIn("8080 8443 2224", result.stdout)

    def test_firewalld_runtime_and_permanent_in_all_active_zones(self):
        self.add_command("firewall-cmd")
        self.env["INSTALL_TEST_DEFAULT_ZONE"] = "public"
        self.run_installer()
        self.run_installer()
        state = self.state()
        for zone in ("external", "internal", "public"):
            for kind in ("runtime", "permanent"):
                self.assertEqual(state[kind + ":" + zone], ["80/tcp", "443/tcp", "2222/tcp"])
        adds = [call for call in self.calls("firewall-cmd") if any("--add-port=" in arg for arg in call)]
        self.assertEqual(len(adds), 18)
        self.assertFalse(any("--reload" in call for call in self.calls("firewall-cmd")))

    def test_default_zone_already_active_is_not_processed_twice(self):
        self.add_command("firewall-cmd")
        self.run_installer()
        queries = [call for call in self.calls("firewall-cmd") if any("--query-port=" in arg for arg in call)]
        self.assertEqual(len(queries), 12)

    def test_both_firewall_managers_are_configured(self):
        self.add_command("ufw")
        self.add_command("firewall-cmd")
        self.run_installer()
        self.assertIn("ufw", self.state())
        self.assertIn("runtime:external", self.state())

    def test_missing_setcap_installs_libcap_package_as_root(self):
        (self.bin / "setcap").unlink()
        self.add_command("apt-get")
        self.env["INSTALL_TEST_UID"] = "0"
        self.run_installer()
        self.assertEqual(self.calls("apt-get"), [["apt-get", "install", "-y", "libcap2-bin"]])
        self.assertEqual(len(self.calls("setcap")), 1)
        self.assertFalse(self.calls("sudo"))

    def test_missing_setcap_without_package_manager_fails(self):
        (self.bin / "setcap").unlink()
        result = self.run_installer(success=False)
        self.assertIn("setcap is required", result.stderr)

    def test_non_root_without_sudo_fails_with_permission_message(self):
        (self.bin / "sudo").unlink()
        result = self.run_installer(success=False)
        self.assertIn("Root permission is required", result.stderr)
        self.assertFalse(self.calls("setcap"))

    def test_failed_package_or_capability_setup_fails_installation(self):
        self.env["INSTALL_TEST_SETCAP_FAIL"] = "1"
        self.run_installer(success=False)
        (self.bin / "setcap").unlink()
        self.add_command("apt-get")
        self.env["INSTALL_TEST_APT_FAIL"] = "100"
        self.run_installer(success=False)

    def test_firewall_write_and_query_errors_fail_installation(self):
        self.add_command("ufw")
        self.env["INSTALL_TEST_UFW_FAIL"] = "1"
        self.run_installer(success=False)
        (self.bin / "ufw").unlink()
        self.add_command("firewall-cmd")
        self.env["INSTALL_TEST_QUERY_FAIL"] = "252"
        result = self.run_installer(success=False)
        self.assertIn("Could not query firewalld rules", result.stderr)
        self.assertFalse(any("--add-port=" in arg for call in self.calls("firewall-cmd") for arg in call))

    def test_no_firewall_and_inactive_firewalld_are_reported(self):
        self.add_command("firewall-cmd")
        self.env["INSTALL_TEST_FIREWALLD"] = "inactive"
        result = self.run_installer()
        self.assertIn("No UFW or running firewalld found", result.stdout)
        self.assertIn("custom nftables/iptables", result.stdout)
        self.assertFalse(any("--add-port=" in arg for call in self.calls("firewall-cmd") for arg in call))

    def test_loopback_address_is_reported(self):
        self.write_config(bind="127.0.0.1")
        result = self.run_installer()
        self.assertIn("Remote service Docker containers cannot connect", result.stdout)

    def test_non_linux_skips_privileged_setup(self):
        self.env["INSTALL_TEST_OS"] = "Darwin"
        result = self.run_installer()
        self.assertIn("Linux firewall and port permission setup skipped", result.stdout)
        self.assertFalse(self.calls("setcap"))
        self.assertFalse(self.calls("sudo"))

    def test_invalid_ports_fail_before_privileged_changes(self):
        self.write_config(ports=(80, 443, 80))
        self.run_installer(success=False)
        self.assertFalse(self.calls("setcap"))
        self.assertFalse(self.calls("sudo"))


if __name__ == "__main__":
    unittest.main()
