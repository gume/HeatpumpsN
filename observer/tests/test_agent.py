import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import pytest

from observer.agent.broker import DataBroker
from observer.agent.data import Series, flux_every, flux_time
from observer.agent.library import Library
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
                            "knowledge": "## Comfort targets\n- BF 21 °C", "accepted": "2026-11-01"},
                "draft": None, "notes": "BF is the bottom floor."}


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

    def coverage(self, ids):
        return {i: {"first": "2026-11-01T10:00:00Z", "last": "2026-11-01T14:58:00Z",
                    "points": len(self.series_data[i].values)} for i in ids}

    def series(self, ids, start, stop="now", every="raw", agg="mean", limit=50_000):
        flux_time(start), flux_time(stop), flux_every(every)     # same validation as the real source
        self.calls.append((tuple(ids), every))
        return {i: Series(i, self.series_data[i].times, self.series_data[i].values)
                for i in ids if i in self.series_data}


def box(tmp_path, sandbox=None):
    return Toolbox(FakeService(), FakeData(), sandbox, str(tmp_path / "ws"), "context")


def test_flux_arguments_are_validated():
    assert flux_time("now") == "now()" and flux_time("-6h") == "-6h"
    assert flux_time("2026-11-01T10:00:00+01:00") == "2026-11-01T09:00:00Z"
    assert flux_every("raw") is None and flux_every("15m") == "15m" and flux_every("5min") == "5m"
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
    ctx = tb.call("house_context", {})
    assert ctx["status"].startswith("accepted") and ctx["owner_knowledge_md"].endswith("BF 21 °C")


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "library")


def lib_at(tmp_path):
    """House items in tmp, plus the fixture library as a library folder."""
    return Library(str(tmp_path / "lib"), [FIXTURE])


TOOL_CODE = "def run(x, factor=2):\n    return x * factor\n"
TOOL_TESTS = "from tool import run\n\n\ndef test_double():\n    assert run(3) == 6\n"


def test_library_sources_and_house_tools(tmp_path):
    lib = lib_at(tmp_path)
    assert [x.kind for x in lib.sources()] == ["house", "path"]
    assert lib.sources()[1].name == "fixture-library" and lib.sources()[1].version == "1.0.0"
    double = lib.get("tools", "double")
    assert double.origin == "path" and double.version == 3 and not double.audited
    assert lib.get("skills", "demo_recipe").version == 2
    with pytest.raises(ValueError):
        lib.save_tool("Bad-Name", "x", [], {"description": "y"}, TOOL_CODE, TOOL_TESTS)
    with pytest.raises(ValueError):
        lib.save_tool("triple", "x", [], {"description": "y"}, "x = 1", TOOL_TESTS)      # no run()
    item = lib.save_tool("triple", "Triples a number", [{"name": "x", "type": "number"}],
                         {"type": "number", "description": "3x"}, TOOL_CODE, TOOL_TESTS, author="run 1")
    assert item.origin == "house" and item.version == 1 and not item.audited
    # A house item of the same name takes precedence and continues the numbering.
    mine = lib.save_tool("double", "My double", [], {"description": "2x"}, TOOL_CODE, TOOL_TESTS)
    assert mine.origin == "house" and mine.version == 4 and mine.meta["based_on"] == "path-fixture-library v3"
    assert mine.shadows == ["path-fixture-library"]
    assert lib.tool_index()["double"]["path"] == "/opt/obs/tools/house/double/tool.py"
    assert set(lib.tool_mounts().values()) == {"/opt/obs/tools/house", "/opt/obs/tools/path-fixture-library"}


def test_versions_history_and_audit_by_content(tmp_path):
    lib = lib_at(tmp_path)
    lib.save_tool("triple", "Triples", [], {"description": "3x"}, TOOL_CODE, TOOL_TESTS)
    assert lib.set_audit("tools", "triple", True, note="read it").audited
    assert lib.set_audit("skills", "demo_recipe", True).audited          # library-folder items can be audited
    changed = lib.save_tool("triple", "Triples", [], {"description": "3x"}, TOOL_CODE + "# changed\n", TOOL_TESTS)
    assert changed.version == 2 and not changed.audited                  # any change voids the audit
    assert [h["version"] for h in lib.history("tools", "triple")] == ["v1"]
    assert lib.get("skills", "demo_recipe").audited
    lib.delete("tools", "triple")
    assert [h["version"] for h in lib.history("tools", "triple")] == ["v2", "v1"]
    with pytest.raises(PermissionError):
        lib.delete("tools", "double")                                     # library folders are not edited here


