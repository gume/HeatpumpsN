"""Upload a firmware or filesystem image over HTTP (POST /api/update).

Used by the esp32dev_sim_ota environment as its upload command:
    python http_ota.py <image> <host:port>
A LittleFS/SPIFFS image (pio run -t uploadfs) goes to ?target=fs.
"""
import os
import sys
import urllib.request
import uuid


def main(image: str, port: str) -> int:
    name = os.path.basename(image)
    target = "fs" if any(k in name for k in ("littlefs", "spiffs", "fatfs")) else "app"
    data = open(image, "rb").read()
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    url = f"http://{port}/api/update" + ("?target=fs" if target == "fs" else "")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    print(f"Uploading {name} ({len(data)} bytes, {target}) to {url}")
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
