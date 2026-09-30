"""Isolate all test locks from the host's SirTunnel processes."""
import atexit
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TEST_DIR = tempfile.TemporaryDirectory(prefix="sirtunnel-tests-")
atexit.register(TEST_DIR.cleanup)
os.environ["SIRTUNNEL_LOCK_FILE"] = str(Path(TEST_DIR.name) / "routes.lock")