def test_export_import_round_trip_keeps_versions_and_audits(tmp_path):
    lib = lib_at(tmp_path)
    lib.save_tool("triple", "Triples", [], {"description": "3x"}, TOOL_CODE, TOOL_TESTS)
    lib.save_skill("my_recipe", "Mine", "a recipe", "1. do it", ["triple"])
    lib.set_audit("tools", "triple", True)
    data = lib.export(None, "house-pack", "0.1.0")                       # all house items
    other = Library(str(tmp_path / "other"))
    other._save_state("audits.yaml", lib._state("audits.yaml"))          # the same owner's audit list
    r = other.import_archive(data)
    assert r == {"pack": "house-pack", "version": "0.1.0", "items": ["skills/my_recipe", "tools/triple"]}
    t = other.get("tools", "triple")
    assert t.origin == "pack" and t.source.version == "0.1.0" and t.version == 1
    assert t.audited                                                      # same content, same audit
    # A newer version of the pack replaces it; the previous one is kept.
    lib.save_tool("triple", "Triples", [], {"description": "3x"}, TOOL_CODE + "# v2\n", TOOL_TESTS)
    other.import_archive(lib.export([("tools", "triple")], "house-pack", "0.2.0"))
    assert other.get("tools", "triple").version == 2 and not other.get("tools", "triple").audited
    assert os.path.isdir(tmp_path / "other" / "packs" / ".previous" / "house-pack" / "0.1.0")
    with pytest.raises(KeyError):
        other.get("skills", "my_recipe")                                  # not in the new pack version
    other.remove_pack("house-pack")
    assert other.items("tools") == {}


def test_import_rejects_unsafe_archives(tmp_path):
    import io
    import tarfile

    def archive(entries):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data, kind in entries:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                else:
                    info.size = len(data)
                tar.addfile(info, io.BytesIO(data) if kind != "link" else None)
        return buf.getvalue()

    lib = Library(str(tmp_path / "lib"))
    manifest = ("library.yaml", b"name: p\nversion: 1\n", "file")
    for bad, why in ((archive([manifest, ("../evil.py", b"x", "file")]), "unsafe"),
                     (archive([manifest, ("tools/x/../../e.py", b"x", "file")]), "unsafe"),
                     (archive([manifest, ("tools/ok_name/run.sh", b"x", "file")]), "expected"),
                     (archive([manifest, ("tools/ok_name/tool.py", b"", "link")]), "regular"),
                     (archive([("tools/ok_name/tool.py", b"x", "file")]), "library.yaml"),
                     (b"not an archive", "not a tar")):
        with pytest.raises(ValueError, match=why):
            lib.import_archive(bad)
    assert lib.items("tools") == {}


def test_core_library_tools_for_the_agent(tmp_path):
    lib = lib_at(tmp_path)
    tb = Toolbox(FakeService(), FakeData(), None, str(tmp_path / "ws"), library=lib, author="run x")
    listed = tb.call("list_library", {"kind": "skills"})
    assert any(i["name"] == "demo_recipe" for i in listed["items"])
    tb.call("read_library_item", {"kind": "skills", "name": "demo_recipe"})
    assert tb.skills_read == {"demo_recipe": False}
    out = tb.call("save_skill", {"name": "slow_start", "title": "Slow start", "description": "d", "recipe": "1. x"})
    assert out["audited"] is False and lib.get("skills", "slow_start").meta["author"] == "run x"
    lib.set_audit("skills", "slow_start", True)
    assert "audited" in tb.call("save_skill", {"name": "slow_start", "title": "t", "description": "d",
                                               "recipe": "2. y"})["error"]   # audited house items are not overwritten
    assert tb.call("data_coverage", {"signals": ["ems-esp/boiler_data#heatingactive"]})["coverage"][
        "ems-esp/boiler_data#heatingactive"]["points"] == 12


