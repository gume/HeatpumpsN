"""Live check of the controller's HPN1 access protection (docs/security.md).

    HPN_URL=localhost:30080 HPN_MQTT=localhost:31883 \\
        python3 firmware/test/live/security_check.py [path/to/firmware.bin]

Needs a controller with factory passwords (admin/admin, monitor/monitor) and
no MQTT command secret. It changes the device state while it runs and
restores it at the end. Standard library only (paho-mqtt for the MQTT part).
With a firmware.bin it also checks that a valid image with a wrong signed
hash is refused without a reboot. Takes about 1.5 minutes (lockout wait).
"""
import hashlib
import json
import os
import socket
import base64
import sys
import time
import urllib.error
import urllib.request
import uuid

FW = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(FW, "tools"))
import hpn_auth  # noqa: E402
from hpn_auth import Client  # noqa: E402
from hpn_sign import sign_msg  # noqa: E402

HOST = os.environ.get("HPN_URL", "localhost:30080").replace("http://", "")
MQ_HOST, _, MQ_PORT = os.environ.get("HPN_MQTT", "localhost:31883").partition(":")
SECRET = "bench-secret-0123456789"
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def raw(method, path, headers=None, data=None):
    req = urllib.request.Request(f"http://{HOST}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode(errors="replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers


def ws_handshake(path_query):
    """Status line of a WebSocket upgrade (101 = accepted)."""
    host, _, port = HOST.partition(":")
    s = socket.create_connection((host, int(port or 80)), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(f"GET {path_query} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
              f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
    line = s.recv(256).split(b"\r\n")[0].decode(errors="replace")
    s.close()
    return line


admin = Client(HOST, "admin", "admin")
monitor = Client(HOST, "monitor", "monitor")
uptime0 = admin.request("GET", "/api/status")[1]["uptime"]

# --- challenge and basic signing
st, body, _ = raw("GET", "/api/auth/challenge?user=root")
check("challenge for unknown user refused", st == 400)
st, body, _ = raw("GET", "/")
check("static portal page is public", st == 200)
st, body, h = raw("GET", "/api/state")
check("unsigned API request -> 401", st == 401 and h.get("X-HPN-Reason") == "header")
st, body = admin.request("GET", "/api/state")
check("signed GET as admin -> 200 with next nonce", st == 200 and admin.nonces)
st, _ = monitor.request("GET", "/api/state")
check("signed GET as monitor -> 200", st == 200)

# --- replay and tampering
nonce = admin.nonce()
hdr = admin.headers("GET", "/api/state", nonce)
check("first use of a signed request", raw("GET", "/api/state", hdr)[0] == 200)
st, _, h = raw("GET", "/api/state", hdr)
check("replayed request -> 401 (nonce spent)", st == 401 and h.get("X-HPN-Reason") == "nonce")
nonce = admin.nonce()
signed = json.dumps({"minutes": 0}).encode()
hdr = admin.headers("POST", "/api/security/uploads", nonce, signed)
hdr["Content-Type"] = "application/json"
st, _, h = raw("POST", "/api/security/uploads", hdr, json.dumps({"minutes": 60}).encode())
check("body changed after signing -> 401", st == 401 and h.get("X-HPN-Reason") == "sig")
nonce = admin.nonce()
hdr = admin.headers("GET", "/api/state?x=1", nonce)
check("query changed after signing -> 401", raw("GET", "/api/state?x=2", hdr)[0] == 401)
check("forged nonce -> 401", raw("GET", "/api/state", admin.headers("GET", "/api/state", "0" * 32))[0] == 401)

# --- monitor is read-only
st, reply = monitor.request("POST", "/api/security/uploads", {"minutes": 10})
check("monitor cannot open the upload window", st == 403, reply)
st, _ = monitor.request("GET", "/api/config/export")
check("monitor cannot export the configuration", st == 403)
st, _ = monitor.request("POST", "/api/mpc/mode", {"mode": "legacy"})
check("monitor cannot change the controller mode", st == 403)

# --- WebSocket
check("unsigned WebSocket refused", "101" not in ws_handshake("/ws"))
n = admin.nonce()
url = f"/ws?hpn_user=admin&hpn_nonce={n}&hpn_sig={hpn_auth.signature(admin.k_auth, 'GET', '/ws', n, hpn_auth.EMPTY_SHA256)}"
check("signed WebSocket accepted", "101" in ws_handshake(url))
check("replayed WebSocket URL refused", "101" not in ws_handshake(url))

# --- sealed secrets
st, reply = admin.request("POST", "/api/security/secret", {"secret": SECRET})
check("secret in clear refused", st == 400 and "sealed" in str(reply), reply)
nonce = admin.nonce()
sealed = hpn_auth.seal(admin.k_enc, nonce, json.dumps({"secret": SECRET}).encode())
bad = base64.b64decode(sealed)
bad = base64.b64encode(bad[:20] + bytes([bad[20] ^ 1]) + bad[21:]).decode()
data = json.dumps({"sealed": bad}).encode()
hdr = admin.headers("POST", "/api/security/secret", nonce, data)
hdr["Content-Type"] = "application/json"
st, body, _ = raw("POST", "/api/security/secret", hdr, data)
check("tampered sealed body refused", st == 400 and "integrity" in body, body)
st, reply = admin.request("POST", "/api/security/secret", sealed={"secret": SECRET})
check("sealed secret accepted", st == 200 and reply["commands"]["secretSet"], reply["commands"]["fingerprint"])
st, export = admin.request("GET", "/api/config/export", unseal_response=True)
check("configuration export arrives sealed and opens", st == 200 and "network" in export)

# --- password change (keys derived on the client)
salt = os.urandom(16).hex()
ka, ke = hpn_auth.derive_keys("monitor-new-pass", salt, 10000)
st, reply = admin.request("POST", "/api/security/password",
                          sealed={"user": "monitor", "salt": salt, "k_auth": ka.hex(), "k_enc": ke.hex()})
check("monitor password changed (sealed keys)", st == 200 and not reply["users"]["monitor"]["defaultPassword"])
check("old monitor password refused", Client(HOST, "monitor", "monitor").request("GET", "/api/state")[0] == 401)
check("new monitor password accepted", Client(HOST, "monitor", "monitor-new-pass").request("GET", "/api/state")[0] == 200)

# --- lockout after repeated failures
wrong = Client(HOST, "monitor", "wrong")
codes = [wrong.request("GET", "/api/state")[0] for _ in range(6)]
check("5 wrong passwords lock the user (423)", codes[:5] == [401] * 5 and codes[5] == 423, codes)
check("locked user refused even with the right password",
      Client(HOST, "monitor", "monitor-new-pass").request("GET", "/api/state")[0] == 423)
check("admin unaffected by the monitor lockout", admin.request("GET", "/api/state")[0] == 200)
time.sleep(31)
check("lock released after 30 s", Client(HOST, "monitor", "monitor-new-pass").request("GET", "/api/state")[0] == 200)
dsalt = hashlib.sha256(b"hpn-default-salt-monitor").digest()[:16].hex()
ka, ke = hpn_auth.derive_keys("monitor", dsalt, 10000)
st, reply = admin.request("POST", "/api/security/password",
                          sealed={"user": "monitor", "salt": dsalt, "k_auth": ka.hex(), "k_enc": ke.hex()})
check("monitor factory password restored", st == 200 and reply["users"]["monitor"]["defaultPassword"])


# --- uploads
def upload(client, image, declared_hash, target=None):
    q = f"sha256={declared_hash}" + ("&target=fs" if target else "")
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"f.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + image + f"\r\n--{b}--\r\n".encode()
    return client.request("POST", f"/api/update?{q}", raw=body, body_hash="multipart",
                          content_type=f"multipart/form-data; boundary={b}")

bogus = os.urandom(8192)
st, reply = upload(admin, bogus, hashlib.sha256(bogus).hexdigest())
check("upload refused while the window is closed", st == 500 and "closed" in str(reply), reply)
check("monitor upload refused", upload(monitor, bogus, hashlib.sha256(bogus).hexdigest())[0] in (401, 403, 500))
admin.request("POST", "/api/security/uploads", {"minutes": 10})
st, reply = upload(admin, bogus, hashlib.sha256(bogus).hexdigest())
check("signed upload reaches the updater while open", st == 500 and "closed" not in str(reply), reply)
if len(sys.argv) > 1:
    image = open(sys.argv[1], "rb").read()
    st, reply = upload(admin, image, hashlib.sha256(b"other bytes").hexdigest())
    check("valid image with a wrong signed hash refused", st == 500 and "does not match" in str(reply), reply)
admin.request("POST", "/api/security/uploads", {"minutes": 0})
check("no reboot during the upload tests", admin.request("GET", "/api/status")[1]["uptime"] >= uptime0)

# --- MQTT commands with the shared secret
import paho.mqtt.client as mqtt  # noqa: E402


def env(cmd):
    msg = json.dumps(dict(cmd, seq=int(time.time() * 1000)), separators=(",", ":"))
    return json.dumps({"msg": msg, "sig": sign_msg(msg, SECRET)})

acks = []
m = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
m.on_message = lambda cl, u, msg: acks.append(json.loads(msg.payload))
m.connect(MQ_HOST, int(MQ_PORT or 1883))
m.subscribe("heatpumpsn/controller/state/mpc/ack")
m.loop_start()
time.sleep(1)
for topic, payload in (("params", lambda: json.dumps({"set": "floor", "delay_s": 44})),
                       ("params", lambda: env({"set": "floor", "reset": True})),
                       ("mode", lambda: "legacy"),
                       ("mode", lambda: env({"mode": "mpc"}))):
    m.publish(f"heatpumpsn/controller/cmd/mpc/{topic}", payload(), qos=1)
    time.sleep(4)
m.loop_stop()
mq = [a for a in acks if a["source"] == "mqtt"]
check("MQTT: unsigned params rejected, signed accepted", [a["ok"] for a in mq if a["what"] == "params"] == [False, True])
check("MQTT: unsigned mode rejected, signed accepted", [a["ok"] for a in mq if a["what"] == "mode"] == [False, True])

admin.request("POST", "/api/security/secret", sealed={"secret": ""})
print("ALL PASS" if ok_all else "SOME FAILED")
