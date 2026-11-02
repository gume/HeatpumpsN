"""Export and import of the recorded data as one compact file (Parquet, zstd).

One row per stored sample, lossless: ``time_ns`` (BIGINT, nanoseconds since
the epoch, UTC), ``id`` (signal id), ``field`` (``value`` or ``text``),
``value`` (DOUBLE) / ``text`` (VARCHAR), and the tags ``source``,
``quantity``, ``location``, ``circuit``, ``role``; sorted by id, field and
time and compressed with zstd (about 2 bytes per sample, five times smaller
than a DuckDB database file of the same data). The file's key-value metadata
carries ``format``, ``meta`` (export time, bucket, range, counts; JSON) and
``signals`` (the registry's description of every exported signal; JSON).
DuckDB reads it directly::

    SELECT make_timestamp(time_ns // 1000) AS t, id, value FROM 'export.parquet' WHERE id LIKE '%temp_mix'

**Export** reads InfluxDB (the read-only token is enough) day by day as CSV
into a staging DuckDB database and writes the sorted Parquet file from it.

**Import** never deletes or overwrites: a sample is identified by signal id,
field and timestamp (not by its tags, which change when the registry
changes). For every signal it first reads the timestamps InfluxDB already has
in the file's time range and writes only the samples that are missing, so
importing the same file twice writes nothing the second time, and a file
that overlaps the database adds only what is new.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

FORMAT = "heatpumpsn-observer-samples/1"
TAGS = ("source", "quantity", "location", "circuit", "role")
FIELDS = ("value", "text")
# InfluxDB's CSV: comma separated, RFC 4180 quoting. Never sniffed: a text with
# a comma may first appear far into a file.
CSV_OPTS = "all_varchar=true, header=true, delim=',', quote='\"', escape='\"'"
WRITE_BATCH = 5000


@dataclass
class Influx:
    url: str
    org: str
    bucket: str
    token: str

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.token}"}

    def query_csv(self, flux: str, path: str) -> None:
        """Stream the result of a Flux query as plain CSV (one header) into `path`."""
        body = {"query": flux, "type": "flux",
                "dialect": {"header": True, "delimiter": ",", "annotations": []}}
        with httpx.stream("POST", f"{self.url.rstrip('/')}/api/v2/query", params={"org": self.org},
                          headers={**self._headers(), "Accept": "application/csv",
                                   "Content-Type": "application/json"},
                          content=json.dumps(body), timeout=600) as r:
            if r.status_code != 200:
                r.read()
                raise RuntimeError(f"InfluxDB query failed: HTTP {r.status_code} {r.text[:300]}")
            with open(path, "wb") as fp:
                for chunk in r.iter_bytes():
                    fp.write(chunk)

    def write_lines(self, lines: list[str]) -> None:
        data = gzip.compress("\n".join(lines).encode())
        r = httpx.post(f"{self.url.rstrip('/')}/api/v2/write",
                       params={"org": self.org, "bucket": self.bucket, "precision": "ns"},
                       headers={**self._headers(), "Content-Encoding": "gzip",
                                "Content-Type": "text/plain; charset=utf-8"}, content=data, timeout=300)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"InfluxDB write failed: HTTP {r.status_code} {r.text[:300]}")


def _sql_str(text: str) -> str:
    """Any text as an SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


_sql_path = _sql_str


def _has_rows(path: str) -> bool:
    """InfluxDB answers an empty result with a blank line and no header."""
    with open(path, "rb") as fp:
        header = fp.readline().strip()
    return b"," in header and b"t" in header.split(b",")


def _rfc3339(ns: int) -> str:
    sec, frac = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{frac:09d}Z"


def _flux_rows(bucket: str, start: str, stop: str, field: str, extra_filter: str = "") -> str:
    tags = ", ".join(f'{t}: if exists r.{t} then r.{t} else ""' for t in TAGS)
    return (f'from(bucket: {json.dumps(bucket)})\n'
            f'  |> range(start: {start}, stop: {stop})\n'
            f'  |> filter(fn: (r) => r._measurement == "signal" and r._field == "{field}"{extra_filter})\n'
            f'  |> map(fn: (r) => ({{t: int(v: r._time), id: r.id, v: r._value, {tags}}}))\n'
            f'  |> group()')


