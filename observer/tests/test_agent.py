import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import pytest

from observer.agent import skills
from observer.agent.data import Series, flux_every, flux_time
from observer.agent.runner import Run, execute
from observer.agent.sandbox import Limits, Sandbox
from observer.agent.tools import Toolbox

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class FakeService:
    def signals(self):
        row = lambda i, **info: {"id": i, "source": i.split("/")[0], "store": True, "info": info, "meta": {},
                                 "unit": "°C", "quantity": "temperature", "device": "", "value": 1.0, "age_s": 5}
        rows = [row("heatpumpsn/controller/state/temp_mix", name="Floor forward", circuit="floor", role="supply"),
                row("ems-esp/boiler_data#heatingactive", name="Heating active", role="burner"),
                row("heatpumpsn/controller/log/event#code"), row("heatpumpsn/controller/log/event#severity"),
                row("heatpumpsn/controller/log/event#detail")]
        rows[1]["quantity"] = "state"
        return rows

    def house(self):
        return {"current": {"description": "# House\nFloor circuit BF.", "model": {"checks": [{"name": "c"}]},
                            "audited": "2026-11-01"}, "proposal": None, "notes": "BF is the bottom floor."}


class FakeData:
    def __init__(self):
        self.series_data = {
            "heatpumpsn/controller/state/temp_mix": Series("heatpumpsn/controller/state/temp_mix",
                                                           [at(m) for m in range(0, 300, 2)],
                                                           [30.0] * 100 + [30.0 + i * 0.1 for i in range(50)]),
            "ems-esp/boiler_data#heatingactive": Series("ems-esp/boiler_data#heatingactive",
                                                        [at(m) for m in range(0, 60, 5)],
                                                        [0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0]),
            "heatpumpsn/controller/log/event#code": Series("e", [at(1), at(9)], [1302.0, 1340.0]),
            "heatpumpsn/controller/log/event#severity": Series("e", [at(1), at(9)], ["warn", "error"]),
            "heatpumpsn/controller/log/event#detail": Series("e", [at(1), at(9)], ["encoder", "MPC anomaly"]),
        }
        self.calls = []

    def series(self, ids, start, stop="now", every="raw", agg="mean"):
        flux_time(start), flux_time(stop), flux_every(every)     # same validation as the real source
        self.calls.append((tuple(ids), every))
        return {i: Series(i, self.series_data[i].times, self.series_data[i].values)
                for i in ids if i in self.series_data}


def box(tmp_path, sandbox=None):
    return Toolbox(FakeService(), FakeData(), sandbox, str(tmp_path / "ws"), "context")


def test_flux_arguments_are_validated():
    assert flux_time("now") == "now()" and flux_time("-6h") == "-6h"
    assert flux_time("2026-11-01T10:00:00+01:00") == "2026-11-01T09:00:00Z"
    assert flux_every("raw") is None and flux_every("15m") == "15m"
    for bad in ('-1h) |> drop(', '6h', 'yesterday'):
        with pytest.raises(ValueError):
            flux_time(bad)
    with pytest.raises(ValueError):
        flux_every("1m, fn: x")


def test_find_signals_and_unknown_ids(tmp_path):
    tb = box(tmp_path)
    found = tb.call("find_signals", {"circuit": "floor"})
    assert [s["id"] for s in found["signals"]] == ["heatpumpsn/controller/state/temp_mix"]
    assert tb.call("find_signals", {"text": "heating active"})["count"] == 1
    assert "unknown signal ids" in tb.call("get_series", {"signals": ['x") |> drop()']})["error"]
    assert "unknown tool" in tb.call("delete_everything", {})["error"]


def test_get_series_writes_csv_and_summary(tmp_path):
    tb = box(tmp_path)
    out = tb.call("get_series", {"signals": ["heatpumpsn/controller/state/temp_mix"], "start": "-6h", "file": "fwd"})
    assert out["file"] == "data/fwd.csv"
    s = out["summary"]["heatpumpsn/controller/state/temp_mix"]
    assert s["points"] == 150 and s["min"] == 30.0 and s["max"] == pytest.approx(34.9)
    with open(tmp_path / "ws" / "data" / "fwd.csv") as fp:
        lines = fp.read().splitlines()
    assert lines[0] == "time,heatpumpsn/controller/state/temp_mix" and len(lines) == 151


