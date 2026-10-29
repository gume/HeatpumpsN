"""Host test of firmware L2 floor coordination: warm-up flush and delta-T pause."""
import shutil
import subprocess
from pathlib import Path

import pytest

FW = Path(__file__).resolve().parents[2] / "firmware"
HOST = FW / "test" / "host"
AJ = FW / ".pio" / "libdeps" / "esp32dev_sim" / "ArduinoJson" / "src"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None or not AJ.exists(),
                                reason="needs g++ and the PlatformIO ArduinoJson checkout")


def test_flush_and_delta_t_pause(tmp_path):
    exe = tmp_path / "l2_harness"
    subprocess.check_call(["g++", "-std=c++17", "-Wall", f"-I{FW / 'include'}", f"-I{HOST / 'stubs'}", f"-I{AJ}",
                           str(HOST / "l2_harness.cpp"), str(FW / "src" / "l2_control.cpp"), "-o", str(exe)])
    out = subprocess.check_output([str(exe)], text=True)
    lines = {ln.split()[0]: ln for ln in out.splitlines() if not ln.startswith("event")}
    assert "flush=1 req=1" in lines["start"]
    assert "flush=0" in lines["after40"]
    assert "paused=1 req=0" in lines["collapse"]
    assert "paused=1 req=0 phys=0" in lines["mid"]          # held for the whole pause, no after-run
    assert "flush=1 req=1" in lines["restart"]               # restart goes through a flush
    assert "flush=0 req=1" in lines["end"]