def test_data_socket_round_trip(tmp_path, monkeypatch):
    lib = lib_at(tmp_path)
    tb = Toolbox(FakeService(), FakeData(), None, str(tmp_path / "ws"), library=lib)
    log = tmp_path / "queries.jsonl"
    with DataBroker(tb, lib, str(log)) as broker:
        monkeypatch.setenv("OBS_SOCKET", broker.path)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "observer", "sandbox_lib"))
        try:
            from obs import _client
            out = _client.call("series", signals=["heatpumpsn/controller/state/temp_mix"], start="-6h", every="raw")
            assert len(out["heatpumpsn/controller/state/temp_mix"]["v"]) == 150
            with pytest.raises(_client.ObsError, match="unknown signal ids"):
                _client.call("series", signals=['x") |> drop()'])
            with pytest.raises(_client.ObsError, match="use 'now'"):
                _client.call("series", signals=["heatpumpsn/controller/state/temp_mix"], start="-1h) |> yield(")
            assert "double" in _client.call("tools")
            _client.call("tool_used", name="double")
            assert _client.call("find", circuit="floor")["count"] == 1
        finally:
            sys.path.pop(0)
        assert broker.tools_used == {"double": False}
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["op"] for x in lines] == ["series", "tool_used", "find"] and lines[0]["values"] == 150
    assert not os.path.exists(broker.path)


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
    skill = lib_at(tmp_path).get("skills", "demo_recipe")
    meta = execute(Run(str(folder)), llm, tb, "Does the boiler short-cycle?", skill)
    assert meta["status"] == "done" and meta["steps"] == 3 and meta["usage"]["prompt_tokens"] == 30
    assert (folder / "report.md").read_text().startswith("# Report")
    steps = Run(str(folder)).steps()
    assert [s["tool"] for s in steps if s["kind"] == "tool"] == ["house_context", "signal_features", "get_events",
                                                                  "finish"]
    # The tool results went back to the model, the recipe was in the prompt.
    assert "Recipe:" in llm.seen[0][0][1]["content"] and "NOT audited" in llm.seen[0][0][1]["content"]
    assert meta["library_used"]["skills"] == {"demo_recipe": False}
    assert any(m.get("role") == "tool" and m["tool_call_id"] == "c1" for m in llm.seen[2][0])


def test_agent_loop_forces_finish_at_the_step_limit(tmp_path):
    llm = ScriptedLlm([[("list_files", {})], [("finish", {"report_md": "partial"})]])
    folder = tmp_path / "run"
    folder.mkdir()
    meta = execute(Run(str(folder)), llm, Toolbox(FakeService(), FakeData(), None, str(folder)), "q", max_steps=2)
    assert meta["status"] == "done" and llm.seen[1][1] == ["finish"]


def _bwrap_works() -> bool:
    import subprocess
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        return False
    probe = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
                            "--symlink", "usr/lib", "/lib", "/usr/bin/true"], capture_output=True)
    return probe.returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bwrap missing or user namespaces not allowed")


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


@needs_bwrap
def test_tool_tests_and_data_socket_in_the_sandbox(tmp_path):
    venv = os.path.expanduser("~/.local/share/heatpumpsn-observer/sandbox/venv")
    if not os.path.exists(os.path.join(venv, "bin", "python")):
        pytest.skip("no sandbox environment (observer sandbox-setup)")
    lib = lib_at(tmp_path)
    sb = Sandbox(venv, limits=Limits(wall_s=60, memory_mb=600), tool_mounts=lib.tool_mounts)
    assert lib.run_tests("double", sb)["passed"]                           # the library folder's tool
    lib.save_tool("double", "Doubles", [], {"description": "2x"}, TOOL_CODE, TOOL_TESTS)
    assert lib.run_tests("double", sb)["passed"] and lib.get("tools", "double").tests_passed
    lib.save_tool("double", "Doubles", [], {"description": "2x"}, TOOL_CODE.replace("factor=2", "factor=3"), TOOL_TESTS)
    assert lib.get("tools", "double").tests_passed is None                 # new version: not tested yet
    assert not lib.run_tests("double", sb)["passed"]
    tb = Toolbox(FakeService(), FakeData(), sb, str(tmp_path / "ws"), library=lib)
    with DataBroker(tb, lib) as broker:
        tb.broker = broker
        r = tb.call("run_python", {"code": "from obs import data, tools\n"
                                           "df = data.series(['heatpumpsn/controller/state/temp_mix'], start='-6h', every='raw')\n"
                                           "print(len(df), tools.load('double').run(5))"})
    assert r["exit_code"] == 0, r["stderr"]
    assert r["stdout"].split() == ["150", "15"] and "NOT audited" in r["stderr"]


