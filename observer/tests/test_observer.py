import json

from observer import metadata
from observer.catalog import Catalog
from observer.ingest import Ingest
from observer.llm import parse_answer, propose
from observer.payload import normalise, parse
from observer.registry import Registry
from observer.store import MemoryWriter

BOILER = json.dumps({"heatingactive": "off", "curflowtemp": 57.2, "burnstarts": 14300,
                     "maintenancemessage": "H00", "reset": "",
                     "dhw": {"curtemp": 59.2, "charging": "off"}}).encode()
SENSORS = json.dumps({"28_FF64_1E9F_9836": {"name": "28_FF64_1E9F_9836", "temp": 19.1}}).encode()


def test_normalise_values():
    assert normalise("21.5") == (21.5, None)
    assert normalise("ON") == (1.0, None)
    assert normalise("off") == (0.0, None)
    assert normalise(True) == (1.0, None)
    assert normalise("H00") == (None, "H00")
    assert normalise("unknown") is None and normalise("") is None


def test_parse_scalar_and_nested_json():
    assert parse("heatpumpsn/controller/state/temp_mix", b"17.00")[0].value == 17.0
    samples = {s.signal: s for s in parse("ems-esp/boiler_data", BOILER)}
    assert samples["ems-esp/boiler_data#curflowtemp"].value == 57.2
    assert samples["ems-esp/boiler_data#dhw.curtemp"].value == 59.2
    assert samples["ems-esp/boiler_data#dhw.charging"].value == 0.0
    assert samples["ems-esp/boiler_data#maintenancemessage"].text == "H00"
    assert "ems-esp/boiler_data#reset" not in samples          # empty values carry nothing
    assert parse("ems-esp/temperaturesensor_data", SENSORS)[1].signal == \
        "ems-esp/temperaturesensor_data#28_FF64_1E9F_9836.temp"


def test_parse_skips_non_measurements():
    for topic in ("homeassistant/sensor/x/y/config", "zigbee2mqtt/bridge/devices",
                  "mixermeter/debug", "zigbee2mqtt/lamp/set", "ems-esp/info", "esphome/discover"):
        assert parse(topic, b"1") == []


def test_metadata_from_discovery_and_z2m():
    cfg = {"name": "Humidity", "state_topic": "zigbee2mqtt/0xa4c1", "unit_of_measurement": "%",
           "value_template": "{{ value_json.humidity }}", "device_class": "humidity",
           "device": {"name": "Radar BF", "model": "ZG-204ZV"}}
    meta = metadata.from_discovery("homeassistant/sensor/0xa4c1/humidity/config", json.dumps(cfg).encode())
    assert meta == {"zigbee2mqtt/0xa4c1#humidity": {"name": "Humidity", "unit": "%", "device_class": "humidity",
                                                    "component": "sensor", "device": "Radar BF",
                                                    "device_model": "ZG-204ZV"}}
    devices = [{"type": "Coordinator"},
               {"type": "EndDevice", "friendly_name": "door", "ieee_address": "0x01",
                "definition": {"vendor": "Tuya", "model": "DS04", "description": "Door sensor",
                               "exposes": [{"type": "binary", "property": "contact", "label": "Contact"},
                                           {"type": "composite", "features": [{"property": "battery",
                                                                               "unit": "%"}]}]}}]
    z2m = metadata.from_z2m_devices(json.dumps(devices).encode())
    assert z2m["zigbee2mqtt/door#contact"]["device_model"] == "Tuya DS04"
    assert z2m["zigbee2mqtt/door#battery"]["unit"] == "%"
    assert metadata.from_statestream("ha_state/sensor/bth01_6f31_temperature/state", "ha_state") == \
        {"ha_state/sensor/bth01_6f31_temperature/state": {"ha_entity": "sensor.bth01_6f31_temperature",
                                                          "component": "sensor"}}


def test_catalog_new_signals_and_pending_meta(tmp_path):
    cat = Catalog(str(tmp_path / "c.json"))
    cat.add_meta({"a": {"name": "A"}})               # metadata before the first sample
    assert cat.observe("a", 1.0, None, now=10) is True
    assert cat.observe("a", 2.0, None, now=20) is False
    assert cat.entries["a"].meta == {"name": "A"} and cat.entries["a"].count == 2
    cat.observe("b", None, "H00", now=20)
    assert cat.entries["b"].numeric is False
    cat.save()
    again = Catalog(str(tmp_path / "c.json"))
    assert again.entries["a"].last_value == 2.0 and again.entries["a"].first_seen == 10


def registry():
    return Registry(data={
        "rules": [{"match": "ha_state/sensor/*_temperature/state", "quantity": "temperature", "unit": "°C",
                   "role": "room"},
                  {"match": "ems-esp/boiler_data#*", "role": "boiler"},
                  {"match": "zigbee2mqtt/*", "store": False}],
        "signals": {"ha_state/sensor/x_temperature/state": {"name": "Lab", "location": "BF"},
                    "ems-esp/boiler_data#burnstarts": {"name": "Burner starts", "store": False}}})


