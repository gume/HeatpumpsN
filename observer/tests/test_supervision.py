import json
import time
from datetime import datetime

import pytest
import yaml

from observer.agent.data import Series
from observer.payload import parse
from observer.supervision import AlertStore, Cron, Supervisor, duration_s


def test_cron_and_durations():
    c = Cron("17 6 * * 1-5")
    assert c.matches(datetime(2026, 11, 2, 6, 17))          # a Monday
    assert not c.matches(datetime(2026, 11, 1, 6, 17))      # Sunday
    assert Cron("*/15 * * * *").matches(datetime(2026, 1, 1, 3, 45))
    assert Cron("0 0 * * 7").matches(datetime(2026, 11, 1, 0, 0))   # 7 = Sunday
    with pytest.raises(ValueError):
        Cron("61 * * * *")
    assert duration_s("5m") == 300 and duration_s("2h") == 7200 and duration_s(30) == 30


class Mqtt:
    def __init__(self):
        self.sent = {}

    def publish(self, topic, payload, retain=False):
        self.sent[topic] = payload


class Svc:
    def __init__(self, rows=(), data=None):
        self.rows = list(rows)
        self.mqtt = Mqtt()
        self.agents = type("A", (), {"data": data})()

    def signals(self):
        return self.rows


def row(sid, value=20.0, age=10, role="room", quantity="temperature", source=None, name=None):
    return {"id": sid, "source": source or sid.split("/")[0], "store": True, "quantity": quantity,
            "info": {"role": role, "name": name or sid}, "meta": {}, "value": value, "age_s": age, "unit": "°C"}


def make(tmp_path, checks, svc):
    (tmp_path / "checks.yaml").write_text(yaml.safe_dump({"checks": checks}))
    cfg = {"paths": {"catalog": str(tmp_path / "catalog.json"), "checks": str(tmp_path / "checks.yaml")},
           "config_path": str(tmp_path / "config.yaml")}
    return Supervisor(svc, cfg)


EVENT = json.dumps({"code": 1340, "severity": "error", "detail": "MPC anomaly: overshoot"}).encode()


def test_event_checks_group_by_subject_and_clear(tmp_path):
    sup = make(tmp_path, [
        {"id": "ctl", "type": "event", "signal": "heatpumpsn/controller/log/event#severity", "when": {"equals": "error"},
         "subject": "#code", "detail": "#detail", "title": "Controller error {subject}: {detail}",
         "severity": "alarm", "clear_after": "1s"},
        {"id": "ems", "type": "event", "signal": "ems-esp/status", "when": {"equals": 0}, "clear_when": {"equals": 1},
         "title": "EMS-ESP offline", "severity": "alarm"}], Svc())
    topic = "heatpumpsn/controller/log/event"
    for _ in range(3):
        sup.on_message(topic, parse(topic, EVENT), False, 0)
    sup.on_message(topic, parse(topic, EVENT), True, 0)               # a retained replay is ignored
    alerts = sup.store.view()
    assert len(alerts) == 1 and alerts[0]["count"] == 3
    assert alerts[0]["title"] == "Controller error 1340: MPC anomaly: overshoot" and alerts[0]["severity"] == "alarm"
    sup.on_message("ems-esp/status", parse("ems-esp/status", b"offline"), False, 0)
    assert sup.summary() == {"open": 2, "alarms": 2, "warnings": 0}
    sup.on_message("ems-esp/status", parse("ems-esp/status", b"online"), False, 0)
    assert sup.summary()["open"] == 1
    time.sleep(1.1)
    sup.tick()                                                         # clear_after passed
    assert sup.summary()["open"] == 0
    published = json.loads(sup.service.mqtt.sent["observer/alerts"])
    assert published["open"] == 0


def test_stale_range_and_ack(tmp_path):
    rows = [row("heatpumpsn/controller/state/temp_mix", age=400, role="supply", source="heatpumpsn"),
            row("heatpumpsn/controller/state/temp_boiler", age=500, role="boiler", source="heatpumpsn"),
            row("ha_state/sensor/lab/state", value=8.5, age=60, name="Lab"),
            row("ha_state/sensor/attic/state", value=12.0, age=30000, name="Attic")]
    svc = Svc(rows)
    sup = make(tmp_path, [
        {"id": "ctl_silent", "type": "stale", "every": "1m", "select": {"source": "heatpumpsn"}, "mode": "source",
         "max_age": "5m", "title": "No data for {age}", "severity": "alarm"},
        {"id": "room_silent", "type": "stale", "every": "15m", "select": {"role": "room"}, "mode": "each",
         "max_age": "6h", "title": "{name}: no reading for {age}"},
        {"id": "room_range", "type": "range", "every": "10m", "select": {"role": "room"}, "min": 10, "max": 32,
         "title": "{name}: {value}"}], svc)
    sup.tick()
    titles = sorted(a["title"] for a in sup.store.view())
    assert titles == ["Attic: no reading for 8.3 h", "Lab: 8.5", "No data for 7 min"]
    assert sup.state["room_range"]["result"] == "1 of 2 outside"    # the stale attic value is not judged
    lab = next(a for a in sup.store.view() if a["title"].startswith("Lab"))
    assert sup.store.acknowledge(lab["key"]) and sup.summary()["open"] == 3
    rows[0]["age_s"] = 5
    rows[2]["value"] = 19.0
    sup.run_now("ctl_silent")
    sup.run_now("room_range")
    assert sorted(a["state"] for a in sup.store.view()) == ["open", "resolved", "resolved"]
    with pytest.raises(KeyError):
        sup.run_now("nope")


