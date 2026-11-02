"""Command line: observer <command> [--config PATH]

All operational data lives outside the repository, in the data directory
OBSERVER_HOME (default ~/.local/share/heatpumpsn-observer): config.yaml, .env,
registry.yaml, house/ (description, model, notes, archive) and data/ (catalog,
settings, proposals). The repository holds only code, docs and defaults.

  init     [--home PATH] [--profile dev|release]   create the data directory from the defaults
  influx-setup             create the read-only InfluxDB token if missing (idempotent)

  serve    [--dry-run] [--host H] [--port P]   ingest + web portal (http://H:P/), the normal way to run it
  listen   [--seconds N]   collect for N s (default 120): update the catalog, store nothing
  catalog  [--unregistered] [--source PREFIX]   show the inventory and how each signal is handled
  run      [--dry-run]     ingest continuously and store into InfluxDB
  propose  [--limit N]     ask the LLM to describe unregistered signals -> proposals.yaml
  accept   ID [ID ...] | --all [--min-confidence X]   merge proposals into registry.yaml

  state export [FILE.tgz] | state import FILE.tgz
                           registry, house understanding, library, settings, context (not secrets, not data)
  db export [-o FILE] [--start T] [--stop T]   recorded samples into one compact Parquet file
  db import FILE [--dry-run]                   add the samples the database does not have (never deletes)

  sandbox-setup            create the sandbox's Python environment (numpy, pandas, scipy, matplotlib)
  library [list]           the library sources, tools and skills (version, audit and test state)
  library export [--all | --item KIND/NAME ...] [--name N] [--version V] [-o FILE.tgz]
                           house items (default), all items or chosen ones as a .tgz pack
  library import FILE.tgz  install a pack (strictly checked; a previous version is kept)
  library remove-pack NAME
  test-tool NAME           run a library tool's tests in the sandbox
  agent    [--skill NAME] [QUESTION ...]   one agent run in the foreground; the record goes to runs/
  mcp                      the agent tools as an MCP server (stdio)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import yaml

from .catalog import Catalog
from .ingest import Ingest, run_mqtt
from .registry import Registry
from .store import InfluxWriter, MemoryWriter

log = logging.getLogger("observer")


DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults")


def observer_home() -> str:
    return os.path.abspath(os.path.expanduser(
        os.environ.get("OBSERVER_HOME") or "~/.local/share/heatpumpsn-observer"))


def cmd_init(home: str, profile: str = "dev") -> None:
    """Create the data directory; files that already exist are left alone."""
    import shutil

    os.makedirs(os.path.join(home, "data"), exist_ok=True)
    os.makedirs(os.path.join(home, "house", "archive"), exist_ok=True)
    created = []
    config = "config.release.yaml" if profile == "release" else "config.example.yaml"
    for src, dst in ((config, "config.yaml"), ("env.example", ".env"),
                     ("registry.yaml", "registry.yaml")):
        target = os.path.join(home, dst)
        if not os.path.exists(target):
            shutil.copyfile(os.path.join(DEFAULTS, src), target)
            created.append(dst)
    notes = os.path.join(home, "house", "notes.md")
    if not os.path.exists(notes):
        open(notes, "w").close()
        created.append("house/notes.md")
    os.chmod(os.path.join(home, ".env"), 0o600)
    print(f"data directory {home}: " + (", ".join(created) + " created" if created else "already complete"))
    print("next: edit config.yaml and .env there, then run `observer serve`")


def load_env(path: str) -> None:
    """KEY=VALUE lines from a .env file into the environment (existing variables win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


ENV_OVERRIDES = (("OBSERVER_MQTT_HOST", "mqtt", "host", str), ("OBSERVER_MQTT_PORT", "mqtt", "port", int),
                 ("OBSERVER_MQTT_USER", "mqtt", "username", str),
                 ("OBSERVER_INFLUX_URL", "influx", "url", str), ("OBSERVER_INFLUX_ORG", "influx", "org", str),
                 ("OBSERVER_INFLUX_BUCKET", "influx", "bucket", str))


def apply_env_overrides(cfg: dict) -> None:
    """Machine-specific settings from the environment win over config.yaml (docker compose)."""
    for var, section, key, conv in ENV_OVERRIDES:
        if os.environ.get(var, "").strip():
            cfg.setdefault(section, {})[key] = conv(os.environ[var].strip())
    if os.environ.get("OBSERVER_MQTT_PASSWORD"):
        cfg.setdefault("mqtt", {})["password_env"] = "OBSERVER_MQTT_PASSWORD"
    if "OBSERVER_MQTT_TOPIC_PREFIX" in os.environ:
        prefix = os.environ["OBSERVER_MQTT_TOPIC_PREFIX"].strip()
        cfg.setdefault("mqtt", {}).update(topic_prefix=prefix, subscribe=[prefix + "#"])


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(f"no configuration at {path}: run `observer init` (data directory: OBSERVER_HOME)")
    base = os.path.dirname(os.path.abspath(path))
    load_env(os.path.join(base, ".env"))
    with open(path, encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}
    cfg["llm"] = cfg.get("llm") or {}   # providers come from .env, models from the Settings page
    apply_env_overrides(cfg)
    paths = cfg.setdefault("paths", {})
    for key, default in (("catalog", "data/catalog.json"), ("registry", "registry.yaml"),
                         ("proposals", "data/proposals.yaml"), ("house_dir", "house/house.keep"),
                         ("house_draft", "data/house_draft.yaml"), ("settings", "data/settings.yaml"),
                         ("runs", "runs/runs.keep"), ("library", "library/library.keep"),
                         ("sandbox_venv", "sandbox/venv"), ("exports", "exports/exports.keep")):
        value = paths.get(key, default)
        paths[key] = value if os.path.isabs(value) else os.path.join(base, value)
        os.makedirs(os.path.dirname(paths[key]), exist_ok=True)
    paths["house_dir"] = os.path.dirname(paths["house_dir"])   # a folder: house/
    cfg["config_path"] = os.path.abspath(path)
    old_draft = os.path.join(os.path.dirname(paths["house_draft"]), "house_proposal.yaml")
    if os.path.exists(old_draft) and not os.path.exists(paths["house_draft"]):
        os.replace(old_draft, paths["house_draft"])             # the draft's former name
    paths["runs"] = os.path.dirname(paths["runs"])             # agent runs: runs/<id>/
    paths["exports"] = os.path.dirname(paths["exports"])       # database exports (.parquet)
    paths["library"] = os.path.dirname(paths["library"])       # house tools and skills, audits
    return cfg


def _mqtt_args(cfg: dict) -> dict:
    m = cfg.get("mqtt", {})
    return {"host": m.get("host", "localhost"), "port": int(m.get("port", 1883)),
            "subscribe": m.get("subscribe", ["#"]), "username": m.get("username"),
            "password": os.environ.get(m.get("password_env", ""), None)}


def _ingest(cfg: dict, writer) -> Ingest:
    m = cfg.get("mqtt", {})
    return Ingest(Catalog(cfg["paths"]["catalog"]), Registry(cfg["paths"]["registry"]), writer,
                  prefix=m.get("topic_prefix", ""), statestream_base=cfg.get("statestream_base", "ha_state"))


def cmd_listen(cfg: dict, args) -> None:
    ingest = _ingest(cfg, MemoryWriter())
    ingest.registry.signals, ingest.registry.rules, ingest.registry.store_unregistered = {}, [], False
    print(f"listening {args.seconds:.0f} s ...", file=sys.stderr)
    run_mqtt(ingest, duration_s=args.seconds, **_mqtt_args(cfg))
    ingest.catalog.save()
    print(f"{len(ingest.catalog.entries)} signals in the catalog, {len(ingest.new_signals)} new")


def cmd_catalog(cfg: dict, args) -> None:
    catalog, registry = Catalog(cfg["paths"]["catalog"]), Registry(cfg["paths"]["registry"])
    now = time.time()
    for sig, e in sorted(catalog.entries.items()):
        if args.source and not sig.startswith(args.source):
            continue
        r = registry.resolve(sig)
        if args.unregistered and r.source != "none":
            continue
        name = r.info.get("name") or e.meta.get("name") or ""
        value = e.last_text if e.last_value is None else f"{e.last_value:g}"
        flag = "STORE" if r.store else "  -  "
        print(f"{flag} {sig:70s} {str(name)[:28]:28s} {str(value)[:14]:>14s}  {e.count:5d}x  "
              f"{(now - e.last_seen) / 60:6.0f} min ago  [{r.source}]")


def cmd_run(cfg: dict, args) -> None:
    if args.dry_run:
        writer = MemoryWriter()
    else:
        i = cfg.get("influx", {})
        token = os.environ.get(i.get("token_env", "INFLUX_TOKEN"))
        if not token:
            sys.exit(f"InfluxDB token missing: set {i.get('token_env', 'INFLUX_TOKEN')}")
        writer = InfluxWriter(i["url"], token, i["org"], i["bucket"])
    ingest = _ingest(cfg, writer)
    last_save = [time.time()]

    def tick():
        if time.time() - last_save[0] >= 60:
            last_save[0] = time.time()
            ingest.catalog.save()
            if ingest.new_signals:
                log.info("new signals: %s", ", ".join(ingest.new_signals[-10:]))
                ingest.new_signals.clear()
            log.info("%d samples stored, %d signals known", ingest.stored, len(ingest.catalog.entries))

    try:
        run_mqtt(ingest, on_tick=tick, **_mqtt_args(cfg))
    except KeyboardInterrupt:
        pass
    finally:
        ingest.catalog.save()
        writer.close()


def _writer(cfg: dict, dry_run: bool):
    if dry_run:
        return MemoryWriter(), "dry-run"
    i = cfg.get("influx", {})
    token = os.environ.get(i.get("token_env", "INFLUX_TOKEN"))
    if not token:
        sys.exit(f"InfluxDB token missing: set {i.get('token_env', 'INFLUX_TOKEN')} (or use --dry-run)")
    return InfluxWriter(i["url"], token, i["org"], i["bucket"]), "influx"


def cmd_serve(cfg: dict, args) -> None:
    import uvicorn

    from .service import Service
    from .web import create_app

    writer, kind = _writer(cfg, args.dry_run)
    service = Service(cfg, writer, kind)
    service.start(_mqtt_args(cfg))
    web = cfg.get("web", {})
    host, port = args.host or web.get("host", "127.0.0.1"), args.port or int(web.get("port", 8090))
    log.info("portal on http://%s:%s/", host, port)
    try:
        uvicorn.run(create_app(service), host=host, port=port, log_level="warning")
    finally:
        service.stop()


def cmd_propose(cfg: dict, args) -> None:
    from .llm import propose
    from .settings import Settings

    client = Settings(cfg["paths"]["settings"]).client("propose")
    if client is None:
        sys.exit("no LLM configured for bulk proposals: set provider and model on the portal's Settings page")
    catalog, registry = Catalog(cfg["paths"]["catalog"]), Registry(cfg["paths"]["registry"])
    todo = {}
    for sig, e in sorted(catalog.entries.items()):
        if registry.resolve(sig).source == "none":
            todo[sig] = {"meta": e.meta, "last": e.last_text if e.last_value is None else e.last_value,
                         "numeric": e.numeric, "messages": e.count}
        if len(todo) >= args.limit:
            break
    if not todo:
        print("every signal in the catalog is described")
        return
    known = {sig: {k: v for k, v in entry.items() if k != "store"} for sig, entry in registry.signals.items()}
    proposals = propose(client, todo, cfg.get("context", ""), known)
    with open(cfg["paths"]["proposals"], "w", encoding="utf-8") as fp:
        yaml.safe_dump({"proposals": proposals}, fp, sort_keys=False, allow_unicode=True, width=100)
    print(f"{len(proposals)} proposals for {len(todo)} signals -> {cfg['paths']['proposals']}")


def cmd_accept(cfg: dict, args) -> None:
    with open(cfg["paths"]["proposals"], encoding="utf-8") as fp:
        proposals = (yaml.safe_load(fp) or {}).get("proposals", [])
    registry = Registry(cfg["paths"]["registry"])
    chosen = [p for p in proposals if (args.all and p.get("confidence", 0) >= args.min_confidence)
              or p["signal"] in args.ids]
    for p in chosen:
        registry.add(p["signal"], p)
    registry.save()
    print(f"{len(chosen)} signals added to {cfg['paths']['registry']}")


def cmd_sandbox_setup(cfg: dict, args) -> None:
    from .agent.manager import make_sandbox
    from .agent.sandbox import Sandbox

    sb = make_sandbox(cfg)
    Sandbox.setup(sb.venv)
    probe = sb.run("import numpy, pandas, scipy, matplotlib, socket\n"
                   "try:\n    socket.create_connection(('1.1.1.1', 53), 2); print('NETWORK OPEN')\n"
                   "except OSError:\n    print('network: none')\n", os.path.join(sb.venv, "..", "probe"), "probe")
    print(probe.stdout.strip() or probe.stderr.strip())
    print(sb.status())


def cmd_library(cfg: dict, args) -> None:
    from .agent.manager import make_library

    lib = make_library(cfg)
    if args.action == "export":
        chosen = [tuple(i.split("/", 1)) for i in args.item] if args.item else \
            ([(k, n) for k in ("tools", "skills") for n in lib.items(k)] if args.all else None)
        name = args.name or ("library-export" if chosen else "house-export")
        version = args.version or time.strftime("%Y.%m.%d-%H%M")
        data = lib.export(chosen, name, version)
        out = args.output or f"{name}-{version}.tgz"
        with open(out, "wb") as fp:
            fp.write(data)
        print(f"{out}: {len(data)} bytes")
        return
    if args.action == "import":
        with open(args.file, "rb") as fp:
            r = lib.import_archive(fp.read())
        print(f"pack {r['pack']} {r['version']}: {len(r['items'])} items")
        return
    if args.action == "remove-pack":
        lib.remove_pack(args.file)
        print("removed")
        return
    print("SOURCES (highest precedence first)")
    for s in lib.sources():
        print(f"  {s.id:28s} {s.kind:5s} {s.version:10s} {s.root}")
    for kind in ("tools", "skills"):
        print(kind.upper())
        for i in lib.items(kind).values():
            s = i.summary()
            tests = {None: "  ", True: "T+", False: "T-"}[s.get("tests_passed")] if kind == "tools" else "  "
            print(f"  {'A' if s['audited'] else '-'} {tests} {i.name:22s} v{i.version:<3} [{i.source.id}] "
                  f"{(s.get('purpose') or s.get('description') or '')[:90]}")
    print("A = audited by the owner, T+/T- = tests passed/failed for the current version")


def cmd_test_tool(cfg: dict, args) -> None:
    from .agent.manager import make_library, make_sandbox

    lib = make_library(cfg)
    r = lib.run_tests(args.name, make_sandbox(cfg, lib))
    print(r["output"])
    print("PASSED" if r["passed"] else "FAILED")


def cmd_agent(cfg: dict, args) -> None:
    from .agent.runner import Run
    from .service import Service

    service = Service(cfg, MemoryWriter(), "none")
    agents = service.agents
    client = service.settings.client("agent")
    if client is None:
        sys.exit("no LLM configured for the task 'agent' (portal Settings)")
    try:
        skill = agents.library.get("skills", args.skill) if args.skill else None
    except KeyError:
        sys.exit(f"no skill {args.skill!r}: see `observer library`")
    folder = os.path.join(agents.runs_dir, time.strftime("%Y%m%d-%H%M%S-") + (args.skill or "question"))
    os.makedirs(folder)
    run = Run(folder)
    meta = agents.execute_in(folder, client, " ".join(args.question), skill)
    for step in run.steps():
        if step["kind"] == "tool":
            print(f"  [{step['n']:2d}] {step['tool']}({', '.join(f'{k}=' for k in step['args'])})")
    print(f"\n{meta['status']} in {meta.get('seconds')} s, {meta.get('steps')} steps -> {folder}\n")
    print(run.report() or meta.get("error") or "")


def cmd_db(cfg: dict, args) -> None:
    import json

    from .dbarchive import export_db, import_db
    from .service import Service

    service = Service(cfg, MemoryWriter(), "none")
    if args.action == "export":
        out = args.output or os.path.join(cfg["paths"]["exports"], time.strftime("observer-%Y%m%d-%H%M%S.parquet"))
        r = export_db(service._influx(write=False), out, args.start, args.stop, service.signals(),
                      lambda m: print(m, file=sys.stderr))
    else:
        if not args.file:
            sys.exit("db import FILE")
        r = import_db(service._influx(write=True), args.file, args.dry_run, lambda m: print(m, file=sys.stderr))
        r.pop("new_by_signal", None)
    print(json.dumps(r, indent=1))


def cmd_influx_setup(cfg: dict, args) -> None:
    """Create the read-only token for the agent tools and exports, if missing (idempotent)."""
    import httpx

    i = cfg.get("influx") or {}
    url, org, bucket = i.get("url", "").rstrip("/"), i.get("org", ""), i.get("bucket", "")
    admin = os.environ.get(i.get("token_env", "INFLUX_TOKEN"), "")
    var = (cfg.get("agent") or {}).get("read_token_env", "OBSERVER_INFLUX_READ_TOKEN")
    current = os.environ.get(var, "")
    if current:
        r = httpx.post(f"{url}/api/v2/query", params={"org": org}, headers={"Authorization": f"Token {current}",
                       "Content-Type": "application/vnd.flux"},
                       content=f'from(bucket: "{bucket}") |> range(start: -1m) |> limit(n: 1)', timeout=30)
        if r.status_code == 200:
            print(f"{var}: present and valid")
            return
        print(f"{var}: not valid here (HTTP {r.status_code}), creating a new one")
    if not admin:
        sys.exit("influx-setup needs the write/admin token (INFLUX_TOKEN)")
    h = {"Authorization": f"Token {admin}"}
    orgs = httpx.get(f"{url}/api/v2/orgs", params={"org": org}, headers=h, timeout=30).json().get("orgs") or []
    buckets = httpx.get(f"{url}/api/v2/buckets", params={"name": bucket, "org": org}, headers=h,
                        timeout=30).json().get("buckets") or []
    if not orgs or not buckets:
        sys.exit(f"InfluxDB at {url}: org {org!r} or bucket {bucket!r} not found")
    org_id, bucket_id = orgs[0]["id"], buckets[0]["id"]
    r = httpx.post(f"{url}/api/v2/authorizations", headers=h, timeout=30, json={
        "orgID": org_id, "description": "observer: read-only (agent tools, exports)",
        "permissions": [{"action": "read", "resource": {"type": "buckets", "id": bucket_id, "orgID": org_id}}]})
    r.raise_for_status()
    token = r.json()["token"]
    env_path = os.path.join(os.path.dirname(os.path.abspath(args.config)), ".env")
    lines = open(env_path, encoding="utf-8").read().splitlines() if os.path.exists(env_path) else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{var}=")] + [f"{var}={token}"]
    with open(env_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    print(f"{var}: created (read access to bucket {bucket}) and stored in {env_path}")


def cmd_state(cfg: dict, args) -> None:
    from .state import export_state, import_state

    if args.action == "export":
        out = args.file or time.strftime("observer-state-%Y%m%d-%H%M%S.tgz")
        data = export_state(cfg)
        with open(out, "wb") as fp:
            fp.write(data)
        print(f"{out}: {len(data)} bytes")
    else:
        if not args.file:
            sys.exit("state import FILE")
        with open(args.file, "rb") as fp:
            print(import_state(cfg, fp.read(), cfg["config_path"]))
        print("restart a running observer (it reads the state at start)")


def cmd_mcp(cfg: dict, args) -> None:
    from .agent.mcp_server import serve

    logging.getLogger().handlers.clear()            # stdout carries the protocol
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    serve(cfg)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="observer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.environ.get("OBSERVER_CONFIG")
                        or os.path.join(observer_home(), "config.yaml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--home", default=None)
    p.add_argument("--profile", choices=["dev", "release"], default="dev",
                   help="release: the configuration for docker compose (mqtt.home, influxdb:8086)")
    p = sub.add_parser("serve")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p = sub.add_parser("listen")
    p.add_argument("--seconds", type=float, default=120)
    p = sub.add_parser("catalog")
    p.add_argument("--unregistered", action="store_true")
    p.add_argument("--source", default="")
    p = sub.add_parser("run")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("propose")
    p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("accept")
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.7)
    sub.add_parser("sandbox-setup")
    p = sub.add_parser("library")
    p.add_argument("action", nargs="?", default="list", choices=["list", "export", "import", "remove-pack"])
    p.add_argument("file", nargs="?", default=None, help="archive to import, or pack name to remove")
    p.add_argument("--all", action="store_true")
    p.add_argument("--item", action="append", default=[], help="KIND/NAME, e.g. tools/on_periods")
    p.add_argument("--name", default="")
    p.add_argument("--version", default="")
    p.add_argument("-o", "--output", default="")
    p = sub.add_parser("test-tool")
    p.add_argument("name")
    p = sub.add_parser("agent")
    p.add_argument("--skill", default=None)
    p.add_argument("question", nargs="*")
    sub.add_parser("mcp")
    sub.add_parser("influx-setup")
    p = sub.add_parser("state")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("file", nargs="?", default=None)
    p = sub.add_parser("db")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("file", nargs="?", default=None)
    p.add_argument("-o", "--output", default="")
    p.add_argument("--start", default=None, help="ISO time (UTC unless an offset is given)")
    p.add_argument("--stop", default=None)
    p.add_argument("--dry-run", action="store_true", help="import: count what would be written, write nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "init":
        cmd_init(os.path.abspath(os.path.expanduser(args.home)) if args.home else observer_home(), args.profile)
        return
    cfg = load_config(args.config)
    {"serve": cmd_serve, "listen": cmd_listen, "catalog": cmd_catalog, "run": cmd_run, "propose": cmd_propose,
     "accept": cmd_accept, "sandbox-setup": cmd_sandbox_setup, "library": cmd_library, "test-tool": cmd_test_tool,
     "agent": cmd_agent,
     "mcp": cmd_mcp, "db": cmd_db, "influx-setup": cmd_influx_setup,
     "state": cmd_state}[args.command](cfg, args)


if __name__ == "__main__":
    main()
