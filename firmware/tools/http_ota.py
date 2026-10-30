"""Upload a firmware or filesystem image over HTTP (POST /api/update).

Used by the esp32dev_sim_ota environment as its upload command:
    python http_ota.py <image> <host:port>
A LittleFS/SPIFFS image (pio run -t uploadfs) goes to ?target=fs.

The request is HPN1-signed as admin (hpn_auth.py); the image's SHA-256 is a
signed query parameter that the controller checks against the received bytes.

Environment: HPN_PORTAL_USER / HPN_PORTAL_PASSWORD (default admin / admin).
"""
import hashlib
import os
import sys
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpn_auth import Client, portal_credentials  # noqa: E402


def main(image: str, port: str) -> int:
    name = os.path.basename(image)
    target = "fs" if any(k in name for k in ("littlefs", "spiffs", "fatfs")) else "app"
    data = open(image, "rb").read()
    query = {"sha256": hashlib.sha256(data).hexdigest()}
    if target == "fs":
        query["target"] = "fs"
    path = "/api/update?" + urllib.parse.urlencode(query)
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    user, password = portal_credentials()
    print(f"Uploading {name} ({len(data)} bytes, {target}) to http://{port}/api/update")
    try:
        client = Client(port, user, password, timeout=300)
        status, reply = client.request("POST", path, raw=body, body_hash="multipart",
                                       content_type=f"multipart/form-data; boundary={boundary}")
    except OSError as e:
        print(f"upload failed: {e}")
        return 1
    print(f"HTTP {status}: {reply}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
