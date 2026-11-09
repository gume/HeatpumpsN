import json

import yaml
from fastapi.testclient import TestClient

from observer.service import Service
from observer.store import MemoryWriter
from observer.web import create_app


def make(tmp_path):
    (tmp_path / "registry.yaml").write_text(yaml.safe_dump({
        "rules": [{"match": "ems-esp/boiler_data#*", "role": "boiler"}], "signals": {}}))
    cfg = {"paths": {"catalog": str(tmp_path / "catalog.json"), "registry": str(tmp_path / "registry.yaml"),
                     "proposals": str(tmp_path / "proposals.yaml"),
                     "house_dir": str(tmp_path / "house"),
                     "settings": str(tmp_path / "settings.yaml")},
           "context": "TF top floor"}
    service = Service(cfg, MemoryWriter(), "dry-run")
    service._handle("ems-esp/boiler_data", json.dumps({"curflowtemp": 57.2}).encode(), False)
    service._handle("zigbee2mqtt/radar", json.dumps({"humidity": 51}).encode(), False)
    return service, TestClient(create_app(service))


def test_status_and_signals(tmp_path):
    service, client = make(tmp_path)
    status = client.get("/api/status").json()
    assert status["signals"] == {"total": 2, "stored": 1, "unregistered": 1}
    assert status["storage"] == {"kind": "dry-run", "samples": 1, "total": None, "counted": None, "collector": None}
    rows = {r["id"]: r for r in client.get("/api/signals").json()}
    assert rows["ems-esp/boiler_data#curflowtemp"]["resolved_by"].startswith("rule:")
    assert rows["zigbee2mqtt/radar#humidity"]["store"] is False
    assert client.get("/").status_code == 200


def test_edit_signal_takes_effect_and_persists(tmp_path):
    service, client = make(tmp_path)
    sid = "zigbee2mqtt/radar#humidity"
    r = client.put("/api/signal", params={"id": sid},
                   json={"name": "Radar BF humidity", "quantity": "humidity", "location": "BF", "store": True})
    assert r.status_code == 200 and r.json()["store"] is True
    service._handle("zigbee2mqtt/radar", json.dumps({"humidity": 52}).encode(), False)
    assert service.writer.points[-1].signal == sid                   # stored from now on
    saved = yaml.safe_load((tmp_path / "registry.yaml").read_text())
    assert saved["signals"][sid]["name"] == "Radar BF humidity"
    assert client.delete("/api/signal", params={"id": sid}).status_code == 200
    assert client.delete("/api/signal", params={"id": sid}).status_code == 404


def test_proposals_flow(tmp_path):
    service, client = make(tmp_path)
    assert client.post("/api/proposals/run", json={"limit": 5}).status_code == 400   # no LLM configured

    class Fake:
        def chat(self, system, user):
            return json.dumps({"proposals": [{"signal": "zigbee2mqtt/radar#humidity", "name": "Humidity",
                                              "quantity": "humidity", "store": True, "confidence": 0.9}]})

    assert service.run_proposals(5, client=Fake()) == 1
    assert client.get("/api/proposals").json()[0]["name"] == "Humidity"
    r = client.post("/api/proposal/accept", params={"id": "zigbee2mqtt/radar#humidity"}, json={"location": "BF"})
    assert r.json()["info"]["location"] == "BF" and r.json()["store"] is True
    assert client.get("/api/proposals").json() == []
    assert client.delete("/api/proposal", params={"id": "nope"}).status_code == 404


def test_bulk_update_keeps_other_fields(tmp_path):
    service, client = make(tmp_path)
    sid = "zigbee2mqtt/radar#humidity"
    client.put("/api/signal", params={"id": sid}, json={"name": "Radar", "description": "Mine", "store": False})
    r = client.put("/api/signals", json={"ids": [sid, "ems-esp/boiler_data#curflowtemp"],
                                         "set": {"store": True, "location": "BF"}})
    assert r.json() == {"updated": 2}
    saved = yaml.safe_load((tmp_path / "registry.yaml").read_text())["signals"]
    assert saved[sid] == {"name": "Radar", "description": "Mine", "store": True, "location": "BF"}
    assert saved["ems-esp/boiler_data#curflowtemp"]["location"] == "BF"
    rows = {x["id"]: x for x in client.get("/api/signals").json()}
    assert rows[sid]["store"] is True and rows["ems-esp/boiler_data#curflowtemp"]["store"] is True
    assert client.put("/api/signals", json={"ids": []}).status_code == 400


