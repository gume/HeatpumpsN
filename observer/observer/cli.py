"""Command line: observer <command> [--config config.yaml]

  serve    [--dry-run] [--host H] [--port P]   ingest + web portal (http://H:P/), the normal way to run it
  listen   [--seconds N]   collect for N s (default 120): update the catalog, store nothing
  catalog  [--unregistered] [--source PREFIX]   show the inventory and how each signal is handled
  run      [--dry-run]     ingest continuously and store into InfluxDB
  propose  [--limit N]     ask the LLM to describe unregistered signals -> proposals.yaml
  accept   ID [ID ...] | --all [--min-confidence X]   merge proposals into registry.yaml
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


def load_config(path: str) -> dict:
    base = os.path.dirname(os.path.abspath(path))
    load_env(os.path.join(base, ".env"))
    with open(path, encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}
    # The LLM endpoint may come entirely from .env.
    llm = cfg.setdefault("llm", {}) or {}
    cfg["llm"] = llm
    llm["base_url"] = os.environ.get("OBSERVER_LLM_BASE_URL") or llm.get("base_url", "")
    llm["model"] = os.environ.get("OBSERVER_LLM_MODEL") or llm.get("model", "")
    paths = cfg.setdefault("paths", {})
    for key, default in (("catalog", "data/catalog.json"), ("registry", "registry.yaml"),
                         ("proposals", "data/proposals.yaml"), ("house_model", "house_model.yaml"),
                         ("house_model_proposal", "data/house_model_proposal.yaml")):
        value = paths.get(key, default)
        paths[key] = value if os.path.isabs(value) else os.path.join(base, value)
        os.makedirs(os.path.dirname(paths[key]), exist_ok=True)
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
    from .llm import LlmClient, propose

    client = LlmClient.from_config(cfg.get("llm"))
    if client is None:
        sys.exit("no LLM configured (llm.base_url and llm.model in the config)")
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="observer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.environ.get("OBSERVER_CONFIG", "config.yaml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    {"serve": cmd_serve, "listen": cmd_listen, "catalog": cmd_catalog, "run": cmd_run, "propose": cmd_propose,
     "accept": cmd_accept}[args.command](cfg, args)


if __name__ == "__main__":
    main()
