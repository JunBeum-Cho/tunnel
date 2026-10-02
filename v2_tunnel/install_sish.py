"""Fetch an official pinned release, verify SHA256 and install atomically."""

import fcntl
import hashlib
import os
from pathlib import Path
import platform
import tarfile
import tempfile
import urllib.request


VERSION = "2.23.0"
RELEASE = "https://github.com/antoniomika/sish/releases/download/v" + VERSION


def release_name():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in ("linux", "darwin") or arch is None:
        raise ValueError("자동 설치는 Linux/macOS의 amd64·arm64를 지원합니다. SISH_BINARY를 지정하세요.")
    return "sish-{}.{}-{}".format(VERSION, system, arch)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "tunnel-sish-installer"})
    return urllib.request.urlopen(request, timeout=30)


def ensure_binary(settings):
    if settings.binary:
        return settings.binary
    name = release_name()
    parent = settings.runtime / "bin"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (parent / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        binary = parent / name / "sish"
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        archive_name = name + ".tar.gz"
        print("공식 sish v{} 다운로드 및 SHA256 확인 중 ({})...".format(VERSION, name), flush=True)
        with fetch(RELEASE + "/sish-{}-checksums.txt".format(VERSION)) as response:
            checksums = response.read().decode()
        expected = next((line.split()[0] for line in checksums.splitlines()
                         if len(line.split()) == 2 and line.split()[1].lstrip("*") == archive_name), None)
        if expected is None:
            raise ValueError("공식 릴리스에 해당 바이너리의 체크섬이 없습니다.")
        with tempfile.TemporaryDirectory(prefix=".install-", dir=parent) as temporary:
            folder = Path(temporary).resolve()
            archive = folder / "download.tar.gz"
            digest = hashlib.sha256()
            with fetch(RELEASE + "/" + archive_name) as response, archive.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError("sish SHA256 검증에 실패했습니다.")
            with tarfile.open(archive) as package:
                for member in package.getmembers():
                    target = (folder / member.name).resolve()
                    if folder not in target.parents or not (member.isdir() or member.isfile()):
                        raise ValueError("sish 압축 파일에 허용되지 않은 경로가 있습니다.")
                package.extractall(folder)
            extracted = folder / name
            executable = extracted / "sish"
            if not executable.is_file():
                raise ValueError("압축 파일에 sish 실행 파일이 없습니다.")
            executable.chmod(0o700)
            extracted.rename(parent / name)
        return binary