def test_effective_unit_and_quantity_come_from_metadata(tmp_path):
    service, client = make(tmp_path)
    service._handle("homeassistant/sensor/r/humidity/config", json.dumps({
        "name": "Humidity", "state_topic": "zigbee2mqtt/radar", "value_template": "{{ value_json.humidity }}",
        "unit_of_measurement": "%", "device_class": "humidity", "device": {"name": "Radar BF"}}).encode(), False)
    row = next(x for x in client.get("/api/signals").json() if x["id"] == "zigbee2mqtt/radar#humidity")
    assert (row["unit"], row["quantity"], row["device"]) == ("%", "humidity", "Radar BF")


def test_polish_uses_draft_and_validates(tmp_path):
    service, client = make(tmp_path)
    seen = {}

    class Fake:
        def chat(self, system, user):
            seen.update(json.loads(user))
            return json.dumps({"name": "Radar BF humidity", "quantity": "humidity", "unit": "%", "location": "BF",
                               "circuit": "nonsense", "role": "room", "description": "Relative humidity in the lab.",
                               "store": True, "notes": "Added the unit."})

    out = service.polish("zigbee2mqtt/radar#humidity", {"description": "lab radar humidity"}, client=Fake())
    assert seen["user_draft"] == {"description": "lab radar humidity"} and seen["house_context"] == "TF top floor"
    assert out["name"] == "Radar BF humidity" and "circuit" not in out and out["notes"] == "Added the unit."
    assert client.post("/api/signal/polish", params={"id": "x"}, json={}).status_code == 400   # no LLM configured


