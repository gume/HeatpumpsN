"""Live check of the controller's access protection (security.h).

    HPN_URL=http://localhost:30080 HPN_MQTT=localhost:31883 \
        floor_simulator/.venv/bin/python firmware/test/live/security_check.py

Needs keys from tools/gen_keys.sh and a controller with the default password
and no keys installed. It changes the device state while it runs (password,
keys, MPC floor set) and removes both keys again at the end.
"""
import asyncio, hashlib, json, os, subprocess, sys, tempfile, time, urllib.parse
import httpx, websockets
FW = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FW + "/tools")
from hpn_sign import sign_bytes
URL = os.environ.get("HPN_URL", "http://localhost:30080")
WS = URL.replace("http", "ws", 1)
MQ_HOST, _, MQ_PORT = os.environ.get("HPN_MQTT", "localhost:31883").partition(":")
OTHER = os.path.join(tempfile.mkdtemp(), "other.pem")
ok_all = True
def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
auth = httpx.DigestAuth("admin", "admin")
c = httpx.Client(base_url=URL, auth=auth, timeout=20)
anon = httpx.Client(base_url=URL, timeout=20)
uptime0 = c.get("/api/status").json()["uptime"]

# --- login
for path in ("/", "/api/state", "/api/config", "/api/mpc", "/api/security"):
    check(f"no login -> 401 {path}", anon.get(path).status_code == 401)
check("wrong password -> 401", httpx.get(URL + "/api/state", auth=httpx.DigestAuth("admin", "nope")).status_code == 401)
check("login -> 200 /api/state", c.get("/api/state").status_code == 200)
files = {"firmware": ("x.bin", b"\x00" * 4096, "application/octet-stream")}
r = anon.post("/api/update", files=files)
check("anonymous upload rejected", r.status_code == 401, r.text[:80])

# --- websocket token
async def ws_try(url):
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            m = await asyncio.wait_for(ws.recv(), 5)
            return len(m)
    except Exception as e:
        return f"{type(e).__name__}"
tok = c.get("/api/session").json()["wsToken"]
check("ws without token refused", isinstance(asyncio.run(ws_try(WS + "/ws")), str))
check("ws with wrong token refused", isinstance(asyncio.run(ws_try(WS + "/ws?token=" + "0" * 32)), str))
n = asyncio.run(ws_try(WS + "/ws?token=" + tok))
check("ws with session token streams state", isinstance(n, int) and n > 1000, f"{n} bytes")

# --- password
r = c.post("/api/security/password", json={"current": "wrong", "new": "test1234"})
check("password change needs current password", r.status_code == 400, r.json().get("error"))
r = c.post("/api/security/password", json={"current": "admin", "new": "test1234"})
check("password changed", r.status_code == 200 and not r.json()["defaultPassword"])
check("old password now refused", c.get("/api/state").status_code == 401)
c2 = httpx.Client(base_url=URL, auth=httpx.DigestAuth("admin", "test1234"), timeout=20)
check("new password accepted", c2.get("/api/state").status_code == 200)
r = c2.post("/api/security/password", json={"current": "test1234", "new": "admin"})
check("password restored to admin", r.status_code == 200 and r.json()["defaultPassword"])

# --- OTA key
pem = open(FW + "/keys/ota_public.pem").read()
r = c.post("/api/security/key", json={"slot": "ota", "pem": "-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----\n"})
check("garbage key rejected", r.status_code == 400, r.json().get("error"))
r = c.post("/api/security/key", json={"slot": "ota", "pem": pem})
st = r.json()
check("OTA key installed", st["ota"]["keyInstalled"] and st["ota"]["signatureRequired"], st["ota"]["fingerprint"])
bogus = os.urandom(8192)
def upload(data, query):
    return c.post("/api/update?" + urllib.parse.urlencode(query), files={"firmware": ("f.bin", data, "application/octet-stream")})
