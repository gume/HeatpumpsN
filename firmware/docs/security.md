# Controller access security

This document records the security design of the controller's web/API
access: what it protects against, what it does not, which alternatives were
considered, and why this design was chosen. Implementation: `security.h`,
`security.cpp`, `web_portal.cpp` (`HpnAuthMiddleware`), `data/js/hpnauth.js`
(browser), `tools/hpn_auth.py` (Python clients).

## 1. Setting and requirements

* The controller runs inside the house network. No access from the internet
  is planned. Remote access, if ever needed, goes through an encrypted tunnel
  into the house, not through the controller.
* The network inside the house is not trusted. Someone on it may **eavesdrop**
  (passively capture all traffic) and may **inject** their own requests.
* Updates are done only over the network (OTA). Nobody has a serial cable
  attached during normal operation.
* A TLS terminator on another house machine was ruled out: the hop from that
  machine to the controller would still be unprotected house network.
* TLS on the controller itself was ruled out for now: see section 3.

What must hold even against an eavesdropper:

1. A captured request cannot be replayed: not a settings change, not
   "open uploads", not an upload.
2. A captured request does not reveal the password, or anything that lets an
   attacker sign their own requests.
3. Secrets (portal passwords, Wi-Fi and MQTT credentials, the MQTT command
   secret) never cross the network in a readable form.
4. Firmware cannot be replaced unless an administrator explicitly allows it
   at that moment.
5. Everyday monitoring does not require the administrator password.

## 2. The concerns that shaped the design

| Concern | How it is answered |
|---|---|
| An eavesdropper captures the login and reuses it to open uploads | No reusable login exists: every request is signed with a key derived from the password and a **single-use nonce issued by the controller**; a replay finds the nonce already spent (section 4.3) |
| HTTP Digest (the previous scheme) | Rejected: it signs only method and URL, not the body; the web library does not track nonces, so a captured header could be replayed with any body. MD5 also makes offline password guessing cheap |
| Sequence numbers: how are they synchronised between sessions? | HTTP needs none: the controller issues the nonces, so there is no client counter or clock to keep in step. For MQTT (no request/response path) see section 4.8 |
| No working clock without internet (NTP may never sync in the house) | Nonces expire by controller uptime (60 s), not by wall-clock time; no clock synchronisation is needed anywhere |
| A fake portal app served to the browser by an active attacker could capture a typed password | Accepted residual risk (section 6), limited by two users: everyday viewing uses `monitor`, which cannot change anything; `admin` is meant to be used rarely. A later house-side proxy with its own client code (section 7) removes the risk |
| A shared key on the observer vs. the update path | The observer's MQTT commands use their own secret; firmware uploads need the admin login |

## 3. Alternatives considered

| Option | Why not (now) |
|---|---|
| **TLS on the controller** | The web library (ESPAsyncWebServer/AsyncTCP) has no TLS. ESP-IDF's `esp_https_server` would mean a TLS front end or rewriting about 60 routes. With the prebuilt mbedTLS config every TLS connection needs about 40 KB of internal RAM (two fixed 16 KB buffers), which leaves room for 1–2 concurrent connections on the ESP32 and fewer on the S2. It would cost 80–120 KB of flash. Remains possible later: the request signing here stays useful underneath it |
| **TLS on a house server** | Ruled out by the requirements: the server-to-controller hop stays exposed |
| **HTTP Digest** | Replayable as implemented (above); browser support for the body-protecting variant (`auth-int`) does not exist |
| **ECDSA-signed uploads and commands** (implemented, then removed) | Protected the images but not the login itself, so a captured Digest login could still be replayed. More moving parts (key pairs, signing tools) than the risk warranted |
| **An "upload window" opened by the admin before an upload** (implemented, then removed) | Useful while a captured login could be replayed. Under HPN1 the upload request itself is admin-signed and single-use, and anyone holding an admin session could open the window just as well, so it added steps without protection |
| **Hawk** | Exactly the "nonce + short time window + nonce cache" idea, but it depends on synchronised clocks; the reference implementation is no longer maintained |
| **HTTP Message Signatures (RFC 9421) + Content-Digest (RFC 9530)** | The current IETF standard for signing HTTP messages, but its canonicalisation (structured fields, component identifiers) is a lot of code on a microcontroller and in a browser without WebCrypto. HPN1 borrows its idea (sign method, path, query, body digest and a nonce) in a much smaller form |

**Chosen: HPN1, a challenge-response request-signing scheme.** It is
modelled on Hawk and RFC 9421, with nonces issued by the controller instead
of timestamps, plus sealed payloads for secrets. Everything is built from
HMAC-SHA256 and PBKDF2, which are available in mbedTLS on every target and
small enough to implement and test in the browser.

## 4. The HPN1 protocol

### 4.1 Users and keys

Two fixed users:

| User | Rights |
|---|---|
| `admin` | everything |
| `monitor` | read-only: `GET` requests and the live WebSocket; never the configuration export |

