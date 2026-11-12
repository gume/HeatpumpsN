"""Host test of the firmware NVS migration (Config v1-v5, Wi-Fi backup)."""
import shutil
import subprocess
from pathlib import Path

import pytest

FW = Path(__file__).resolve().parents[2] / "firmware"
HOST = FW / "test" / "host"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")


def test_config_migration(tmp_path):
    exe = tmp_path / "config_store_harness"
    subprocess.check_call(["g++", "-std=c++17", "-Wall", "-Wextra",
                           f"-I{FW / 'include'}", f"-I{HOST / 'stubs'}",
                           str(HOST / "config_store_harness.cpp"),
                           str(FW / "src" / "config_store.cpp"), "-o", str(exe)])
    out = subprocess.check_output([str(exe)], text=True)
    assert "ok round-trip" in out
    assert "ok v4 migration" in out
    assert "ok v3 migration" in out
    assert "ok v2 migration" in out
    assert "ok v1 migration" in out
    assert "ok corrupt fallback" in out
    assert "ok unknown-version fallback" in out
    assert "ok missing-config fallback" in out
    assert "ok factory reset clears backup" in out
    assert "all config migration tests passed" in out
