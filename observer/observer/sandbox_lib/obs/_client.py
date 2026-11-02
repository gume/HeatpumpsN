"""One JSON request per connection over the observer's Unix socket."""
import json
import os
import socket


class ObsError(RuntimeError):
    pass


def call(op: str, **args):
    path = os.environ.get("OBS_SOCKET")
    if not path or not os.path.exists(path):
        raise ObsError("no data service: OBS_SOCKET is not set (not running inside an observer run)")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(300)
        s.connect(path)
        s.sendall(json.dumps({"op": op, "args": args}).encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    reply = json.loads(b"".join(chunks) or b"{}")
    if "error" in reply:
        raise ObsError(reply["error"])
    return reply["result"]
