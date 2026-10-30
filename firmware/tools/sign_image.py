"""Sign a firmware or LittleFS image for the controller.

    python tools/sign_image.py <image.bin> [private_key.pem]
Writes <image.bin>.sig ({"sha256": hex, "sig": base64}) for the portal's upload
form. The key defaults to $HPN_OTA_KEY.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpn_sign import sign_bytes  # noqa: E402


def main(image: str, key: str) -> int:
    data = open(image, "rb").read()
    out = {"sha256": hashlib.sha256(data).hexdigest(), "sig": sign_bytes(data, key)}
    with open(image + ".sig", "w") as f:
        json.dump(out, f)
    print(f"{image}.sig written (sha256 {out['sha256'][:16]}…)")
    return 0


if __name__ == "__main__":
    key = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("HPN_OTA_KEY", "")
    if len(sys.argv) < 2 or not key:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], key))