def _time_range(influx: Influx) -> tuple[int, int] | None:
    out = {}
    with tempfile.TemporaryDirectory(prefix="obs-db-") as tmp:
        for fn in ("first", "last"):
            p = os.path.join(tmp, f"{fn}.csv")
            influx.query_csv(f'from(bucket: {json.dumps(influx.bucket)})\n  |> range(start: 0)\n'
                             f'  |> filter(fn: (r) => r._measurement == "signal")\n  |> {fn}()\n'
                             f'  |> map(fn: (r) => ({{t: int(v: r._time)}}))\n  |> group()', p)
            import duckdb
            if not _has_rows(p):
                out[fn] = []
                continue
            vals = duckdb.sql(f"SELECT t FROM read_csv({_sql_path(p)}, {CSV_OPTS}) WHERE t IS NOT NULL AND t <> 't'"
                              ).fetchall()
            out[fn] = [int(v[0]) for v in vals]
    if not out["first"]:
        return None
    return min(out["first"]), max(out["last"])


def _parse_time(value: str | None) -> int | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def export_db(influx: Influx, path: str, start: str | None = None, stop: str | None = None,
              signals: list[dict] | None = None, progress=lambda msg: None, level: int = 19) -> dict:
    """Write the recorded samples (optionally only [start, stop)) into a new Parquet file at `path`."""
    import duckdb

    if os.path.exists(path):
        raise FileExistsError(path)
    full = _time_range(influx)
    if full is None:
        raise ValueError("the database holds no samples")
    lo = max(full[0], _parse_time(start) or full[0])
    hi = min(full[1] + 1, _parse_time(stop) or full[1] + 1)
    if lo >= hi:
        raise ValueError("no samples in this range")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="obs-db-") as tmp:
        stage = duckdb.connect(os.path.join(tmp, "stage.duckdb"))
        stage.execute("CREATE TABLE s (time_ns BIGINT, id VARCHAR, field VARCHAR, value DOUBLE, text VARCHAR, "
                      + ", ".join(f"{t} VARCHAR" for t in TAGS) + ")")
        day = 86_400 * 1_000_000_000
        a = lo - lo % day
        while a < hi:
            b = min(a + day, hi)
            for field in FIELDS:
                csv = os.path.join(tmp, "chunk.csv")
                influx.query_csv(_flux_rows(influx.bucket, _rfc3339(max(a, lo)), _rfc3339(b), field), csv)
                if _has_rows(csv):
                    val = "TRY_CAST(v AS DOUBLE), NULL" if field == "value" else "NULL, v"
                    stage.execute(
                        f"INSERT INTO s SELECT CAST(t AS BIGINT), id, '{field}', {val}, "
                        + ", ".join(f"NULLIF({t}, '')" for t in TAGS)
                        + f" FROM read_csv({_sql_path(csv)}, {CSV_OPTS}) WHERE t IS NOT NULL AND t <> 't'")
                os.remove(csv)
            progress(f"read up to {_rfc3339(b)[:10]}")
            a = b
        progress("writing the file")
        exported = {r[0] for r in stage.execute("SELECT DISTINCT id FROM s").fetchall()}
        counts = dict(stage.execute("SELECT field, count(*) FROM (SELECT DISTINCT * FROM s) GROUP BY field").fetchall())
        info = {}
        for sig in signals or []:
            if sig.get("id") in exported:
                i = sig.get("info") or {}
                info[sig["id"]] = {k: v for k, v in {
                    "name": i.get("name") or (sig.get("meta") or {}).get("name"), "unit": sig.get("unit"),
                    "quantity": sig.get("quantity"), "location": i.get("location"), "circuit": i.get("circuit"),
                    "role": i.get("role"), "description": i.get("description")}.items() if v}
        meta = {"format": FORMAT, "exported": time.strftime("%Y-%m-%d %H:%M:%S"), "bucket": influx.bucket,
                "org": influx.org, "first": _rfc3339(lo), "last": _rfc3339(hi - 1),
                "samples": sum(counts.values()), "value_samples": counts.get("value", 0),
                "text_samples": counts.get("text", 0), "signals": len(exported)}
        kv = ", ".join(f"{k}: {v}" for k, v in (("format", _sql_str(FORMAT)), ("meta", _sql_str(json.dumps(meta))),
                                                ("signals", _sql_str(json.dumps(info, ensure_ascii=False)))))
        tmp_out = path + ".part"
        stage.execute(f"COPY (SELECT DISTINCT * FROM s ORDER BY id, field, time_ns) TO {_sql_path(tmp_out)} "
                      f"(FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL {int(level)}, ROW_GROUP_SIZE 1000000, "
                      f"KV_METADATA {{{kv}}})")
        stage.close()
        os.replace(tmp_out, path)
    return {**meta, "file": path, "bytes": os.path.getsize(path), "seconds": round(time.monotonic() - started, 1)}