r = upload(bogus, {})
check("unsigned upload rejected", r.status_code == 500 and "signed image required" in r.text, r.json().get("error"))
other_key = subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"], capture_output=True).stdout
open(OTHER, "wb").write(other_key)
r = upload(bogus, {"sha256": hashlib.sha256(bogus).hexdigest(), "sig": sign_bytes(bogus, OTHER)})
check("image signed with another key rejected", r.status_code == 500 and "signature invalid" in r.text, r.json().get("error"))
signed_other = os.urandom(8192)
r = upload(bogus, {"sha256": hashlib.sha256(signed_other).hexdigest(), "sig": sign_bytes(signed_other, FW + "/keys/ota_private.pem")})
check("valid signature but different bytes rejected", r.status_code == 500, r.json().get("error"))
r = upload(bogus, {"sha256": hashlib.sha256(bogus).hexdigest(), "sig": sign_bytes(bogus, FW + "/keys/ota_private.pem")})
check("correctly signed bogus image passes the signature gate", r.status_code == 500 and "sign" not in r.text and "match" not in r.text, r.json().get("error"))
r = c.post("/api/security/unsigned", json={"minutes": 15})
check("unsigned window opened", not r.json()["ota"]["signatureRequired"] and r.json()["ota"]["unsignedRemainingS"] > 800)
r = upload(bogus, {})
check("unsigned image passes the gate in the window", r.status_code == 500 and "sign" not in r.text, r.json().get("error"))
r = c.post("/api/security/unsigned", json={"minutes": 0})
check("enforcement re-armed", r.json()["ota"]["signatureRequired"])
check("no reboot during rejection tests", c.get("/api/status").json()["uptime"] >= uptime0)

# --- observer
obs_pem = open(FW + "/keys/observer_public.pem").read()
r = c.post("/api/security/key", json={"slot": "observer", "pem": obs_pem})
check("observer key installed", r.json()["observer"]["keyInstalled"], r.json()["observer"]["fingerprint"])
rev0 = c.get("/api/mpc").json()["params"]["floor"]["revision"]
r = c.post("/api/mpc/params", json={"set": "floor", "delay_s": 41})
check("unsigned params rejected (REST)", r.status_code == 400 and "signed envelope" in r.text, r.json().get("error"))
def env(cmd, key=FW + "/keys/observer_private.pem", seq=None):
    cmd = dict(cmd, seq=seq or int(time.time() * 1000))
    msg = json.dumps(cmd, separators=(",", ":"))
    return {"msg": msg, "sig": sign_bytes(msg.encode(), key)}
e1 = env({"set": "floor", "delay_s": 41})
r = c.post("/api/mpc/params", json=e1)
check("signed params accepted (REST)", r.status_code == 200 and r.json()["params"]["floor"]["delay_s"] == 41, f"rev {rev0}->{r.json()['params']['floor']['revision']}")
r = c.post("/api/mpc/params", json=e1)
check("replayed envelope rejected", r.status_code == 400 and "seq" in r.text, r.json().get("error"))
e2 = env({"set": "floor", "delay_s": 42})
e2["msg"] = e2["msg"].replace("42", "60")
r = c.post("/api/mpc/params", json=e2)
check("tampered msg rejected", r.status_code == 400 and "signature invalid" in r.text, r.json().get("error"))
r = c.post("/api/mpc/params", json=env({"set": "floor", "delay_s": 43}, key=OTHER))
check("envelope signed by another key rejected", r.status_code == 400, r.json().get("error"))
# MQTT path
import paho.mqtt.client as mqtt
acks = []
m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
m.on_message = lambda cl, u, msg: acks.append(json.loads(msg.payload))
m.connect(MQ_HOST, int(MQ_PORT or 1883)); m.subscribe("heatpumpsn/controller/state/mpc/ack"); m.loop_start(); time.sleep(1)
m.publish("heatpumpsn/controller/cmd/mpc/params", json.dumps({"set": "floor", "delay_s": 44}), qos=1); time.sleep(4)
m.publish("heatpumpsn/controller/cmd/mpc/params", json.dumps(env({"set": "floor", "delay_s": 40, "reset": True})), qos=1); time.sleep(4)
m.publish("heatpumpsn/controller/cmd/mpc/mode", "legacy", qos=1); time.sleep(4)
m.publish("heatpumpsn/controller/cmd/mpc/mode", json.dumps(env({"mode": "mpc"})), qos=1); time.sleep(4)
m.loop_stop()
print("   acks:", acks)
check("MQTT unsigned params rejected", any(not a["ok"] and a["what"] == "params" for a in acks))
check("MQTT signed params accepted", any(a["ok"] and a["what"] == "params" for a in acks))
check("MQTT unsigned mode rejected", any(not a["ok"] and a["what"] == "mode" for a in acks))
check("MQTT signed mode accepted", any(a["ok"] and a["what"] == "mode" for a in acks))
mp = c.get("/api/mpc").json()
check("params reset by signed MQTT command", mp["params"]["floor"]["delay_s"] == 40 and mp["status"]["mode"] == "mpc")
for slot in ("ota", "observer"):
    c.post("/api/security/key", json={"slot": slot, "pem": ""})
print("ALL PASS" if ok_all else "SOME FAILED")