def test_registry_rules_and_entries_merge():
    reg = registry()
    lab = reg.resolve("ha_state/sensor/x_temperature/state")
    assert lab.store and lab.source == "signal"
    assert lab.info == {"name": "Lab", "location": "BF", "quantity": "temperature", "unit": "°C", "role": "room"}
    assert reg.resolve("ha_state/sensor/y_temperature/state").source.startswith("rule:")
    assert reg.resolve("ems-esp/boiler_data#burnstarts").store is False
    assert reg.resolve("zigbee2mqtt/lamp#state").store is False
    assert reg.resolve("something/else").source == "none"
    assert reg.tags("ha_state/sensor/x_temperature/state") == {"quantity": "temperature", "location": "BF",
                                                               "role": "room"}


def test_ingest_stores_selected_live_samples_only():
    writer = MemoryWriter()
    ingest = Ingest(Catalog(), registry(), writer, prefix="mirror/")
    ingest.handle("mirror/ems-esp/boiler_data", BOILER, time_ns=5)
    ingest.handle("mirror/ha_state/sensor/x_temperature/state", b"21.4", retained=True, time_ns=6)
    ingest.handle("mirror/zigbee2mqtt/lamp", b'{"state": "ON"}', time_ns=7)
    ingest.handle("other/topic", b"1", time_ns=8)                       # outside the prefix
    stored = {p.signal for p in writer.points}
    assert "ems-esp/boiler_data#curflowtemp" in stored
    assert "ems-esp/boiler_data#burnstarts" not in stored               # store: false
    assert "ha_state/sensor/x_temperature/state" not in stored          # retained replay
    assert not any(s.startswith("zigbee2mqtt") for s in stored)
    flow = next(p for p in writer.points if p.signal == "ems-esp/boiler_data#curflowtemp")
    assert flow.time_ns == 5 and flow.tags == {"source": "ems-esp", "role": "boiler"}
    # The catalog still knows every signal, retained and unstored ones included.
    assert "ha_state/sensor/x_temperature/state" in ingest.catalog.entries
    assert "zigbee2mqtt/lamp#state" in ingest.catalog.entries


def test_ingest_merges_discovery_metadata():
    ingest = Ingest(Catalog(), Registry(data={}), MemoryWriter())
    cfg = {"name": "Mixer position", "state_topic": "heatpumpsn/controller/state/mixer_position",
           "unit_of_measurement": "%"}
    ingest.handle("homeassistant/sensor/hpn/mixer_position/config", json.dumps(cfg).encode())
    ingest.handle("heatpumpsn/controller/state/mixer_position", b"12")
    assert ingest.catalog.entries["heatpumpsn/controller/state/mixer_position"].meta["unit"] == "%"


def test_llm_answers_are_validated():
    text = 'Sure: {"proposals": [' \
           '{"signal": "a", "name": "Radar BF humidity", "quantity": "humidity", "role": "room",' \
           ' "store": true, "confidence": 1.4},' \
           '{"signal": "b", "quantity": "bogus", "role": "pump", "store": "yes"},' \
           '{"signal": "not-asked", "name": "x"}]}'
    out = parse_answer(text, {"a", "b"})
    assert [p["signal"] for p in out] == ["a", "b"]
    assert out[0]["confidence"] == 1.0 and out[0]["quantity"] == "humidity"
    assert "quantity" not in out[1] and out[1]["role"] == "pump"
    assert parse_answer("no json here", {"a"}) == []


def test_propose_batches_and_passes_context():
    calls = []

    class Fake:
        def chat(self, system, user):
            calls.append(json.loads(user))
            wanted = list(calls[-1]["signals_to_describe"])
            return json.dumps({"proposals": [{"signal": s, "name": s.upper(), "store": False,
                                              "confidence": 0.5} for s in wanted]})

    signals = {f"s{i}": {"meta": {}} for i in range(5)}
    out = propose(Fake(), signals, "TF top floor", {"known": {"name": "K"}}, batch=2)
    assert len(calls) == 3 and len(out) == 5
    assert calls[0]["house_context"] == "TF top floor"


def test_lenient_json_and_one_repair_round():
    from observer.llm import chat_json, loads_lenient
    assert loads_lenient('```json\n{"a": [1, 2,], "b": {"c": 3,},}\n```') == {"a": [1, 2], "b": {"c": 3}}
    answers = iter(['{"a": 1, b: 2}', '{"a": 1, "b": 2}'])
    calls = []

    class Fake:
        def chat(self, system, user):
            calls.append(user)
            return next(answers)

    assert chat_json(Fake(), "sys", "question") == {"a": 1, "b": 2}
    assert len(calls) == 2 and "not valid JSON" in calls[1]
