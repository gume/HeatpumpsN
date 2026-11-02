"""The data service for sandboxed code: a Unix socket, one per run.

The sandbox has no network. To reach the recorded data its code connects to
this socket (bound into the sandbox, ``OBS_SOCKET``) with the ``obs`` client
library: one JSON request per connection, one JSON answer. The service runs
inside the observer, checks every request (signal ids against the catalog,
times and windows against strict patterns, size limits) and reads InfluxDB
with the read-only token; the code never sees a token or a database address.
Every request is appended to the run's ``queries.jsonl`` for the audit.

Operations: find, coverage, series, events, house, tools, tool_used.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time

MAX_REQUEST = 1 << 20
MAX_VALUES_PER_REQUEST = 5_000_000
MAX_REQUESTS = 2_000


class DataBroker:
    def __init__(self, toolbox, library, log_path: str | None = None):
        self.toolbox, self.library, self.log_path = toolbox, library, log_path
        # A short path: AF_UNIX paths are limited to 108 bytes.
        self.dir = tempfile.mkdtemp(prefix="obs-")
        self.path = os.path.join(self.dir, "data.sock")
        self.requests = 0
        self.tools_used: dict[str, bool] = {}
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> "DataBroker":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, name="obs-data", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
        for p in (self.path,):
            if os.path.exists(p):
                os.unlink(p)
        os.rmdir(self.dir)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(60)
            buf = b""
            try:
                while len(buf) <= MAX_REQUEST:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf or b"{}")
                reply = {"result": self.dispatch(str(req.get("op", "")), req.get("args") or {})}
            except Exception as exc:          # the answer carries the error to the sandbox
                reply = {"error": f"{type(exc).__name__}: {exc}" if not isinstance(exc, ValueError) else str(exc)}
            try:
                conn.sendall(json.dumps(reply, default=str).encode())
            except OSError:
                pass

    # --- operations ---------------------------------------------------------------
    def _log(self, op: str, args: dict, summary: dict) -> None:
        if not self.log_path:
            return
        with open(self.log_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "op": op, "args": args, **summary},
                                default=str) + "\n")

    def dispatch(self, op: str, args: dict):
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise ValueError(f"request limit of this run reached ({MAX_REQUESTS})")
        tb = self.toolbox
        if op == "find":
            out = tb.find_signals(**{k: v for k, v in args.items()
                                     if k in ("text", "location", "circuit", "role", "quantity", "source",
                                              "stored_only", "limit")})
            self._log(op, args, {"count": out["count"]})
            return out
        if op == "coverage":
            ids = tb._known(list(args.get("signals") or []))
            out = tb._need_data().coverage(ids)
            self._log(op, args, {"signals": len(out)})
            return out
        if op == "series":
            ids = tb._known(list(args.get("signals") or []))
            data = tb._need_data().series(ids, str(args.get("start", "-24h")), str(args.get("stop", "now")),
                                          str(args.get("every", "1m")), str(args.get("agg", "mean")),
                                          limit=MAX_VALUES_PER_REQUEST)
            total = sum(len(s.values) for s in data.values())
            if total > MAX_VALUES_PER_REQUEST:
                raise ValueError(f"{total} values: more than {MAX_VALUES_PER_REQUEST} in one request; "
                                 "use a coarser window or a shorter range")
            self._log(op, args, {"values": total})
            return {sig: {"t": [int(t.timestamp() * 1000) for t in s.times], "v": s.values}
                    for sig, s in data.items()}
        if op == "events":
            out = tb.get_events(**{k: v for k, v in args.items()
                                   if k in ("start", "stop", "min_severity", "text", "limit")})
            self._log(op, args, {"count": out.get("count", 0)})
            return out
        if op == "house":
            return tb.house_context()
        if op == "tools":
            return self.library.tool_index() if self.library else {}
        if op == "tool_used":
            name = str(args.get("name", ""))
            index = self.library.tool_index() if self.library else {}
            if name in index:
                self.tools_used[name] = bool(index[name]["audited"])
                self._log(op, args, {"audited": self.tools_used[name]})
            return {"ok": True}
        raise ValueError(f"unknown operation {op!r}")