def test_stuck_from_the_database(tmp_path):
    t = [datetime(2026, 11, 2, h) for h in range(8)]

    class Data:
        def series(self, ids, start, stop, every, limit=0):
            return {"a/t": Series("a/t", t, [21.0] * 8), "b/t": Series("b/t", t, [21.0] * 7 + [21.5])}

    sup = make(tmp_path, [{"id": "stuck", "type": "stuck", "every": "1h", "select": {"quantity": "temperature"},
                           "hours": 8, "title": "{name}: unchanged ({value}) for {hours} h"}],
               Svc([row("a/t", name="A"), row("b/t", name="B")], Data()))
    sup.tick()
    assert [a["title"] for a in sup.store.view()] == ["A: unchanged (21) for 8 h"]


def test_agent_findings_become_alerts(tmp_path):
    class Agents:
        data = None

        def __init__(self):
            self.status = "running"

        def start(self, skill, question):
            return "20261102-061700-sensor_health"

        def run(self, run_id):
            return {"meta": {"status": self.status, "findings": [
                {"title": "Bathroom sensor stale", "severity": "warning", "evidence": "gap 7 h"},
                {"title": "all fine elsewhere", "severity": "info"}]}}

    svc = Svc()
    svc.agents = Agents()
    sup = make(tmp_path, [{"id": "daily", "type": "agent", "cron": "* * * * *", "skill": "sensor_health"}], svc)
    sup.tick()
    assert sup._agent_runs == {"daily": "20261102-061700-sensor_health"}
    sup.tick()                                                         # still running: nothing yet
    assert sup.store.view() == []
    svc.agents.status = "done"
    sup.tick()
    assert [a["title"] for a in sup.store.view()] == ["Bathroom sensor stale (run 20261102-061700-sensor_health)"]


def test_validation_discovery_and_persistence(tmp_path):
    sup = make(tmp_path, [{"id": "x", "type": "range", "every": "1m", "max": 1}], Svc())
    with pytest.raises(ValueError, match="duplicate"):
        sup.save_text(yaml.safe_dump({"checks": [{"id": "a", "type": "range", "every": "1m"}] * 2}))
    with pytest.raises(ValueError, match="needs signal and when"):
        sup.save_text(yaml.safe_dump({"checks": [{"id": "a", "type": "event"}]}))
    with pytest.raises(ValueError):
        sup.save_text(yaml.safe_dump({"checks": [{"id": "a", "type": "stale", "cron": "bad"}]}))
    assert [c["id"] for c in sup.checks] == ["x"]                      # a rejected save changes nothing
    sup.publish_discovery()
    sent = sup.service.mqtt.sent
    assert sent["observer/status"] == "online"
    cfg = json.loads(sent["homeassistant/binary_sensor/heatpumpsn_observer/problem/config"])
    assert cfg["device_class"] == "problem" and cfg["availability_topic"] == "observer/status"
    store = AlertStore(str(tmp_path / "a.json"))
    store.raise_("k", "c", "warning", "t")
    assert AlertStore(str(tmp_path / "a.json")).view()[0]["title"] == "t"   # survives a restart


def test_default_checks_are_valid():
    from observer.supervision import DEFAULTS

    checks = Supervisor.validate(yaml.safe_load(open(DEFAULTS))["checks"])
    assert {c["type"] for c in checks} == {"event", "stale", "range", "stuck", "agent", "identify"}


def test_collector_alarm(tmp_path):
    svc = Svc()
    svc.writer_kind = "external"
    st = {"updated": time.time(), "connected": True}
    svc.collector_status = lambda: st
    sup = make(tmp_path, [], svc)
    sup.tick()
    assert sup.summary()["open"] == 0
    st["connected"] = False
    sup.tick()
    assert "lost the MQTT broker" in sup.store.view()[0]["title"]
    st.update(connected=True, updated=time.time() - 200)
    sup.tick()
    assert "not running" in sup.store.view()[0]["title"] and sup.summary()["alarms"] == 1
    st["updated"] = time.time()
    sup.tick()
    assert sup.summary()["open"] == 0
    svc.writer_kind = "influx"                          # all-in-one: no collector to watch
    st["updated"] = 0
    sup.tick()
    assert sup.summary()["open"] == 0
