"""From identified mixer models to parameter sets on the controller, with the owner's approval.

* **Snapshot**: the controller publishes each MPC parameter set retained on
  ``<controller>/state/mpc/params/<set>``; the observer keeps the latest whole
  JSON (``data/mpc_params.json``), because the law (two lists) is not stored
  as samples.
* **Identification** (``identify.py``), on demand or scheduled (check type
  ``identify``): every run is kept in ``data/identification.jsonl``; a
  scenario whose re-identified model predicts clearly better on held-out data
  becomes a **proposal**. A large gap raises a supervision alert ("mixer model
  drift").
* **Approval and sending**: nothing is sent automatically. The owner approves
  a proposal in the portal; only the model fields (law, delay, lags) are sent
  to ``<controller>/cmd/mpc/params``, as an HMAC-signed envelope when the
  controller has a command secret (``OBSERVER_CONTROLLER_SECRET``). Sending
  must be enabled in config.yaml (``parameters.push_enabled``). The controller
  validates the set (all or nothing, compiled bounds) and answers on
  ``state/mpc/ack``; the proposal becomes *accepted* or *rejected*.
* **Versioning and rollback**: every sent set keeps the set it replaced; *Roll
  back* sends that one again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid

from . import identify

log = logging.getLogger(__name__)
MODEL_FIELDS = identify.MODEL_FIELDS


def sign_envelope(command: dict, secret: str) -> str:
    """The controller's signed command: {"msg": "<json with a rising seq>", "sig": hex HMAC-SHA256}."""
    msg = json.dumps(dict(command, seq=int(time.time() * 1000)), separators=(",", ":"))
    return json.dumps({"msg": msg, "sig": hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()})