def test_signal_features_stuck_and_cycles(tmp_path):
    tb = box(tmp_path)
    f = tb.call("signal_features", {"signal": "heatpumpsn/controller/state/temp_mix"})
    assert f["longest_constant_s"] == pytest.approx(100 * 120) and f["longest_constant_value"] == 30.0
    b = tb.call("signal_features", {"signal": "ems-esp/boiler_data#heatingactive"})
    assert b["switch_ons"] == 3 and b["shortest_on_s"] == 300 and b["duty"] == pytest.approx(30 / 55, abs=0.001)


def test_events_severity_and_workspace_guard(tmp_path):
    tb = box(tmp_path)
    ev = tb.call("get_events", {"min_severity": 2})
    assert ev["count"] == 1 and ev["events"][0]["code"] == 1340 and ev["events"][0]["severity"] == "error"
    assert tb.call("get_events", {})["count"] == 2
    assert "stay inside" in tb.call("read_file", {"path": "../../.env"})["error"]
    assert tb.call("house_context", {})["status"].startswith("audited")


def test_skills_load_and_install(tmp_path):
    loaded = skills.load([str(tmp_path / "none")])
    assert {"sensor-health", "heating-start", "boiler-cycling"} <= set(loaded)
    hs = loaded["heating-start"]
    assert hs.defaults == {"window": "-24h"} and "starts.py" in hs.helpers()
    assert hs.install(str(tmp_path)) == ["skill/starts.py"]
    local = tmp_path / "house_skills" / "heating-start"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\ntitle: Local version\n---\nDo it the local way.")
    assert skills.load([str(tmp_path / "house_skills")])["heating-start"].origin == "house"


class ScriptedLlm:
    """Answers with the given tool calls, one list per step."""
    model = "scripted"

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def chat_tools(self, messages, tools):
        self.seen.append((messages, [t["function"]["name"] for t in tools]))
        calls = self.steps.pop(0)
        return ({"role": "assistant", "content": None,
                 "tool_calls": [{"id": f"c{i}", "type": "function",
                                 "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]},
                {"prompt_tokens": 10, "completion_tokens": 2})


def test_agent_loop_records_the_run(tmp_path):
    llm = ScriptedLlm([[("house_context", {})],
                       [("signal_features", {"signal": "ems-esp/boiler_data#heatingactive"}), ("get_events", {})],
                       [("finish", {"report_md": "# Report\nAll good.", "confidence": 0.8,
                                    "findings": [{"title": "short cycles", "severity": "warning"}]})]])
    folder = tmp_path / "run"
    folder.mkdir()
    tb = Toolbox(FakeService(), FakeData(), None, str(folder))
    meta = execute(Run(str(folder)), llm, tb, "Does the boiler short-cycle?", skills.load()["boiler-cycling"])
    assert meta["status"] == "done" and meta["steps"] == 3 and meta["usage"]["prompt_tokens"] == 30
    assert (folder / "report.md").read_text().startswith("# Report")
    steps = Run(str(folder)).steps()
    assert [s["tool"] for s in steps if s["kind"] == "tool"] == ["house_context", "signal_features", "get_events",
                                                                  "finish"]
    # The tool results went back to the model, the recipe was in the prompt.
    assert "Recipe:" in llm.seen[0][0][1]["content"]
    assert any(m.get("role") == "tool" and m["tool_call_id"] == "c1" for m in llm.seen[2][0])


def test_agent_loop_forces_finish_at_the_step_limit(tmp_path):
    llm = ScriptedLlm([[("list_files", {})], [("finish", {"report_md": "partial"})]])
    folder = tmp_path / "run"
    folder.mkdir()
    meta = execute(Run(str(folder)), llm, Toolbox(FakeService(), FakeData(), None, str(folder)), "q", max_steps=2)
    assert meta["status"] == "done" and llm.seen[1][1] == ["finish"]


needs_bwrap = pytest.mark.skipif(not shutil.which("bwrap") or not shutil.which("prlimit"), reason="bwrap missing")


@needs_bwrap
def test_sandbox_isolation(tmp_path):
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=x")
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = Sandbox(sys.prefix, limits=Limits(wall_s=5, memory_mb=200))
    r = sb.run(f"""
import os, socket
print("secret", os.path.exists({str(secret)!r}))
try:
    socket.create_connection(("1.1.1.1", 53), 1); print("net open")
except OSError:
    print("net blocked")
open("out.txt", "w").write("hi")
print(sorted(k for k in os.environ if "TOKEN" in k or "KEY" in k))
""", str(ws), "probe")
    assert r.exit_code == 0, r.stderr
    assert "secret False" in r.stdout and "net blocked" in r.stdout and "[]" in r.stdout
    assert r.new_files == ["out.txt"]
    assert sb.run("while True: pass", str(ws), "loop").timed_out
