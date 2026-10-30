"""Live check of the controller's access protection (security.h).

    HPN_URL=http://localhost:30080 HPN_MQTT=localhost:31883 \\
        floor_simulator/.venv/bin/python firmware/test/live/security_check.py

Needs a controller with the default password and no command secret. It
changes the device state while it runs (password, secret, upload window, MPC
floor set) and restores the defaults at the end.
"""
import asyncio
import json
import os
import sys
import time

import httpx
import websockets

FW = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(FW, "tools"))
from hpn_sign import sign_msg  # noqa: E402

URL = os.environ.get("HPN_URL", "http://localhost:30080")
WS = URL.replace("http", "ws", 1)
MQ_HOST, _, MQ_PORT = os.environ.get("HPN_MQTT", "localhost:31883").partition(":")
SECRET = "bench-secret-0123456789"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


c = httpx.Client(base_url=URL, auth=httpx.DigestAuth("admin", "admin"), timeout=20)
anon = httpx.Client(base_url=URL, timeout=20)
uptime0 = c.get("/api/status").json()["uptime"]

# --- login
for path in ("/", "/api/state", "/api/config", "/api/mpc", "/api/security"):
    check(f"no login -> 401 {path}", anon.get(path).status_code == 401)
check("wrong password -> 401", httpx.get(URL + "/api/state", auth=httpx.DigestAuth("admin", "nope")).status_code == 401)
check("login -> 200", c.get("/api/state").status_code == 200)


async def ws_try(url):
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            return len(await asyncio.wait_for(ws.recv(), 5))
    except Exception as e:  # noqa: BLE001
        return type(e).__name__

tok = c.get("/api/session").json()["wsToken"]
check("ws without token refused", isinstance(asyncio.run(ws_try(WS + "/ws")), str))
check("ws with wrong token refused", isinstance(asyncio.run(ws_try(WS + "/ws?token=" + "0" * 32)), str))
n = asyncio.run(ws_try(WS + "/ws?token=" + tok))
check("ws with session token streams state", isinstance(n, int) and n > 1000, f"{n} bytes")

r = c.post("/api/security/password", json={"current": "wrong", "new": "test1234"})
check("password change needs current password", r.status_code == 400, r.json().get("error"))
r = c.post("/api/security/password", json={"current": "admin", "new": "test1234"})
check("password changed", r.status_code == 200 and not r.json()["defaultPassword"])
check("old password refused", c.get("/api/state").status_code == 401)
c2 = httpx.Client(base_url=URL, auth=httpx.DigestAuth("admin", "test1234"), timeout=20)
r = c2.post("/api/security/password", json={"current": "test1234", "new": "admin"})
check("new password works; restored to admin", r.status_code == 200 and r.json()["defaultPassword"])

# --- upload window (bogus images fail in the updater, so nothing reboots)
bogus = {"firmware": ("f.bin", os.urandom(8192), "application/octet-stream")}
r = anon.post("/api/update", files=bogus)
check("anonymous upload rejected", r.status_code == 401)
r = c.post("/api/update", files=bogus)
check("upload refused while closed", r.status_code == 500 and "closed" in r.text, r.json().get("error"))
r = c.post("/api/security/uploads", json={"minutes": 10})
check("window opened", r.json()["uploads"]["open"] and r.json()["uploads"]["remainingS"] > 500)
r = c.post("/api/update", files=bogus)
check("upload reaches the updater while open", r.status_code == 500 and "closed" not in r.text, r.json().get("error"))
r = c.post("/api/security/uploads", json={"minutes": 0})
check("window closed again", not r.json()["uploads"]["open"])
r = c.post("/api/update?target=fs", files=bogus)
check("filesystem upload refused while closed", r.status_code == 500 and "closed" in r.text)
check("no reboot during the upload tests", c.get("/api/status").json()["uptime"] >= uptime0)

# --- command secret
r = c.post("/api/security/secret", json={"secret": "short"})
check("short secret rejected", r.status_code == 400, r.json().get("error"))
r = c.post("/api/security/secret", json={"secret": SECRET})
check("secret set", r.json()["commands"]["secretSet"], r.json()["commands"]["fingerprint"])


def env(cmd, secret=SECRET):
    msg = json.dumps(dict(cmd, seq=int(time.time() * 1000)), separators=(",", ":"))
    return {"msg": msg, "sig": sign_msg(msg, secret)}

r = c.post("/api/mpc/params", json={"set": "floor", "delay_s": 41})
check("unsigned params rejected", r.status_code == 400 and "signed envelope" in r.text, r.json().get("error"))
e1 = env({"set": "floor", "delay_s": 41})
r = c.post("/api/mpc/params", json=e1)
check("signed params accepted", r.status_code == 200 and r.json()["params"]["floor"]["delay_s"] == 41)
r = c.post("/api/mpc/params", json=e1)
check("replay rejected", r.status_code == 400 and "seq" in r.text, r.json().get("error"))
e2 = env({"set": "floor", "delay_s": 42})
e2["msg"] = e2["msg"].replace("42", "60")
r = c.post("/api/mpc/params", json=e2)
check("tampered msg rejected", r.status_code == 400 and "signature invalid" in r.text)
r = c.post("/api/mpc/params", json=env({"set": "floor", "delay_s": 43}, secret="another-secret-0123456"))
check("wrong secret rejected", r.status_code == 400 and "signature invalid" in r.text)

import paho.mqtt.client as mqtt  # noqa: E402
acks = []
m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
m.on_message = lambda cl, u, msg: acks.append(json.loads(msg.payload))
m.connect(MQ_HOST, int(MQ_PORT or 1883))
m.subscribe("heatpumpsn/controller/state/mpc/ack")
m.loop_start()
time.sleep(1)
# Envelopes are built at send time: two in the same millisecond share a seq.
for topic, payload in (("params", lambda: json.dumps({"set": "floor", "delay_s": 44})),
                       ("params", lambda: json.dumps(env({"set": "floor", "reset": True}))),
                       ("mode", lambda: "legacy"),
                       ("mode", lambda: json.dumps(env({"mode": "mpc"})))):
    m.publish(f"heatpumpsn/controller/cmd/mpc/{topic}", payload(), qos=1)
    time.sleep(4)
m.loop_stop()
mq = [a for a in acks if a["source"] == "mqtt"]
check("MQTT: unsigned params rejected, signed accepted",
      [a["ok"] for a in mq if a["what"] == "params"] == [False, True])
check("MQTT: unsigned mode rejected, signed accepted",
      [a["ok"] for a in mq if a["what"] == "mode"] == [False, True])
mp = c.get("/api/mpc").json()
check("signed MQTT reset applied", mp["params"]["floor"]["delay_s"] == 40 and mp["status"]["mode"] == "mpc")

c.post("/api/security/secret", json={"secret": ""})
print("ALL PASS" if ok_all else "SOME FAILED")
