"""HPN1 challenge-response client for the controller (standard library only).

Protocol (firmware/docs/security.md):
    keys     master = PBKDF2-HMAC-SHA256(password, salt, iter, 32)
             k_auth = HMAC(master, "hpn-auth"); k_enc = HMAC(master, "hpn-enc")
    request  X-HPN-Auth: user=<u>,nonce=<device nonce>,sig=<hex>
             sig = HMAC(k_auth, "HPN1\\n" METHOD "\\n" path "\\n" query "\\n" nonce "\\n" body_hash)
             body_hash = hex SHA-256 of the raw body ("multipart" for uploads, whose
             image hash travels in the signed `sha256` query parameter)
    sealed   {"sealed": base64(iv16 | ct | tag32)}: keystream HMAC(HMAC(k_enc,"seal-stream"), iv|ctr32),
             tag = HMAC(HMAC(k_enc,"seal-mac"), "HPN1-seal\\n" nonce "\\n" | iv | ct)
Every response to an authenticated request carries the next nonce in X-HPN-Nonce.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def hmac256(key: bytes, msg) -> bytes:
    return _hmac.new(key, msg.encode() if isinstance(msg, str) else msg, hashlib.sha256).digest()


def derive_keys(password: str, salt_hex: str, iterations: int) -> tuple[bytes, bytes]:
    master = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iterations, 32)
    return hmac256(master, "hpn-auth"), hmac256(master, "hpn-enc")


def canonical_query(query: str) -> str:
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True) if not k.startswith("hpn_")]
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def signature(k_auth: bytes, method: str, url: str, nonce: str, body_hash: str) -> str:
    u = urllib.parse.urlsplit(url)
    text = f"HPN1\n{method.upper()}\n{u.path}\n{canonical_query(u.query)}\n{nonce}\n{body_hash}"
    return hmac256(k_auth, text).hex()


def _keystream(k_enc: bytes, iv: bytes, n: int) -> bytes:
    ks = hmac256(k_enc, "seal-stream")
    out = b"".join(hmac256(ks, iv + i.to_bytes(4, "big")) for i in range((n + 31) // 32))
    return out[:n]


def seal(k_enc: bytes, nonce: str, plaintext: bytes, iv: bytes | None = None) -> str:
    iv = iv or os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(plaintext, _keystream(k_enc, iv, len(plaintext))))
    tag = hmac256(hmac256(k_enc, "seal-mac"), f"HPN1-seal\n{nonce}\n".encode() + iv + ct)
    return base64.b64encode(iv + ct + tag).decode()


def unseal(k_enc: bytes, nonce: str, sealed: str) -> bytes:
    raw = base64.b64decode(sealed)
    iv, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    want = hmac256(hmac256(k_enc, "seal-mac"), f"HPN1-seal\n{nonce}\n".encode() + iv + ct)
    if not _hmac.compare_digest(want, tag):
        raise ValueError("sealed payload failed its integrity check")
    return bytes(a ^ b for a, b in zip(ct, _keystream(k_enc, iv, len(ct))))


class Client:
    """Minimal urllib client: Client("localhost:30080", "admin", "admin").request("GET", "/api/state")."""

    def __init__(self, host: str, user: str, password: str, timeout: float = 30.0):
        self.base = host if host.startswith("http") else f"http://{host}"
        self.user = user
        self.timeout = timeout
        c = self.challenge()
        self.k_auth, self.k_enc = derive_keys(password, c["salt"], c["iter"])
        self.nonces = [c["nonce"]]

    def challenge(self) -> dict:
        url = f"{self.base}/api/auth/challenge?user={urllib.parse.quote(self.user)}"
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return json.load(r)

    def nonce(self) -> str:
        return self.nonces.pop(0) if self.nonces else self.challenge()["nonce"]

    def headers(self, method: str, path: str, nonce: str, body: bytes | None = None, body_hash: str | None = None):
        bh = body_hash or hashlib.sha256(body or b"").hexdigest()
        return {"X-HPN-Auth": f"user={self.user},nonce={nonce},sig={signature(self.k_auth, method, path, nonce, bh)}"}

    def request(self, method: str, path: str, body=None, sealed=None, unseal_response: bool = False,
                raw: bytes | None = None, content_type: str | None = None, body_hash: str | None = None):
        """Returns (status, parsed JSON or text). `sealed` encrypts a JSON body."""
        for attempt in range(2):
            nonce = self.nonce()
            data = raw
            if sealed is not None:
                data = json.dumps({"sealed": seal(self.k_enc, nonce, json.dumps(sealed).encode())}).encode()
            elif body is not None:
                data = json.dumps(body).encode()
            headers = self.headers(method, path, nonce, data, body_hash)
            if data is not None:
                headers["Content-Type"] = content_type or "application/json"
            req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    status, text, hdrs = r.status, r.read().decode(errors="replace"), r.headers
            except urllib.error.HTTPError as e:
                status, text, hdrs = e.code, e.read().decode(errors="replace"), e.headers
            if hdrs.get("X-HPN-Nonce"):
                self.nonces.append(hdrs["X-HPN-Nonce"])
            if status == 401 and attempt == 0 and hdrs.get("X-HPN-Reason") == "nonce":
                self.nonces.clear()
                continue
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                return status, text
            if unseal_response and status == 200 and isinstance(payload, dict) and "sealed" in payload:
                payload = json.loads(unseal(self.k_enc, nonce, payload["sealed"]))
            return status, payload
        return status, payload


def portal_credentials():
    return os.environ.get("HPN_PORTAL_USER", "admin"), os.environ.get("HPN_PORTAL_PASSWORD", "admin")
