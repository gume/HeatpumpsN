"""Upload a firmware or filesystem image over HTTP (POST /api/update).

Used by the esp32dev_sim_ota environment as its upload command:
    python http_ota.py <image> <host:port>
A LittleFS/SPIFFS image (pio run -t uploadfs) goes to ?target=fs.

Uploads are refused until the upload window is opened on the portal
(Settings > Security). The window closes with every reboot, so open it again
between an application and a filesystem upload.

Environment: HPN_PORTAL_USER / HPN_PORTAL_PASSWORD (default admin / admin).
"""
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpn_sign import digest_header, portal_credentials  # noqa: E402


def main(image: str, port: str) -> int:
    name = os.path.basename(image)
    target = "fs" if any(k in name for k in ("littlefs", "spiffs", "fatfs")) else "app"
    data = open(image, "rb").read()
    uri = "/api/update" + ("?target=fs" if target == "fs" else "")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    user, password = portal_credentials()
    req = urllib.request.Request(f"http://{port}{uri}", data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": digest_header("POST", uri, user, password)})
    print(f"Uploading {name} ({len(data)} bytes, {target}) to http://{port}/api/update")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            print(resp.read().decode(errors="replace"))
            return 0
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        print(f"HTTP {e.code}: {text}")
        if "closed" in text:
            print("Open the upload window on the portal: Settings > Security > Open for 10 min")
    except OSError as e:
        print(f"upload failed: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
