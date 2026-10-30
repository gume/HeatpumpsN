"""HMAC signature for MQTT commands signed with the shared command secret."""
import hashlib
import hmac


def sign_msg(msg: str, secret: str) -> str:
    """Hex HMAC-SHA256 of the exact msg bytes with the shared command secret."""
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
