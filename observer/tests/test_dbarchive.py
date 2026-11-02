import json
import os

import duckdb
import pytest

from observer import dbarchive
from observer.dbarchive import FORMAT, _line, import_db, read_meta


def make_file(path, rows):
    con = duckdb.connect()
    con.execute("CREATE TABLE s (time_ns BIGINT, id VARCHAR, field VARCHAR, value DOUBLE, text VARCHAR, "
                "source VARCHAR, quantity VARCHAR, location VARCHAR, circuit VARCHAR, role VARCHAR)")
    con.executemany("INSERT INTO s VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    meta = json.dumps({"exported": "x", "first": "a", "last": "b"})
    con.execute(f"COPY (SELECT * FROM s ORDER BY id, field, time_ns) TO '{path}' "
                f"(FORMAT parquet, COMPRESSION zstd, KV_METADATA {{format: '{FORMAT}', meta: '{meta}', signals: '{{}}'}})")


class FakeInflux:
    """Keeps (id, field, time) -> line; answers the import's existence queries as InfluxDB's CSV."""
    bucket = "b"

    def __init__(self, existing=()):
        self.points = {k: "old" for k in existing}

    def query_csv(self, flux, path):
        sig = flux.split('r.id == ')[1].split(' and')[0].strip('"') if "r.id == " in flux else None
        field = flux.split('r._field == "')[1].split('"')[0]
        times = sorted(t for (i, f, t) in self.points if i == sig and f == field)
        with open(path, "w") as fp:
            fp.write(",result,table,t\r\n" + "".join(f",_result,0,{t}\r\n" for t in times) if times else "\r\n")

    def write_lines(self, lines):
        for ln in lines:
            rest, t = ln.rsplit(" ", 1)                 # the timestamp is last
            head, fields = rest.split(" ", 1)           # the tag set has no unescaped spaces in these tests
            sig = [p.split("=", 1)[1] for p in head.split(",")[1:] if p.startswith("id=")][0]
            self.points[(sig, fields.split("=", 1)[0], int(t))] = ln


ROWS = [(1_000, "room/temp", "value", 20.5, None, "ha_state", "temperature", "BF", None, "room"),
        (2_000, "room/temp", "value", 20.6, None, "ha_state", "temperature", "BF", None, "room"),
        (2_000, "room/temp", "value", 20.6, None, "ha_state", "temperature", "MF", None, "room"),  # same sample, other tags
        (3_000, "ctl/event#detail", "text", None, 'Mixer "A", moved', "heatpumpsn", None, None, None, None)]


def test_import_writes_only_missing_samples_and_is_repeatable(tmp_path):
    path = str(tmp_path / "x.parquet")
    make_file(path, ROWS)
    influx = FakeInflux(existing=[("room/temp", "value", 1_000)])
    r = import_db(influx, path)
    assert (r["written"], r["skipped_existing"]) == (2, 2)               # 1000 existed; the double row at 2000 once
    again = import_db(influx, path)
    assert (again["written"], again["skipped_existing"]) == (0, 4)       # a second import writes nothing
    assert influx.points[("ctl/event#detail", "text", 3000)] == \
        'signal,id=ctl/event#detail,source=heatpumpsn text="Mixer \\"A\\", moved" 3000'
    dry = import_db(FakeInflux(), path, dry_run=True)
    assert dry["written"] == 3 and dry["dry_run"]


def test_line_protocol_escaping():
    assert _line(5, "zigbee2mqtt/hall sensor#temp", "value", 21.0, None, {"location": "BF hall", "role": ""}) == \
        r"signal,id=zigbee2mqtt/hall\ sensor#temp,location=BF\ hall value=21.0 5"
    assert _line(5, "x", "value", float("nan"), None, {}) is None
    assert _line(5, "x", "text", None, "a\\b", {"circuit": "a,b=c"}) == r'signal,circuit=a\,b\=c,id=x text="a\\b" 5'


def test_read_meta_rejects_other_files(tmp_path):
    good = str(tmp_path / "g.parquet")
    make_file(good, ROWS)
    assert read_meta(good)["exported"] == "x"
    other = str(tmp_path / "o.parquet")
    duckdb.execute(f"COPY (SELECT 1 AS a) TO '{other}' (FORMAT parquet)")
    with pytest.raises(ValueError, match="not an observer export"):
        read_meta(other)
    junk = tmp_path / "j.parquet"
    junk.write_bytes(b"hello")
    with pytest.raises(ValueError):
        read_meta(str(junk))
