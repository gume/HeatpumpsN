import json
from types import SimpleNamespace

from observer import collector
from observer.service import Service
from observer.store import NullWriter


def test_status_file(tmp_path):
    cfg = {"paths": {"catalog": str(tmp_path / "data" / "catalog.json")}}
    (tmp_path / "data").mkdir()
    assert collector.read_status(cfg) is None
    (tmp_path / "data" / "collector.json").write_text("{broken")
    assert collector.read_status(cfg) is None
    (tmp_path / "data" / "collector.json").write_text(json.dumps({"stored": 5}))
    assert collector.read_status(cfg) == {"stored": 5}
    w = NullWriter()
    w.write(object())
    w.close()


def test_total_counts_once_and_follows_the_collector():
    written = {"v": ("run1", 100)}
    svc = SimpleNamespace(_written=lambda: written["v"], _last_total=None,
                          db_total={"samples": 1000, "session": "run1", "stored_then": 100})
    total = lambda: Service.stored_total(svc)
    assert total() == 1000
    written["v"] = ("run1", 150)
    assert total() == 1050
    written["v"] = ("run2", 0)                      # the collector restarted: its counter begins at 0
    assert total() == 1050
    written["v"] = ("run2", 20)
    assert total() == 1070
    svc.db_total = None
    assert total() is None
