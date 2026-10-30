"""Upload a firmware or filesystem image over HTTP (POST /api/update).

Used by the esp32dev_sim_ota environment as its upload command:
    python http_ota.py <image> <host:port>
A LittleFS/SPIFFS image (pio run -t uploadfs) goes to ?target=fs.

Environment:
    HPN_OTA_KEY          private key: the image is signed (required once the
                         controller has an OTA public key installed)
    HPN_PORTAL_USER      portal login (default admin)
    HPN_PORTAL_PASSWORD  portal password (default admin)
"""
import hashlib
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpn_sign import digest_header, portal_credentials, sign_bytes  # noqa: E402


def main(image: str, port: str) -> int:
    name = os.path.basename(image)
    target = "fs" if any(k in name for k in ("littlefs", "spiffs", "fatfs")) else "app"
    data = open(image, "rb").read()
    query = {"target": "fs"} if target == "fs" else {}
    key = os.environ.get("HPN_OTA_KEY", "")
    if key:
        query["sha256"] = hashlib.sha256(data).hexdigest()
        query["sig"] = sign_bytes(data, key)
    uri = "/api/update" + ("?" + urllib.parse.urlencode(query) if query else "")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    user, password = portal_credentials()
    req = urllib.request.Request(f"http://{port}{uri}", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": digest_header("POST", uri, user, password)})
    print(f"Uploading {name} ({len(data)} bytes, {target}, {'signed' if key else 'unsigned'}) to http://{port}/api/update")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            print(resp.read().decode(errors="replace"))
            return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    except OSError as e:
        print(f"upload failed: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
