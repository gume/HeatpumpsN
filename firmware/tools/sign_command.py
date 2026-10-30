"""Build (and optionally publish) a signed command for the controller.

    python tools/sign_command.py params '{"set":"floor","delay_s":45}' [--publish host:port]
    python tools/sign_command.py mode mpc [--publish host:port]

The command JSON gets "seq" = current Unix time in ms (must increase) and is
wrapped as {"msg": "<json>", "sig": "<hex HMAC-SHA256(secret, msg)>"}.
Secret: $HPN_COMMAND_SECRET (the value set under Settings > Security).
MQTT base topic: $HPN_BASE (default heatpumpsn/controller).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpn_sign import sign_msg  # noqa: E402


def envelope(command: dict, secret: str) -> str:
    command = dict(command, seq=int(time.time() * 1000))
    msg = json.dumps(command, separators=(",", ":"))
    return json.dumps({"msg": msg, "sig": sign_msg(msg, secret)})


def main(argv) -> int:
    if len(argv) < 2 or argv[0] not in ("params", "mode"):
        print(__doc__)
        return 2
    secret = os.environ.get("HPN_COMMAND_SECRET", "")
    if not secret:
        print("set HPN_COMMAND_SECRET to the controller's command secret")
        return 2
    kind = argv[0]
    command = json.loads(argv[1]) if kind == "params" else {"mode": argv[1]}
    payload = envelope(command, secret)
    if "--publish" in argv:
        import paho.mqtt.client as mqtt
        host, _, port = argv[argv.index("--publish") + 1].partition(":")
        topic = f"{os.environ.get('HPN_BASE', 'heatpumpsn/controller')}/cmd/mpc/{kind}"
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        c.connect(host, int(port or 1883))
        c.loop_start()
        c.publish(topic, payload, qos=1).wait_for_publish(5)
        c.loop_stop()
        print(f"published to {topic}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
