"""Compare the controller's built-in target partition table with a build's partitions.bin.

    HPN_URL=localhost:30080 HPN_PORTAL_PASSWORD=... \\
        python3 tools/partition_check.py [.pio/build/esp32dev_sim_ota/partitions.bin]

Run before a migration (docs/partition-migration.md): the table the firmware
would write must be byte-identical to the one gen_esp32part.py produces from
partitions.csv. Exit status 0 when it matches. Pass --migrate to start the
migration when the tables match and the device reports it ready.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpn_auth import Client  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("bin", nargs="?", default=".pio/build/esp32dev_sim_ota/partitions.bin")
ap.add_argument("--migrate", action="store_true")
args = ap.parse_args()

host = os.environ.get("HPN_URL", "localhost:30080").replace("http://", "")
client = Client(host, os.environ.get("HPN_PORTAL_USER", "admin"), os.environ.get("HPN_PORTAL_PASSWORD", "admin"))
st, lay = client.request("GET", "/api/system/partitions")
if st != 200:
    sys.exit(f"GET /api/system/partitions: HTTP {st} {lay}")

device = bytes.fromhex(lay["targetTable"])
build = open(args.bin, "rb").read()[: len(device)]
run = lay["running"]
print(f"layout {lay['layout']}  running {run['label']} at {run['address']} ({run['state']})")
print(f"ready {lay['migrationReady']}  {lay['migrationBlocker']}")
if device != build:
    sys.exit(f"MISMATCH: device target table differs from {args.bin}")
print(f"target table matches {args.bin} ({len(device)} bytes)")
if args.migrate:
    if not lay["migrationReady"]:
        sys.exit("not migrating: " + lay["migrationBlocker"])
    try:
        st, reply = client.request("POST", "/api/system/partitions/migrate")
        print(f"migrate: HTTP {st} {reply}")
        sys.exit(0 if st == 200 else 1)
    except Exception as exc:  # the controller reboots before it answers
        print(f"migrate: connection closed ({exc.__class__.__name__}); the controller is rebooting")
