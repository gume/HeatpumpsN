import base64
import json
import io
import os
import sys
import tarfile

import pytest
import yaml
from fastapi.testclient import TestClient

from observer.cli import apply_env_overrides, cmd_init, load_config
from observer.service import Service
from observer.state import export_state, import_state
from observer.store import MemoryWriter
from observer.web import create_app


def test_release_init_and_env_overrides(tmp_path, monkeypatch):
    cmd_init(str(tmp_path), "release")
    cfg_text = (tmp_path / "config.yaml").read_text()
    assert "mqtt.home" in cfg_text and "influxdb:8086" in cfg_text
    for var in ("OBSERVER_MQTT_HOST", "OBSERVER_MQTT_PORT", "OBSERVER_MQTT_TOPIC_PREFIX", "OBSERVER_INFLUX_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OBSERVER_MQTT_HOST", "broker.lan")
    monkeypatch.setenv("OBSERVER_MQTT_PORT", "1884")
    monkeypatch.setenv("OBSERVER_MQTT_TOPIC_PREFIX", "mirror/")
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg["mqtt"]["host"] == "broker.lan" and cfg["mqtt"]["port"] == 1884
    assert cfg["mqtt"]["topic_prefix"] == "mirror/" and cfg["mqtt"]["subscribe"] == ["mirror/#"]
    assert cfg["influx"]["url"] == "http://influxdb:8086"
    cfg2 = {"mqtt": {"topic_prefix": "x/"}}
    monkeypatch.setenv("OBSERVER_MQTT_TOPIC_PREFIX", "")
    apply_env_overrides(cfg2)
    assert cfg2["mqtt"]["subscribe"] == ["#"]                       # empty prefix: everything


def test_example_config_carries_no_house_description():
    from observer.cli import DEFAULTS

    for name in ("config.example.yaml", "config.release.yaml"):
        context = yaml.safe_load(open(os.path.join(DEFAULTS, name)))["context"]
        assert context.strip().startswith("(Describe the house")


def make_home(tmp_path, name):
    home = tmp_path / name
    cmd_init(str(home), "dev")
    return load_config(str(home / "config.yaml"))


def test_state_round_trip_with_backup(tmp_path):
    src = make_home(tmp_path, "a")
    with open(src["paths"]["registry"], "a") as fp:
        fp.write("\n# house a\n")
    os.makedirs(os.path.join(src["paths"]["library"], "skills", "mine"), exist_ok=True)
    open(os.path.join(src["paths"]["library"], "skills", "mine", "SKILL.md"), "w").write("---\nversion: 1\n---\nx")
    open(os.path.join(src["paths"]["house_dir"], "description.md"), "w").write("## Building\n- 15 cm insulation\n")
    src["context"] = "House A: three floors."
    data = export_state(src)
    dst = make_home(tmp_path, "b")
    r = import_state(dst, data, dst["config_path"])
    assert r["context"] and r["backup"]
    assert open(dst["paths"]["registry"]).read().endswith("# house a\n")
    assert os.path.exists(os.path.join(dst["paths"]["library"], "skills", "mine", "SKILL.md"))
    assert "15 cm" in open(os.path.join(dst["paths"]["house_dir"], "description.md")).read()
    assert load_config(dst["config_path"])["context"].startswith("House A")
    assert "mqtt" in open(dst["config_path"]).read()                 # the rest of config.yaml is kept
    assert os.path.exists(os.path.join(r["backup"], "registry.yaml"))


def test_state_import_rejects_foreign_archives(tmp_path):
    dst = make_home(tmp_path, "b")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("state.yaml", ".env"):
            data = b"format: heatpumpsn-observer-state/1\n" if name == "state.yaml" else b"TOKEN=x"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="not part of an observer state"):
        import_state(dst, buf.getvalue(), dst["config_path"])
    with pytest.raises(ValueError):
        import_state(dst, b"junk", dst["config_path"])


def test_portal_password(tmp_path, monkeypatch):
    cfg = make_home(tmp_path, "p")
    monkeypatch.setenv("OBSERVER_PORTAL_PASSWORD", "s3cret")
    client = TestClient(create_app(Service(cfg, MemoryWriter(), "dry-run")))
    assert client.get("/api/status").status_code == 401
    assert client.get("/healthz").status_code == 200
    auth = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
    assert client.get("/api/status", headers=auth("admin", "wrong")).status_code == 401
    assert client.get("/api/status", headers=auth("admin", "s3cret")).status_code == 200


def test_empty_environment_does_not_hide_the_env_file(tmp_path, monkeypatch):
    from observer.cli import load_env

    (tmp_path / ".env").write_text("OBSERVER_LLM_BASE_URL=http://proxy:4000/v1\nOBSERVER_LLM_KEY=k\n")
    monkeypatch.setenv("OBSERVER_LLM_BASE_URL", "")              # what compose passes when deploy/.env is silent
    monkeypatch.setenv("OBSERVER_LLM_KEY", "from-compose")
    load_env(str(tmp_path / ".env"))
    assert os.environ["OBSERVER_LLM_BASE_URL"] == "http://proxy:4000/v1"
    assert os.environ["OBSERVER_LLM_KEY"] == "from-compose"      # a real value from the environment still wins


# --- the collector's own image and the install-folder layout ---------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_collector_image_is_locked_and_self_contained():
    import re
    import subprocess

    from observer import collector

    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "collector_lock.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    for name in collector.FILES:                       # only imports among themselves
        text = open(os.path.join(ROOT, "observer", name), encoding="utf-8").read()
        mods = re.findall(r"^\s*from \.(\w+) import", text, re.M)
        mods += [m.strip() for line in re.findall(r"^\s*from \. import (.+)$", text, re.M) for m in line.split(",")]
        for mod in mods:                                   # (the image's package __init__ is empty)
            assert f"{mod}.py" in collector.FILES, f"{name} imports .{mod}: not in the collector image"
    docker = open(os.path.join(ROOT, "Dockerfile.collector"), encoding="utf-8").read()
    copied = set(re.findall(r"observer/(\w+\.py)", docker)) - {"__init__.py"}   # (created empty)
    assert copied == set(collector.FILES)


def test_compose_layout():
    import yaml as Y

    from observer import collector

    c = Y.safe_load(open(os.path.join(ROOT, "deploy", "docker-compose.yml"), encoding="utf-8"))
    col, obs = c["services"]["collector"], c["services"]["observer"]
    assert col["image"] == f"heatpumpsn-collector:{collector.COLLECTOR_VERSION}"
    assert col["build"]["context"] == "./app" and col["build"]["dockerfile"] == "Dockerfile.collector"
    lock = json.load(open(os.path.join(ROOT, "collector.lock"), encoding="utf-8"))
    assert col["build"]["args"]["COLLECTOR_DIGEST"] == lock["versions"][collector.COLLECTOR_VERSION]
    assert obs["build"]["context"] == "./app" and obs["image"].startswith("heatpumpsn-observer:")
    assert "env_file" not in col                       # unrelated .env edits must not recreate it
    assert col["volumes"] == ["./data:/data"] and obs["volumes"] == ["./data:/data"]
    assert set(c["volumes"]) == {"influx-data", "influx-config"}


def test_scripts_parse():
    import shutil
    import subprocess

    if not shutil.which("bash"):
        pytest.skip("no bash")
    for s in ("update.sh", "cleanup.sh", "release.sh", "native.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", s)]).returncode == 0, s