class ParameterManager:
    def __init__(self, service, cfg: dict):
        self.service, self.cfg = service, cfg
        p = cfg.get("parameters") or {}
        self.controller = (p.get("controller_topic") or "heatpumpsn/controller").strip("/")
        self.push_enabled = bool(p.get("push_enabled", False))
        self.secret_env = p.get("command_secret_env", "OBSERVER_CONTROLLER_SECRET")
        self.window = p.get("window", "-14d")
        self.min_slow_hours = float(p.get("min_slow_hours", 6))
        self.drift_gain = float(p.get("drift_gain", 0.3))
        data = os.path.dirname(cfg["paths"]["catalog"])
        self.snapshot_path = os.path.join(data, "mpc_params.json")
        self.runs_path = os.path.join(data, "identification.jsonl")
        self.proposals_path = os.path.join(data, "parameter_proposals.json")
        self._lock = threading.RLock()
        self.snapshot: dict = self._load(self.snapshot_path, {})
        self.proposals: list = self._load(self.proposals_path, [])
        self.last_ack: dict | None = None
        self.job: dict | None = None

    # --- files ---------------------------------------------------------------------------
    @staticmethod
    def _load(path, default):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                return json.load(fp)
        return default

    def _save(self, path, data) -> None:
        with open(path + ".tmp", "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    # --- the controller's messages ------------------------------------------------------------
    def on_raw(self, topic: str, payload: bytes, retained: bool) -> None:
        if not topic.startswith(self.controller + "/state/mpc/"):
            return
        rest = topic[len(self.controller) + len("/state/mpc/"):]
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if rest.startswith("params/") and isinstance(data, dict) and data.get("xk"):
            with self._lock:
                self.snapshot[rest[len("params/"):]] = {"params": data, "received": time.strftime("%Y-%m-%d %H:%M:%S")}
                self._save(self.snapshot_path, self.snapshot)
        elif rest == "ack" and isinstance(data, dict) and not retained:
            self.last_ack = {**data, "received": time.strftime("%Y-%m-%d %H:%M:%S")}
            if data.get("what") == "params":
                self._ack(data)

    def _ack(self, ack: dict) -> None:
        with self._lock:
            waiting = [p for p in self.proposals if p["status"] == "sent"]
            if not waiting:
                return
            p = waiting[-1]
            p["ack"] = ack
            p["status"] = "accepted" if ack.get("ok") else "rejected by the controller"
            p["answered"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save(self.proposals_path, self.proposals)
        log.info("controller answered the parameter set %s: %s", p["id"], p["status"])

    def current_sets(self) -> dict:
        return {k: v["params"] for k, v in self.snapshot.items()}

    # --- identification -------------------------------------------------------------------------
    def identify_now(self) -> dict:
        """Run the identification (blocking); creates proposals and drift alerts."""
        data = self.service.agents.data
        if data is None:
            raise RuntimeError("no read-only database token (OBSERVER_INFLUX_READ_TOKEN)")
        sets = self.current_sets()
        if not sets:
            raise RuntimeError("the controller's parameter sets are not known yet (state/mpc/params/<set> not seen)")
        g = identify.fetch(data, self.window, source="slow")
        slow_hours = sum(s["hours"] for s in identify.segments(g))
        source = "slow"
        if slow_hours < self.min_slow_hours:            # not enough exact inputs yet: the published values
            g = identify.fetch(data, self.window, source="state")
            source = "state"
        result = identify.identify(g, sets)
        result.update(source=source, inputs="exact (slow log)" if source == "slow" else
                      f"approximate (published values; the slow log has {slow_hours:.1f} h so far)")
        with open(self.runs_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps({k: v for k, v in result.items()}, ensure_ascii=False) + "\n")
        self._proposals_from(result, g)
        self._drift(result)
        return result

    def _proposals_from(self, result: dict, g: dict) -> None:
        with self._lock:
            for scen, r in result["scenarios"].items():
                if r.get("status") != "proposal":
                    continue
                for old in self.proposals:                  # one open proposal per scenario
                    if old["scenario"] == scen and old["status"] == "proposed":
                        old["status"] = "superseded"
                self.proposals.append({
                    "id": uuid.uuid4().hex[:10], "created": result["time"], "scenario": scen,
                    "status": "proposed", "inputs": result["inputs"], "hours": r["hours"],
                    "train_h": r["train_h"], "holdout_h": r["holdout_h"], "rmse": r["rmse"],
                    "current": r["current"], "identified": r["identified"], "coverage_min": r["coverage_min"],
                    "plot": self._plot(g, scen, r)})
            self._save(self.proposals_path, self.proposals)

    @staticmethod
    def _plot(g: dict, scenario: str, r: dict, every_s: int = 60) -> dict:
        """Measured vs predicted T_fwd on the held-out data, for the portal (one point a minute)."""
        _, hold = identify.split_holdout([s for s in identify.segments(g) if s["scenario"] == scenario])
        step = max(1, int(every_s / identify.DT))
        out = {"t": [], "measured": [], "current": [], "identified": []}
        for s in hold:
            cur = identify.simulate(s, r["current"], g)
            new = identify.simulate(s, r["identified"], g)
            meas = g["fwd"][s["slice"]]
            t = g["t"][s["slice"]]
            for i in range(0, len(t), step):
                out["t"].append(float(t[i]))
                out["measured"].append(round(float(meas[i]), 2))
                out["current"].append(round(float(cur[i]), 2))
                out["identified"].append(round(float(new[i]), 2))
        return out

    def _drift(self, result: dict) -> None:
        sup = getattr(self.service, "_supervisor", None)
        if sup is None:
            return
        check = {"id": "mixer_model_drift", "severity": "warning"}
        for scen, r in result["scenarios"].items():
            rm = r.get("rmse")
            if not rm:
                continue
            gain = (rm["holdout_current"] - rm["holdout_identified"]) / rm["holdout_current"] if rm["holdout_current"] else 0
            if r.get("status") == "proposal" and gain >= self.drift_gain:
                sup._raise(check, scen, f"Mixer model drift ({scen}): the current parameters predict "
                           f"{rm['holdout_current']:.2f} K off, re-identified ones {rm['holdout_identified']:.2f} K",
                           "a parameter proposal waits on the Parameters tab")
            else:
                sup._resolve(check, scen)

    def start_identification(self) -> dict:
        with self._lock:
            if self.job and self.job["state"] == "running":
                raise RuntimeError("an identification is already running")
            self.job = {"state": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S"), "t0": time.time()}
        job = self.job

        def work():
            try:
                job["result"] = self.identify_now()
                job["state"] = "done"
            except Exception as exc:
                job.update(state="error", error=f"{type(exc).__name__}: {exc}")
            job["seconds"] = round(time.time() - job["t0"], 1)

        threading.Thread(target=work, name="identification", daemon=True).start()
        return self.view()["job"]

    def last_run(self) -> dict | None:
        if not os.path.exists(self.runs_path):
            return None
        last = None
        with open(self.runs_path, encoding="utf-8") as fp:
            for line in fp:
                if line.strip():
                    last = line
        return json.loads(last) if last else None

    # --- approval, sending, rollback ---------------------------------------------------------------
    def _proposal(self, pid: str) -> dict:
        p = next((x for x in self.proposals if x["id"] == pid), None)
        if p is None:
            raise KeyError(pid)
        return p

    def command(self, scenario: str, params: dict) -> dict:
        return {"set": scenario, **{k: params[k] for k in MODEL_FIELDS}, "persist": True}

    def _send(self, command: dict) -> str:
        if not self.push_enabled:
            raise RuntimeError("sending parameters is disabled (config.yaml: parameters.push_enabled)")
        mqtt = getattr(self.service, "mqtt", None)
        if not (mqtt and mqtt.connected):
            raise RuntimeError("not connected to the MQTT broker")
        secret = os.environ.get(self.secret_env, "")
        payload = sign_envelope(command, secret) if secret else json.dumps(command, separators=(",", ":"))
        mqtt.publish(f"{self.controller}/cmd/mpc/params", payload)
        return "signed" if secret else "unsigned"

    def approve(self, pid: str) -> dict:
        with self._lock:
            p = self._proposal(pid)
            if p["status"] != "proposed":
                raise ValueError(f"this proposal is {p['status']}")
            previous = self.snapshot.get(p["scenario"], {}).get("params")
            if previous is None:
                raise RuntimeError(f"the controller's current {p['scenario']} set is not known")
            mode = self._send(self.command(p["scenario"], p["identified"]))
            p.update(status="sent", sent=time.strftime("%Y-%m-%d %H:%M:%S"), signed=mode,
                     previous={k: previous[k] for k in MODEL_FIELDS})
            self._save(self.proposals_path, self.proposals)
        log.warning("parameter set %s (%s) sent to the controller (%s)", pid, p["scenario"], mode)
        return p

    def rollback(self, pid: str) -> dict:
        with self._lock:
            p = self._proposal(pid)
            if p["status"] not in ("accepted", "sent") or not p.get("previous"):
                raise ValueError("only a sent or accepted set can be rolled back")
            mode = self._send(self.command(p["scenario"], p["previous"]))
            entry = {"id": uuid.uuid4().hex[:10], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "scenario": p["scenario"], "status": "sent", "rollback_of": pid, "signed": mode,
                     "sent": time.strftime("%Y-%m-%d %H:%M:%S"), "identified": p["previous"],
                     "previous": p["identified"], "inputs": "rollback"}
            p["status"] = "rolled back"
            self.proposals.append(entry)
            self._save(self.proposals_path, self.proposals)
        return entry

    def dismiss(self, pid: str) -> None:
        with self._lock:
            p = self._proposal(pid)
            if p["status"] != "proposed":
                raise ValueError(f"this proposal is {p['status']}")
            p["status"] = "dismissed"
            self._save(self.proposals_path, self.proposals)

    def check_timeouts(self, after_s: float = 180) -> None:
        """A sent set without an answer from the controller is marked."""
        now = time.time()
        with self._lock:
            changed = False
            for p in self.proposals:
                if p["status"] == "sent" and now - time.mktime(time.strptime(p["sent"], "%Y-%m-%d %H:%M:%S")) > after_s:
                    p["status"] = "no answer from the controller"
                    changed = True
            if changed:
                self._save(self.proposals_path, self.proposals)

    def view(self) -> dict:
        job = dict(self.job) if self.job else None
        if job:
            job.pop("t0", None)
            job.pop("result", None)
        return {"snapshot": self.snapshot, "proposals": list(reversed(self.proposals)), "last_run": self.last_run(),
                "job": job, "last_ack": self.last_ack,
                "push": {"enabled": self.push_enabled, "signed": bool(os.environ.get(self.secret_env)),
                         "topic": f"{self.controller}/cmd/mpc/params", "secret_env": self.secret_env}}
