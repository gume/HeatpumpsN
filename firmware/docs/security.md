# Access protection

Basic protection without TLS. It keeps out casual access, stray uploads and
unsigned observer commands. It does not protect against someone who can read
or modify the network traffic: the portal traffic is unencrypted, and a
captured Digest login header can be replayed, because the web server library
does not track nonces.

| Layer | Protects | Default |
|---|---|---|
| Portal login (HTTP Digest) | every page, API and `/ws` | on, admin / admin |
| Upload window | `POST /api/update` (application and LittleFS) | closed |
| Command secret (HMAC-SHA256) | MPC parameters (MQTT and REST), MQTT MPC mode | off until a secret is set |

Home Assistant commands (`cmd/heating_*`, `cmd/floor_heating`,
`cmd/floor_setpoint`) are not signed. The emulation API (`/api/sim/*`) and the
emulation page exist only in `HPN_IO_EMULATION` builds. Hardware filesystem
images leave the emulation files out.

## 1. Portal login

* User `admin`, password `admin` until changed under **Settings → Security**
  (4–63 characters; the current password is required).
* The device stores only the Digest hash (MD5 of `admin:HeatpumpsN:<password>`).
* Browsers cannot answer a Digest challenge on a WebSocket, so the logged-in
  portal fetches a per-boot token from `/api/session` and opens
  `/ws?token=…`.
* A factory reset restores admin / admin and removes the command secret.
* Clients: `curl --digest -u admin:<pw>`, httpx `DigestAuth`. The HIL simulator
  takes `--controller-password` or `HPN_PORTAL_PASSWORD`.

## 2. Upload window

Firmware and portal-file uploads are refused until a logged-in user opens the
window (**Settings → Security → Open for 10 min**, or
`POST /api/security/uploads {"minutes": 10}`; at most 60, 0 closes it).

* The window lives in RAM, so every reboot closes it. After an application
  update, open it again before uploading the portal files.
* `/api/update` checks the login and the window on the first chunk of the
  body, because the library's login check would otherwise run only after the
  image had already been written to flash.

```bash
export HPN_PORTAL_PASSWORD=<password>
pio run -e esp32dev_sim_ota -t upload      # open the window first
pio run -e esp32dev_sim_ota -t uploadfs    # open it again after the reboot
```

Event codes: 1702 upload rejected (window closed); 1610–1612 password,
secret and window changes.

## 3. Signed commands (shared secret)

Set a 16–64 character secret under **Settings → Security** and give the same
value to the observer. From then on MPC parameter and MQTT mode commands must
be an envelope:

```json
{"msg": "{\"seq\":1793319338550,\"set\":\"floor\",\"delay_s\":45}", "sig": "<hex HMAC-SHA256(secret, msg)>"}
```

* `msg` is the command as a JSON string; the HMAC covers exactly those bytes,
  so no canonical JSON is needed.
* `seq` must be larger than the last accepted one (stored in NVS). Unix time in
  milliseconds works without any state on the observer side.
* MQTT: `<base>/cmd/mpc/params` and `<base>/cmd/mpc/mode` (`msg` = `{"seq":…,"mode":"mpc"}`).
  REST: `POST /api/mpc/params` (after the portal login). The REST mode change
  (`/api/mpc/mode`) needs only the login, so a person at the portal can switch
  modes.
* The Security page shows a fingerprint (first 8 bytes of SHA-256 of the
  secret), so both sides can be compared without showing the secret.
* Rejections are reported on `<base>/state/mpc/ack` and as event 1346.

```bash
export HPN_COMMAND_SECRET=<secret>
python firmware/tools/sign_command.py params '{"set":"rad2","delay_s":45}' --publish localhost:31883
python firmware/tools/sign_command.py mode mpc --publish localhost:31883
```

## 4. Checking it

`firmware/test/live/security_check.py` runs the whole matrix against a device
with the default password and no secret: login, WebSocket token, password
change, the upload window for application and filesystem images, and signed,
unsigned, tampered, wrong-secret and replayed commands over REST and MQTT. It
changes the device state while it runs and restores the defaults at the end.