The factory passwords are admin/admin and monitor/monitor; the Security page
warns while either is unchanged.

```
master = PBKDF2-HMAC-SHA256(password, salt[16], 10000 iterations, 32 bytes)
k_auth = HMAC-SHA256(master, "hpn-auth")      request signatures
k_enc  = HMAC-SHA256(master, "hpn-enc")       sealed payloads
```

* The controller stores salt, `k_auth` and `k_enc` per user (NVS namespace
  `hpnsec`); it never sees or stores a password and never runs PBKDF2. The
  factory keys are precomputed constants.
* Changing a password: the client picks a new random salt, derives the keys
  from the new password and sends `{user, salt, k_auth, k_enc}` sealed with
  the admin's current `k_enc`.
* PBKDF2 makes each guess against captured traffic cost 10 000 HMAC rounds.
  It does not save a short password: use at least 8 characters for monitor
  and a long passphrase for admin.

### 4.2 Challenge

`GET /api/auth/challenge?user=<name>` (the only API route without signature)
returns `{"user", "salt", "iter": 10000, "nonce": <32 hex>, "ttl": 60}`.

* Nonce: 128 bits from the hardware RNG, valid 60 s of controller uptime,
  usable once.
* The controller keeps up to 32 outstanding nonces and evicts the oldest
  first.
* Every response to a signed request carries the next nonce in `X-HPN-Nonce`,
  so a steady client needs no extra challenge round trips.

### 4.3 Signed request

```
X-HPN-Auth: user=<name>,nonce=<nonce>,sig=<hex HMAC-SHA256(k_auth, S)>

S = "HPN1\n" METHOD "\n" path "\n" canonical_query "\n" nonce "\n" body_hash
```

* `canonical_query`: the URL parameters as `name=value`, sorted by name then
  value, joined with `&`; parameters starting with `hpn_` are excluded.
* `body_hash`: hex SHA-256 of the exact body bytes (of the empty string for
  `GET`).

The controller's checks, in order:
1. Known user, not locked.
2. The nonce is live: **it is removed from the table on first use**, valid
   signature or not.
3. The signature matches (constant-time compare).

Errors carry `X-HPN-Reason` (`header`, `user`, `nonce`, `sig`, `locked`,
`role`, `sealed`). On `nonce`, clients fetch a fresh challenge and retry once.
After 5 consecutive bad signatures a user is locked for 30 s (HTTP 423).

### 4.4 Uploads

`POST /api/update?sha256=<hex>[&target=fs]`, signed with
`body_hash = "multipart"`:

* The image's SHA-256 is a signed query parameter.
* The controller authenticates on the **first chunk**, before anything is
  written to flash. The library's own checks would only run after the whole
  body has been written.
* It hashes the bytes while writing and compares the result at the end. On a
  mismatch an application image is never activated, and a filesystem image
  (written in place) is erased.
* Uploads are admin-only.

### 4.5 WebSocket

Browsers cannot set headers on a WebSocket handshake. The credentials
therefore travel as query parameters:
`/ws?hpn_user=..&hpn_nonce=..&hpn_sig=..`, signed as a `GET /ws` with an empty
body. A captured URL cannot be reused, because its nonce is spent.

### 4.6 Sealed payloads (confidentiality of secrets)

`{"sealed": base64(iv[16] | ciphertext | tag[32])}`, using the requesting
user's `k_enc` and the request's nonce:

```
ks  = HMAC(k_enc, "seal-stream")    km = HMAC(k_enc, "seal-mac")
ciphertext = plaintext XOR (HMAC(ks, iv | ctr32) for ctr = 0, 1, ...)
tag = HMAC(km, "HPN1-seal\n" nonce "\n" | iv | ciphertext)          encrypt-then-MAC
```

* HMAC-SHA256 used as a PRF in counter mode is a standard stream-cipher
  construction. It is combined encrypt-then-MAC, with separate stream and MAC
  keys and a random IV.
* The nonce in the tag binds a sealed body to one request, so it cannot be
  replayed in another.
* AES-GCM was not used because browsers offer no WebCrypto on plain HTTP and
  a hand-written AES in JavaScript was judged riskier than one more use of
  the HMAC that already has to be correct.

The controller **refuses unsealed bodies** for:
* password changes;
* the MQTT command secret;
* `PUT /api/config/network` (Wi-Fi and MQTT credentials);
* configuration import.

The **configuration export is sent sealed.** Other configuration reads
contain no secrets; they only show `has_*_password` flags.

### 4.7 Roles

* Uploads, every `POST`/`PUT`, and the configuration export require `admin`.
* `monitor` gets HTTP 403 for all of them.

### 4.8 MQTT commands (observer)

MQTT gives the controller no way to send a challenge, so MPC parameter and
MQTT mode commands use a different scheme. The envelope is
`{"msg": "<json>", "sig": "<hex HMAC-SHA256(secret, msg)>"}`, signed with a
shared **command secret** (16–64 characters, set sealed on the Security page).

