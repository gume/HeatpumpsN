"""Shared helpers: HMAC command signatures and the controller's Digest login."""
import hashlib
import hmac
import os
import uuid


def sign_msg(msg: str, secret: str) -> str:
    """Hex HMAC-SHA256 of the exact msg bytes with the shared command secret."""
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


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
