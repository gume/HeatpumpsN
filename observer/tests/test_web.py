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
                     "proposals": str(tmp_path / "proposals.yaml")}, "context": "TF top floor"}
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
