"""Build a DuckDB database from the exported CSV.gz series.

Input : /home/gume/HeatpumpsN/analysis/db/*.csv.gz  (columns t,v; t is UTC ISO-8601)
Output: /home/gume/HeatpumpsN/analysis/heatpumps.duckdb

Creates one table per entity (name = entity_id with '.' -> '_'), a unified
long table `readings(entity_id, t TIMESTAMPTZ, v DOUBLE)`, and an `entities`
metadata table with row counts for verification.
"""
import gzip
import os
import duckdb

DB_DIR = "/home/gume/HeatpumpsN/analysis/db"
DB_PATH = "/home/gume/HeatpumpsN/analysis/heatpumps.duckdb"

MEASUREMENT = {
    "mixer_state": "%",
    "mixermeter_bmi160_mixer_value": "%",
    "mixer_forward_temperature": "degC",
    "mixer_return_temperature": "degC",
    "mixer_incoming_temperature": "degC",
    "boiler_curburnpow": "W",
    "boiler_curflowtemp": "degC",
    "boiler_heatingpumpmod": "%",
    "binary_sensor.boiler_heatingactive": "binary",
    "binary_sensor.boiler_wwcharging": "binary",
    "heatpumps_pump_1_power": "W",
    "heatpumps_pump_2_power": "W",
    "heatpumps_pump_3_power": "W",
    "outside_temperature_last_value": "degC",
    "temperature_humidity_sensor_d281_temperature": "degC",
}


def sanitize(name):
    return name.replace(".", "_").replace("-", "_")


def count_csv_rows(path):
    with gzip.open(path, "rt") as f:
        # count data rows (subtract header)
        n = sum(1 for _ in f)
    return n - 1


def main():
    # fresh build
    for suffix in ("", ".wal"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)

    con = duckdb.connect(DB_PATH)
    con.execute("SET timezone='UTC'")

    files = sorted(f for f in os.listdir(DB_DIR) if f.endswith(".csv.gz"))
    entities_rows = []

    for fname in files:
        entity_id = fname[: -len(".csv.gz")]
        table = sanitize(entity_id)
        fpath = os.path.join(DB_DIR, fname)
        expected = count_csv_rows(fpath)

        con.execute(f'''
            CREATE TABLE "{table}" AS
            SELECT CAST(t AS TIMESTAMPTZ) AS t,
                   CAST(v AS DOUBLE) AS v
            FROM read_csv_auto('{fpath}')
        ''')
        actual = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        status = "OK" if actual == expected else "MISMATCH"
        print(f"{entity_id:45s} expected={expected:8d} actual={actual:8d} {status}")
        if actual != expected:
            raise SystemExit(f"Row count mismatch for {entity_id}")

        entities_rows.append((entity_id, table, fname, MEASUREMENT.get(entity_id), actual))

    # metadata table
    con.execute("""
        CREATE TABLE entities (
            entity_id VARCHAR,
            table_name VARCHAR,
            source_file VARCHAR,
            measurement VARCHAR,
            rows BIGINT
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?,?,?,?,?)", entities_rows)

    # unified long table
    union_sql = " UNION ALL ".join(
        f"SELECT '{eid}' AS entity_id, t, v FROM {sanitize(eid)}"
        for eid, *_ in [(r[0],) for r in entities_rows]
    )
    # rebuild cleanly with entity list
    union_sql = " UNION ALL ".join(
        f"SELECT '{r[0]}' AS entity_id, t, v FROM \"{r[1]}\""
        for r in entities_rows
    )
    con.execute(f"CREATE TABLE readings AS {union_sql}")

    total = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    print(f"\nTotal readings rows: {total}")
    print("Tables:", [r[0] for r in con.execute("SHOW TABLES").fetchall()])

    con.close()
    print(f"\nDatabase written to {DB_PATH}")


if __name__ == "__main__":
    main()