def test_settings_providers_and_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVER_LLM_BASE_URL", "http://proxy:4000/v1")
    monkeypatch.setenv("OBSERVER_LLM_KEY", "secret")
    monkeypatch.setenv("OBSERVER_LLM_2_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.delenv("OBSERVER_LLM_MODEL", raising=False)
    service, client = make(tmp_path)
    view = client.get("/api/settings").json()
    assert [p["slot"] for p in view["providers"]] == ["1", "2"]
    assert view["providers"][0]["has_key"] is True and "secret" not in json.dumps(view)
    assert all(not t["ready"] for t in view["tasks"])
    r = client.put("/api/settings", json={"providers": {"1": {"name": "Gemini"}, "9": {"name": "nope"}},
                                          "tasks": {"house": {"provider": "1", "model": "gemini-2.5-pro"},
                                                    "describe": {"provider": "2", "model": "qwen2.5"}}}).json()
    assert r["providers"][0]["name"] == "Gemini"
    ready = {t["task"]: t["ready"] for t in r["tasks"]}
    assert ready == {"describe": True, "propose": False, "house": True, "agent": False, "harvest": False}
    house_client = service.settings.client("house")
    assert (house_client.base_url, house_client.model, house_client.api_key) == ("http://proxy:4000/v1", "gemini-2.5-pro", "secret")
    assert service.settings.client("describe").api_key is None
    assert client.get("/api/status").json()["llm"] == ready
    assert "gemini-2.5-pro" in (tmp_path / "settings.yaml").read_text()


def test_house_description_update_clear_and_undo(tmp_path):
    service, client = make(tmp_path)
    client.put("/api/signal", params={"id": "ems-esp/boiler_data#curflowtemp"},
               json={"name": "Boiler flow", "quantity": "temperature", "store": True})
    service._handle("zigbee2mqtt/radar", json.dumps({"humidity": 53}).encode(), False)   # candidates need 2 messages
    assert client.post("/api/house/update").status_code == 400               # no LLM configured

    client.put("/api/house/input", json={"text": "The bottom floor has floor heating only."})
    seen = {}

    class Fake:
        model = "fake-model"

        def chat(self, system, user):
            seen.update(json.loads(user))
            return json.dumps({
                "description_md": "## Overview\nA gas boiler feeds three circuits: `ems-esp/boiler_data#curflowtemp`, `ems-esp/boiler_data#curflowtmp`.",
                "notes": "Added the bottom floor.",
                "model": {"systems": [{"name": "Boiler", "description": "", "signals": ["ems-esp/boiler_data#curflowtemp"]}]}})

    out = service.update_house(client=Fake())
    assert seen["user_input"] == "The bottom floor has floor heating only."
    assert seen["current_description"] == "" and seen["previous_description"] == ""
    assert "ems-esp/boiler_data#curflowtemp" in seen["described_signals"]
    assert "zigbee2mqtt/radar#humidity" in seen["candidate_signals"]
    assert out["notes"] == "Added the bottom floor."
    assert out["unknown_ids"] == ["ems-esp/boiler_data#curflowtmp"]          # typo in the prose flagged
    h = client.get("/api/house").json()
    assert h["description"].startswith("## Overview")
    assert h["user_input"].startswith("The bottom floor")
    assert h["can_undo"] is True and h["previous"] == ""
    assert h["model"]["systems"][0]["name"] == "Boiler"
    assert h["unknown_ids"] == ["ems-esp/boiler_data#curflowtmp"]
    for name in ("description.md", "input.md", "previous.md", "model.yaml"):
        assert (tmp_path / "house" / name).exists()

    # The next update sees the first draft as current and as the previous version.
    seen.clear()

    class Fake2(Fake):
        def chat(self, system, user):
            seen.update(json.loads(user))
            return json.dumps({"description_md": "## Overview\nThe radiators are back.",
                               "notes": "Restored the radiators.", "model": {}})

    service.update_house(client=Fake2())
    assert seen["current_description"].startswith("## Overview")
    assert "A gas boiler" in seen["previous_description"] or seen["previous_description"] == ""
    h = client.get("/api/house").json()
    assert h["description"].startswith("## Overview\nThe radiators are back.")
    assert h["previous"].startswith("## Overview\nA gas boiler")

    assert client.post("/api/house/undo").status_code == 200
    h = client.get("/api/house").json()
    assert h["description"].startswith("## Overview\nA gas boiler")

    r = client.post("/api/house/clear").json()
    assert r["description"] == ""
    h = client.get("/api/house").json()
    assert h["description"] == "" and h["user_input"].startswith("The bottom floor")
    assert h["previous"].startswith("## Overview\nA gas boiler")

    assert client.post("/api/house/undo").status_code == 200
    assert client.get("/api/house").json()["description"].startswith("## Overview\nA gas boiler")


def test_llm_unavailable_answers_503_with_details(tmp_path):
    from observer.llm import LlmUnavailable

    service, client = make(tmp_path)

    class Busy:
        model = "gemini-x"

        def chat(self, system, user):
            raise LlmUnavailable("quota", "requests per day exceeded", "gemini-x", "house", 3600, 429)

    service.settings.client = lambda task: Busy()
    r = client.post("/api/house/update", json={"text": "anything"})
    assert r.status_code == 503
    body = r.json()
    assert body["llm"] == {"kind": "quota", "message": "requests per day exceeded", "model": "gemini-x",
                           "task": "house", "retry_after_s": 3600, "status": 429}
    assert "retry in 60 min" in body["detail"]


def test_response_limit_settings(tmp_path):
    service, client = make(tmp_path)
    assert client.get("/api/settings").json()["response"] == {"timeout_s": 120, "stream": True}
    assert client.put("/api/settings", json={"response": {"timeout_s": 5}}).status_code == 400
    r = client.put("/api/settings", json={"response": {"timeout_s": 300, "stream": False}}).json()
    assert r["response"] == {"timeout_s": 300, "stream": False}
    from observer.settings import Settings
    assert Settings(str(tmp_path / "settings.yaml")).data["response"]["timeout_s"] == 300
