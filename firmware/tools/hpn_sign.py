"""Shared helpers: ECDSA P-256 signing through the openssl CLI, Digest auth."""
import base64
import hashlib
import os
import subprocess
import uuid


def sign_bytes(data: bytes, private_key: str) -> str:
    """Base64 DER ECDSA signature over SHA-256(data)."""
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", private_key], input=data,
                         capture_output=True, check=True).stdout
    return base64.b64encode(sig).decode()


def digest_header(method: str, uri: str, user: str, password: str, realm: str = "HeatpumpsN") -> str:
    """Authorization header for the controller's Digest login (client nonce)."""
    nonce = uuid.uuid4().hex
    cnonce = uuid.uuid4().hex
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}".encode()).hexdigest()
    return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'qop=auth, nc=00000001, cnonce="{cnonce}", response="{resp}"')


def portal_credentials():
    return os.environ.get("HPN_PORTAL_USER", "admin"), os.environ.get("HPN_PORTAL_PASSWORD", "admin")
