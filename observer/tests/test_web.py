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
                     "house_model": str(tmp_path / "house_model.yaml"),
                     "house_model_proposal": str(tmp_path / "house_model_proposal.yaml")},
           "context": "TF top floor"}
    service = Service(cfg, MemoryWriter(), "dry-run")
    service._handle("ems-esp/boiler_data", json.dumps({"curflowtemp": 57.2}).encode(), False)
    service._handle("zigbee2mqtt/radar", json.dumps({"humidity": 51}).encode(), False)
    return service, TestClient(create_app(service))


def test_status_and_signals(tmp_path):
    service, client = make(tmp_path)
    status = client.get("/api/status").json()
    assert status["signals"] == {"total": 2, "stored": 1, "unregistered": 1}
    assert status["storage"] == {"kind": "dry-run", "samples": 1}
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


def test_house_model_generate_accept_edit(tmp_path):
    service, client = make(tmp_path)
    client.put("/api/signal", params={"id": "ems-esp/boiler_data#curflowtemp"},
               json={"name": "Boiler flow", "quantity": "temperature", "store": True})
    client.put("/api/signal", params={"id": "zigbee2mqtt/radar#humidity"},
               json={"name": "Radar humidity", "store": True})
    assert set(service.model_inputs()) == {"ems-esp/boiler_data#curflowtemp", "zigbee2mqtt/radar#humidity"}

    class Fake:
        def chat(self, system, user):
            return json.dumps({
                "systems": [{"name": "Boiler", "description": "Gas boiler", "signals": ["ems-esp/boiler_data#curflowtemp", "bogus"]}],
                "points": [{"signal": "ems-esp/boiler_data#curflowtemp", "measures": "flow", "use": "load"}],
                "relations": [{"type": "redundant", "signals": ["ems-esp/boiler_data#curflowtemp", "zigbee2mqtt/radar#humidity"],
                               "condition": "x", "expectation": "y", "why": "z"},
                              {"type": "magic", "signals": ["a", "b"]},
                              {"type": "order", "signals": ["ems-esp/boiler_data#curflowtemp"]}],
                "checks": [{"name": "stale", "signals": ["ems-esp/boiler_data#curflowtemp"], "condition": "no data 10 min",
                            "severity": "extreme"}],
                "gaps": ["no outdoor temperature"]})

    model = service.generate_model(client=Fake())
    assert model["systems"][0]["signals"] == ["ems-esp/boiler_data#curflowtemp"]     # unknown id dropped
    assert len(model["relations"]) == 1 and model["checks"][0]["severity"] == "warning"
    m = client.get("/api/model").json()
    assert m["current"] is None and m["proposal"]["gaps"] == ["no outdoor temperature"]
    assert client.post("/api/model/accept").status_code == 200
    m = client.get("/api/model").json()
    assert m["proposal"] is None and m["current"]["systems"][0]["name"] == "Boiler"
    assert client.post("/api/model/accept").status_code == 404
    assert client.put("/api/model", json={"text": "systems: [\n"}).status_code == 400   # invalid YAML refused
    assert client.put("/api/model", json={"text": "systems: []\n"}).status_code == 200