def test_harvest_proposes_and_adds_to_the_library(tmp_path):
    from observer.agent.manager import AgentManager

    class Settings:
        def client(self, task):
            return None

    class Svc(FakeService):
        settings = Settings()

    cfg = {"paths": {"runs": str(tmp_path / "runs"), "library": str(tmp_path / "lib"),
                     "sandbox_venv": str(tmp_path / "none")}, "agent": {"library_paths": [FIXTURE]}}
    m = AgentManager(Svc(), cfg)
    folder = tmp_path / "runs" / "20261102-010101-question"
    folder.mkdir(parents=True)
    run = Run(str(folder))
    run.save_meta({"status": "done", "question": "q"})
    run.add_step({"n": 1, "kind": "tool", "tool": "run_python", "args": {"code": "print(1)"},
                  "result": {"exit_code": 0, "stdout": "1"}})
    seen = []

    class Fake:
        model = "strong"

        def chat(self, system, user):
            seen.append(json.loads(user))
            return json.dumps({"tools": [{"name": "double", "purpose": "Doubles", "needs": "a number",
                                          "inputs": [{"name": "x", "type": "number"}],
                                          "outputs": {"type": "number", "description": "2x"},
                                          "code": TOOL_CODE, "tests": TOOL_TESTS, "why": "step 1"},
                                         {"name": "Bad Name", "code": "x"}],
                               "skills": [{"name": "doubling", "title": "Doubling", "description": "d",
                                           "recipe": "1. double", "tools": ["double"]}], "notes": "simple"})

    with pytest.raises(RuntimeError):
        m.harvest(run.id)                                              # no model for the task
    h = m.harvest(run.id, client=Fake())
    assert [t["name"] for t in h["tools"]] == ["double"] and h["skills"][0]["name"] == "doubling"
    assert seen[0]["run"]["steps"][0]["run_python"] == "print(1)"
    assert any(t["name"] == "double" for t in seen[0]["library"]["tools"])
    m.accept_harvest(run.id, "skills", 0)
    assert m.library.get("skills", "doubling").meta["author"] == f"harvest of run {run.id}"
    assert m.run(run.id)["harvest"]["skills"][0]["added"]
    with pytest.raises(KeyError):
        m.accept_harvest(run.id, "tools", 5)


def test_quota_pause_and_resume_with_another_model(tmp_path):
    import httpx

    class Quota(ScriptedLlm):
        def chat_tools(self, messages, tools):
            if len(self.seen) == 1:
                req = httpx.Request("POST", "http://proxy/v1/chat/completions")
                raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(
                    429, request=req, text='{"error": "quota exceeded, retry in 14 h"}'))
            return super().chat_tools(messages, tools)

    folder = tmp_path / "run"
    folder.mkdir()
    first = Quota([[("house_context", {})]])
    first.model = "small"
    tb = Toolbox(FakeService(), FakeData(), None, str(folder))
    meta = execute(Run(str(folder)), first, tb, "q", max_steps=5)
    assert meta["status"] == "paused" and "retry in 14 h" in meta["error"] and meta["steps"] == 1
    assert (folder / "state.json").exists()

    second = ScriptedLlm([[("finish", {"report_md": "done after the pause"})]])
    second.model = "big"
    tb2 = Toolbox(FakeService(), FakeData(), None, str(folder))
    meta = execute(Run(str(folder)), second, tb2, "q", max_steps=5, resume=True)
    assert meta["status"] == "done" and meta["models"] == ["small", "big"] and meta["steps"] == 2
    history = second.seen[0][0]
    assert [m["role"] for m in history[:4]] == ["system", "user", "assistant", "tool"]   # the work so far was kept
    assert (folder / "report.md").read_text().startswith("done after the pause")
    kinds = [s["kind"] for s in Run(str(folder)).steps()]
    assert "pause" in kinds and "resume" in kinds