* `msg` must contain a `seq` larger than the last accepted one. The
  controller keeps the last value in NVS, across reboots.
* The observer uses Unix time in milliseconds as `seq`, so it needs no stored
  state.
* A command is refused if the observer's clock jumps backwards, or if a
  second observer's clock lags behind the first. A factory reset clears the
  stored value.

Home Assistant's own commands (`cmd/heating_*`, `cmd/floor_heating`,
`cmd/floor_setpoint`) are unsigned and rely on the broker's access control.

## 5. Other measures

* The emulation API (`/api/sim/*`), the emulated board model and the
  emulation page exist only in `HPN_IO_EMULATION` builds. Hardware
  filesystem images leave the emulation files out.
* A factory reset restores the factory passwords and removes the command
  secret.
* Events: 1610 password changed, 1611 command secret, 1613 password recovery
  build reset the passwords, 1704 upload hash mismatch, 1346 rejected
  MQTT/REST command.

## 6. Residual risks (accepted or out of scope)

| Risk | Assessment |
|---|---|
| **Active man-in-the-middle serving a modified portal app** that captures the typed password | Possible on plain HTTP; the main residual risk. Mitigated by the monitor/admin split (use admin rarely) and removed by a client that does not load code from the controller (section 7) or by TLS |
| **Responses are not authenticated** (except the sealed export) | An active attacker could falsify readings shown in the browser. Integrity of *requests* (what changes the plant) is protected |
| **Readings are not confidential** | Temperatures, states and the event log are readable by an eavesdropper; judged acceptable |
| **Denial of service** | Anyone on the network can request challenges (evicting nonces) or trip the 30 s lockout. Clients retry; a determined attacker on the LAN can always flood the controller |
| **Weak passwords** | Captured traffic allows offline guessing at 10 000 HMAC rounds per guess; long passwords make this impractical |
| **Physical access** | Serial flashing, reading NVS (keys are stored unencrypted) and the boot buttons are out of scope; flash encryption and Secure Boot are for the final hardware |
| **Unsigned Home Assistant commands on MQTT** | Protected only by broker authentication and ACLs, which belong to the broker configuration |
| **No forward secrecy** | Whoever learns a password later can decrypt previously captured sealed payloads |

## 7. Planned evolution

* **House proxy with its own client.** A proxy on a house server can serve
  its own portal (no code fetched from the controller), offer HTTPS to the
  browsers, and speak HPN1 to the controller as `monitor` or `admin`. This
  removes the active-MITM risk for the browser without trusting the
  server-to-controller hop: that hop is HPN1-protected.
* **TLS on the controller** stays possible later. HPN1 would keep protecting
  the requests underneath it.

## 8. Operating it

```bash
# portal: open http://<controller>/, log in as monitor (viewing) or admin (changes)
# change both factory passwords under Settings → Security

# firmware update (admin)
export HPN_PORTAL_PASSWORD=<admin password>
pio run -e esp32dev_sim_ota -t upload       # signed HPN1 upload
pio run -e esp32dev_sim_ota -t uploadfs

# observer commands over MQTT
export HPN_COMMAND_SECRET=<secret set on the Security page>
python firmware/tools/sign_command.py params '{"set":"rad2","delay_s":45}' --publish <broker:port>
```

Python clients use `tools/hpn_auth.py` (standard library only); the HIL
simulator takes `--controller-user` and `--controller-password`.

### Forgotten password

There is no network recovery path on purpose: anything that resets a password
over the network would be the weakest login. Recovery needs the USB cable:

```bash
pio run -e esp32s2_password_reset -t upload --upload-port /dev/ttyACM0   # bench: esp32dev_sim_password_reset
```

This is the normal firmware with `HPN_PASSWORD_RESET`. On the first boot of
that image it restores admin/admin and monitor/monitor and removes the MQTT
command secret (event 1613); configuration, MPC parameters and Wi-Fi are kept.
It remembers the image hash (NVS `hpnrst/image`), so later reboots of the same
image do not reset again. Log in, set new passwords and the command secret,
then upload the normal firmware over the air. `pio run -t erase` before a USB
flash is the heavier alternative: everything back to defaults.

## 9. Verification

* `floor_simulator/tests/test_hpn_auth.py` checks the browser implementation
  of SHA-256, HMAC, PBKDF2 and sealing against OpenSSL (via Node) and Python,
  including SHA-256 padding boundaries and the nonce binding of sealed data.
* `firmware/test/live/security_check.py` runs the protocol against a device
  (35 checks):
  * challenge, unsigned and forged requests;
  * replayed request, changed body and changed query;
  * monitor restrictions;
  * signed, unsigned and replayed WebSocket handshakes;
  * clear and tampered sealed bodies, sealed secret, sealed export;
  * password change, lockout and lockout release;
  * monitor upload, and a valid image with a wrong signed hash;
  * signed and unsigned MQTT commands.
