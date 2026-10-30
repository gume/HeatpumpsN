# Access protection

Basic protection without TLS. It keeps out casual access, unsigned code and
unsigned observer commands. It does not protect against someone who can read
or modify the network traffic: the portal traffic is unencrypted, and a
captured Digest login header can be replayed, because the web server library
does not track nonces.

| Layer | Protects | Enforced |
|---|---|---|
| Portal login (HTTP Digest) | every page, API and `/ws` | always (default admin / admin) |
| Signed updates (ECDSA P-256) | `POST /api/update`, application and LittleFS | once an OTA public key is installed |
| Signed observer commands (ECDSA P-256) | MPC parameters (MQTT and REST), MQTT MPC mode | once an observer public key is installed |

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
* `/api/update` checks the login on the first chunk of the upload body. The
  library's login check would otherwise run only after the image had already
  been written to flash.
* A factory reset restores admin / admin and removes both keys.
* Clients: `curl --digest -u admin:<pw>`, httpx `DigestAuth`. The HIL simulator
  takes `--controller-password` or `HPN_PORTAL_PASSWORD`.

## 2. Keys

```bash
firmware/tools/gen_keys.sh            # firmware/keys/{ota,observer}_{private,public}.pem
```

`firmware/keys/` and `*_private.pem` are ignored by git. Paste a
`*_public.pem` into **Settings → Security**; the fingerprint shown there
matches the one the script prints. Give `observer_private.pem` to the
observer only. The OTA key stays with whoever builds firmware.

## 3. Signed updates

The image is signed over its SHA-256, which is sent as two query parameters:
`POST /api/update[?target=fs]&sha256=<hex>&sig=<base64 DER>`. The device:

1. checks the signature against the declared hash before writing anything;
2. hashes the bytes as they are written;
3. compares the hash with the declared one at the end. On a mismatch an
   application image is never activated, and a LittleFS image (written in
   place) is erased rather than served.

```bash
export HPN_OTA_KEY=$PWD/firmware/keys/ota_private.pem
export HPN_PORTAL_PASSWORD=<password>
pio run -e esp32dev_sim_ota -t upload      # signed application
pio run -e esp32dev_sim_ota -t uploadfs    # signed portal files
python firmware/tools/sign_image.py firmware.bin    # .sig file for the portal upload form
```

**Allow unsigned for 15 min** (Security page) opens a temporary exception,
for example while changing the key. It ends by itself and does not survive a
reboot. Event codes: 1702 unsigned rejected, 1703 bad signature, 1704 hash
mismatch, 1610–1612 password, key and exception changes.

## 4. Signed observer commands

```json
{"msg": "{\"seq\":1793319338550,\"set\":\"floor\",\"delay_s\":45}", "sig": "<base64 DER over msg>"}
```

* `msg` is the command as a JSON string; the signature covers exactly those
  bytes, so no canonical JSON is needed.
* `seq` must be larger than the last accepted one (stored in NVS). Unix time
  in milliseconds works without any state on the observer side.
* MQTT: `<base>/cmd/mpc/params` and `<base>/cmd/mpc/mode` (`msg` = `{"seq":…,"mode":"mpc"}`).
  REST: `POST /api/mpc/params` (after the portal login). The REST mode change
  (`/api/mpc/mode`) needs only the login, so a person at the portal can switch
  modes.
* Rejections are reported on `<base>/state/mpc/ack` and as event 1346.

```bash
export HPN_OBSERVER_KEY=$PWD/firmware/keys/observer_private.pem
python firmware/tools/sign_command.py params '{"set":"rad2","delay_s":45}' --publish localhost:31883
python firmware/tools/sign_command.py mode mpc --publish localhost:31883
```

## 5. Checking it

`firmware/test/live/security_check.py` runs the whole matrix against a device
that has the default password and no keys installed: login, WebSocket token,
password change, key installation, unsigned, wrongly signed and tampered
uploads, the unsigned window, and observer envelopes over REST and MQTT
including replay. It changes the device state while it runs and removes both
keys at the end.
