"""Raw storage: every stored sample with its own timestamp, no aggregation.

One InfluxDB measurement, ``signal``:

* tags: ``id`` (signal id), ``source`` (first topic level: ``ems-esp``,
  ``heatpumpsn``, ``zigbee2mqtt``, ``ha_state``, ...) and the registry's
  ``quantity``/``location``/``circuit``/``role`` when set;
* fields: ``value`` (float) or ``text`` (string);
* time: when the message arrived, in nanoseconds.

Resampling (for example to 10 s) happens when the data is processed, never
when it is stored.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)
MEASUREMENT = "signal"


@dataclass(frozen=True)
class Point:
    signal: str
    tags: dict
    value: float | None
    text: str | None
    time_ns: int


class MemoryWriter:
    """Keeps points in a list (tests, dry runs)."""

    def __init__(self):
        self.points: list[Point] = []

    def write(self, point: Point) -> None:
        self.points.append(point)

    def close(self) -> None:
        pass


class NullWriter:
    """Stores nothing: the portal when a separate collector writes the data (OBSERVER_INGEST=external)."""

    def write(self, point: Point) -> None:
        pass

    def close(self) -> None:
        pass


class InfluxWriter:
    """Batched writes to an InfluxDB 2.x bucket."""

    def __init__(self, url: str, token: str, org: str, bucket: str):
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import WriteOptions

        self.bucket, self.org = bucket, org
        self.client = InfluxDBClient(url=url, token=token, org=org)
        self.api = self.client.write_api(write_options=WriteOptions(
            batch_size=500, flush_interval=5_000, retry_interval=5_000, max_retries=10))

    def write(self, point: Point) -> None:
        from influxdb_client import Point as IPoint, WritePrecision

        p = IPoint(MEASUREMENT).tag("id", point.signal)
        for key, value in point.tags.items():
            p = p.tag(key, value)
        p = p.field("value", point.value) if point.value is not None else p.field("text", point.text)
        self.api.write(bucket=self.bucket, org=self.org, record=p.time(point.time_ns, WritePrecision.NS))

    def close(self) -> None:
        self.api.close()
        self.client.close()