def read_meta(path: str) -> dict:
    """The export's metadata (and the signal descriptions under "signal_info"); checks the format."""
    import duckdb

    try:
        kv = {k.decode(): v.decode() for k, v in duckdb.sql(
            f"SELECT key, value FROM parquet_kv_metadata({_sql_path(path)})").fetchall()}
        cols = [r[0] for r in duckdb.sql(f"SELECT name FROM parquet_schema({_sql_path(path)})").fetchall()][1:]
    except duckdb.Error as exc:
        raise ValueError(f"not a Parquet file: {exc}") from exc
    if kv.get("format") != FORMAT:
        raise ValueError(f"not an observer export (format {kv.get('format')!r}, expected {FORMAT})")
    need = ["time_ns", "id", "field", "value", "text", *TAGS]
    if cols != need:
        raise ValueError(f"the file has the columns {cols}, expected {need}")
    meta = json.loads(kv.get("meta") or "{}")
    meta["signal_info"] = json.loads(kv.get("signals") or "{}")
    return meta


def _escape_tag(v: str) -> str:
    return v.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ").replace("\n", "\\n")


def _line(time_ns: int, sig: str, field: str, value, text, tags: dict) -> str | None:
    if field == "value":
        if value is None or not math.isfinite(value):
            return None
        fv = f"value={repr(float(value))}"
    else:
        if text is None:
            return None
        fv = 'text="' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'
    tagstr = "".join(f",{k}={_escape_tag(str(v))}" for k, v in sorted({"id": sig, **tags}.items()) if v not in (None, ""))
    return f"signal{tagstr} {fv} {time_ns}"


def import_db(influx: Influx, path: str, dry_run: bool = False, progress=lambda msg: None) -> dict:
    """Write the samples of an export file that the database does not have yet. Never deletes anything."""
    import duckdb

    meta = read_meta(path)
    started = time.monotonic()
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE VIEW src_samples AS SELECT * FROM read_parquet({_sql_path(path)})")
    groups = con.execute("SELECT id, field, min(time_ns), max(time_ns), count(*) FROM src_samples "
                         "GROUP BY id, field ORDER BY id, field").fetchall()
    written = skipped = 0
    per_signal = []
    with tempfile.TemporaryDirectory(prefix="obs-db-") as tmp:
        for n, (sig, field, lo, hi, count) in enumerate(groups, 1):
            csv = os.path.join(tmp, "existing.csv")
            esc = json.dumps(sig)
            influx.query_csv(f'from(bucket: {json.dumps(influx.bucket)})\n'
                             f'  |> range(start: {_rfc3339(lo)}, stop: {_rfc3339(hi + 1)})\n'
                             f'  |> filter(fn: (r) => r._measurement == "signal" and r.id == {esc} '
                             f'and r._field == "{field}")\n'
                             f'  |> map(fn: (r) => ({{t: int(v: r._time)}}))\n  |> group()', csv)
            con.execute("CREATE OR REPLACE TEMP TABLE ex (t BIGINT)")
            if _has_rows(csv):
                con.execute(f"INSERT INTO ex SELECT DISTINCT CAST(t AS BIGINT) FROM read_csv({_sql_path(csv)}, {CSV_OPTS}) "
                            "WHERE t IS NOT NULL AND t <> 't'")
            cur = con.execute(
                "SELECT time_ns, value, text, " + ", ".join(TAGS) + " FROM ("
                "  SELECT DISTINCT ON (time_ns) * FROM src_samples WHERE id = ? AND field = ? ORDER BY time_ns"
                ") s WHERE NOT EXISTS (SELECT 1 FROM ex WHERE ex.t = s.time_ns) ORDER BY time_ns", [sig, field])
            new = 0
            while True:
                rows = cur.fetchmany(WRITE_BATCH)
                if not rows:
                    break
                lines = [ln for ln in (_line(r[0], sig, field, r[1], r[2], dict(zip(TAGS, r[3:]))) for r in rows) if ln]
                if lines and not dry_run:
                    influx.write_lines(lines)
                new += len(lines)
            written += new
            skipped += count - new
            if new:
                per_signal.append({"id": sig, "field": field, "written": new, "skipped": count - new})
            if n % 20 == 0:
                progress(f"{n}/{len(groups)} signals checked, {written} samples {'to write' if dry_run else 'written'}")
    con.close()
    return {"file": os.path.basename(path), "dry_run": dry_run, "exported": meta.get("exported"),
            "range": [meta.get("first"), meta.get("last")], "signals": len(groups),
            "written": written, "skipped_existing": skipped, "new_by_signal": per_signal[:200],
            "seconds": round(time.monotonic() - started, 1)}
