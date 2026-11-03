"""The collector's version lock: one hash of its image's inputs per COLLECTOR_VERSION.

    python scripts/collector_lock.py           # record the current version (after raising it)
    python scripts/collector_lock.py --check   # fails when the files changed without a new version

The collector image is built only from these files (Dockerfile.collector). An
unchanged version must mean an unchanged image, otherwise an update would
replace the running collector, or two machines would run different code
under one version.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "collector.lock")
COMPOSE = os.path.join(ROOT, "deploy", "docker-compose.yml")


def version() -> str:
    text = open(os.path.join(ROOT, "observer", "collector.py"), encoding="utf-8").read()
    return re.search(r'^COLLECTOR_VERSION = "([^"]+)"', text, re.M).group(1)


def files() -> list[str]:
    text = open(os.path.join(ROOT, "observer", "collector.py"), encoding="utf-8").read()
    names = re.findall(r'"([a-z_]+\.py)"', re.search(r"^FILES = \(([^)]*)\)", text, re.M).group(1))
    return sorted([f"observer/{n}" for n in names] + ["Dockerfile.collector", "collector-requirements.txt"])


def digest() -> str:
    h = hashlib.sha256()
    for rel in files():
        h.update(rel.encode() + b"\0")
        with open(os.path.join(ROOT, rel), "rb") as fp:
            h.update(fp.read().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def load() -> dict:
    if not os.path.exists(LOCK):
        return {"versions": {}}
    with open(LOCK, encoding="utf-8") as fp:
        return json.load(fp)


def check() -> str | None:
    """None when the current files are the ones recorded for the current version."""
    v, recorded = version(), load()["versions"].get(version())
    if recorded is None:
        return f"collector {v} is not in collector.lock: run python scripts/collector_lock.py"
    if recorded != digest():
        return (f"the collector's files changed but COLLECTOR_VERSION is still {v}: raise it in "
                "observer/collector.py, then run python scripts/collector_lock.py")
    compose = open(COMPOSE, encoding="utf-8").read()
    if f'COLLECTOR_DIGEST: "{recorded}"' not in compose or f"image: heatpumpsn-collector:{v} " not in compose:
        return "deploy/docker-compose.yml does not carry the collector's version and hash: run python scripts/collector_lock.py"
    return None


def record() -> str:
    lock, v, d = load(), version(), digest()
    old = lock["versions"].get(v)
    if old is not None and old != d:
        sys.exit(f"collector {v} is already recorded with other files: raise COLLECTOR_VERSION first")
    lock["versions"][v] = d
    with open(LOCK, "w", encoding="utf-8") as fp:
        json.dump(lock, fp, indent=1, sort_keys=True)
        fp.write("\n")
    with open(COMPOSE, encoding="utf-8") as fp:      # the compose file builds with the same label
        text = fp.read()
    text = re.sub(r'(COLLECTOR_DIGEST: )"[^"]*"', rf'\g<1>"{d}"', text)
    text = re.sub(r"(image: heatpumpsn-collector:)\S+", rf"\g<1>{v}", text)
    with open(COMPOSE, "w", encoding="utf-8") as fp:
        fp.write(text)
    return f"collector {v}: {d[:12]} (collector.lock, deploy/docker-compose.yml)"


if __name__ == "__main__":
    if "--check" in sys.argv:
        problem = check()
        print(problem or f"collector {version()}: unchanged")
        sys.exit(1 if problem else 0)
    print(record())
