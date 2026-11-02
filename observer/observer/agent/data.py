"""Read-only access to the recorded signals.

Uses a token that can only read the observer's bucket (OBSERVER_INFLUX_READ_TOKEN),
so nothing an agent does through these functions can change the database.
Every argument that reaches a Flux query is validated first: signal ids must
be known to the catalog, times and windows must match strict patterns.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

_RELATIVE = re.compile(r"^-\d{1,5}(s|m|h|d|w)$")
_EVERY = re.compile(r"^\d{1,4}(s|m|h|d)$")
AGGREGATES = ("mean", "min", "max", "last", "first", "count")
MAX_RAW_POINTS = 50_000


def flux_time(value: str) -> str:
    """A Flux time literal from "now", a relative duration (-6h) or an ISO time."""
    value = (value or "").strip()
    if value in ("", "now"):
        return "now()"
    if _RELATIVE.match(value):
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"time {value!r}: use 'now', a relative duration like -6h or an ISO time") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def flux_every(value: str) -> str | None:
    """A window like 1m, or None for raw samples."""
    value = (value or "raw").strip()
    if value == "raw":
        return None
    if not _EVERY.match(value):
        raise ValueError(f"every {value!r}: use 'raw' or a window like 30s, 1m, 15m, 1h")
    return value


@dataclass
class Series:
    signal: str
    times: list            # datetime, UTC
    values: list           # float, or str for text signals


class DataSource:
    """Queries against the observer's bucket with a read-only token."""

    def __init__(self, url: str, org: str, bucket: str, token: str):
        from influxdb_client import InfluxDBClient

        self.bucket, self.org = bucket, org
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=60_000)

    def _id_filter(self, ids: list[str]) -> str:
        """r.id == "a" or r.id == "b": equality on the tag uses InfluxDB's index (contains() scans)."""
        return "(" + " or ".join(f"r.id == {json.dumps(str(i))}" for i in ids) + ")"

    def series(self, ids: list[str], start: str, stop: str = "now", every: str = "raw",
               agg: str = "mean", limit: int = MAX_RAW_POINTS) -> dict[str, Series]:
        """Samples per signal; `limit` caps the points per signal."""
        if agg not in AGGREGATES:
            raise ValueError(f"agg must be one of {AGGREGATES}")
        window = flux_every(every)
        q = (f'from(bucket: {json.dumps(self.bucket)})\n'
             f'  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})\n'
             f'  |> filter(fn: (r) => r._measurement == "signal" and {self._id_filter(ids)})\n')
        if window:
            q += (f'  |> filter(fn: (r) => r._field == "value")\n'
                  f'  |> aggregateWindow(every: {window}, fn: {agg}, createEmpty: false)\n')
        q += f'  |> keep(columns: ["_time", "_value", "_field", "id"])\n  |> limit(n: {int(limit)})'
        out: dict[str, Series] = {}
        for table in self.client.query_api().query(q, org=self.org):
            for r in table.records:
                s = out.setdefault(r["id"], Series(r["id"], [], []))
                s.times.append(r.get_time())
                s.values.append(r.get_value())
        for s in out.values():
            order = sorted(range(len(s.times)), key=s.times.__getitem__)
            s.times = [s.times[i] for i in order]
            s.values = [s.values[i] for i in order]
        return out

    def coverage(self, ids: list[str]) -> dict[str, dict]:
        """{id: {first, last, points}} over the whole recorded history."""
        base = (f'from(bucket: {json.dumps(self.bucket)})\n  |> range(start: 0)\n'
                f'  |> filter(fn: (r) => r._measurement == "signal" and {self._id_filter(ids)})\n'
                f'  |> group(columns: ["id"])\n  |> keep(columns: ["_time", "_value", "id"])\n')
        out = {i: {"first": None, "last": None, "points": 0} for i in ids}
        api = self.client.query_api()
        for fn, key in (("first()", "first"), ("last()", "last")):
            for table in api.query(base + f"  |> {fn}", org=self.org):
                for r in table.records:
                    out[r["id"]][key] = r.get_time().strftime("%Y-%m-%dT%H:%M:%SZ")
        for table in api.query(base + '  |> map(fn: (r) => ({r with _value: 1}))\n  |> count()', org=self.org):
            for r in table.records:
                out[r["id"]]["points"] = int(r.get_value())
        return out

    def close(self) -> None:
        self.client.close()
