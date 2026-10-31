# Upgrade checklist: ESPHome board → HeatpumpsN firmware

For the LOLIN S2 mini on the HomeNet Heating control v0.1 board, running the
ESPHome configuration in `analysis/OLD-CODE/heatpumps.yaml`. The details
behind each step (tables, power-loss behaviour, rollback) are in
[partition-migration.md](partition-migration.md); passwords and recovery are
in [security.md](security.md).

Work with **mains disconnected** from the relay outputs for the whole
procedure, up to and including the function checks.

```
ESPHome ──(1) ESPHome OTA──▶ new firmware, maintenance page
        ──(2) Migrate──▶ new partition layout
        ──(3) littlefs.bin──▶ full portal ──(4) checks ──(5) counter preset ──(6) final firmware
```

---

## 0. Before you start

### 0.1 Record from ESPHome

Nothing below carries over to the new firmware. Once ESPHome is replaced it
cannot be read any more.

Relay counters: the ESPHome log prints them every 10 minutes
(`[W][counters]: Relay N count: …`). Take the block just before step 1.

| ESPHome | Relay | Count |
|---|---|---|
| `Relay 0` | Heating | |
| `Relay 1` | Pump 1 (radiator 1) | |
| `Relay 2` | Pump 2 (radiator 2) | |
| `Relay 3` | Pump 3 (floor) | |
| `Relay 4` | Mixer direction | |
| `Relay 5` | Mixer motion | |

Settings:

| ESPHome entity | Value |
|---|---|
| Mixer forward temperature sensor address | |
| Mixer return temperature sensor address | |
| Mixer incoming temperature sensor address | |
| Floorheating circuit | |
| Floorheating temperature (setpoint) | |
| Summer mode | |
| MQTT broker (for later) | |

- [ ] Relay counters written down
- [ ] Sensor addresses and settings written down
- [ ] Controller IP address noted: ______________

### 0.2 Prepare the images

Built from this repository (`firmware/`):

- [ ] Decide the factory credentials. Without `include/hpn/credentials_local.h`
      the build uses Wi-Fi **Testing / testing123** and portal **admin/admin**.
      To build your home network in, copy
      `include/hpn/credentials_local.h.example` to `credentials_local.h`, edit
      it and rebuild.
- [ ] `pio run -e esp32s2` → `.pio/build/esp32s2/firmware.bin`
- [ ] `pio run -e esp32s2 -t buildfs` → `.pio/build/esp32s2/littlefs.bin`
- [ ] The Wi-Fi network from the build is reachable where the controller is
      (router SSID `Testing`, case-sensitive, unless you built your own)
- [ ] A laptop or phone ready to join the fallback AP if Wi-Fi fails
- [ ] Optional: a USB cable and PlatformIO at hand for recovery

---

## 1. Upload the firmware with ESPHome

From a machine on the controller's network, with the ESPHome project:

```
esphome upload heatpumps.yaml --device <controller-ip> \
    --file ~/Work/HeatpumpsN/firmware/.pio/build/esp32s2/firmware.bin
```

ESPHome asks for its OTA password. If `--file` is unknown, check
`esphome upload --help` for your ESPHome version.

- [ ] Upload finished without error
- [ ] Controller rebooted

---

## 2. First boot: maintenance page

The board now runs the new firmware on ESPHome's partition table. It has no
portal files yet, so `/` shows the built-in **maintenance page**.

**Rollback timer:** the new image must be confirmed within **10 minutes** or
the board returns to ESPHome. It is confirmed after 2 minutes with Wi-Fi up,
or by an admin login. (If ESPHome's bootloader has no rollback support, there
is no timer and the image counts as confirmed at once.)

- [ ] Controller found on the network (`http://<ip>/`, same IP likely; new
      hostname `heatpumpsn`)

  If it is not on the network after about 2 minutes:
  - [ ] Join the AP **HeatpumpsN-Setup**, password **heatpumps-setup** (or
        your build's values); accept the "Sign in to network" prompt or open
        `http://192.168.4.1/`
  - [ ] Log in as admin at once (this confirms the image)
  - [ ] Settings → Network: **Scan networks**, pick the SSID, enter the
        password, save, reboot

- [ ] Logged in on the maintenance page as **admin**
- [ ] Status shows *Partition layout: esphome*
- [ ] Image state is *valid* (or *new*). While it shows *pending_verify*, wait:
      the countdown is on the page

---

## 3. Migrate the partition table

- [ ] **Migrate partition table and reboot** is enabled (no blocker shown)
- [ ] Clicked it and confirmed. **Do not cut power for the next minute.**
- [ ] The board reboots by itself; the page reloads after 30 s
- [ ] Logged in again
- [ ] Status shows *Partition layout: current*, and the Migrate card is gone

If the migration refuses, the page names the reason; nothing has changed.
If the board does not come back, see [Recovery](#recovery).

---

## 4. Upload the portal files

On the maintenance page, section **Upload**:

- [ ] Target: **Portal files (littlefs.bin)**
- [ ] File: `~/Work/HeatpumpsN/firmware/.pio/build/esp32s2/littlefs.bin`
- [ ] **Upload and reboot**; the board reboots by itself
- [ ] `http://<ip>/` now shows the full portal with the HomeNET logo
- [ ] Logged in as admin

---

## 5. Secure it

Settings → Security:

- [ ] New **admin** password set (and stored somewhere safe)
- [ ] New **monitor** password set
- [ ] Logged out and back in with the new password

---

## 6. Function checks (mains still disconnected)

### 6.1 Overview

- [ ] Dashboard updates live (values change, no "offline")
- [ ] Diagnostics: Wi-Fi connected, sensible RSSI
- [ ] Event Log: no repeating errors

### 6.2 Temperature sensors

Settings → Hardware & Pins → *Temperature sensor identities*:

- [ ] **Discover sensors** finds all three 1-Wire sensors
- [ ] Roles assigned from the addresses recorded in 0.1:
  - [ ] Mixer forward
  - [ ] Mixer return
  - [ ] Mixer incoming (hot in)
- [ ] Saved, rebooted, **Verify roles** passes
- [ ] Diagnostics → *Temperature sensor health*: all three valid, plausible
      values, error count not rising

### 6.3 LEDs

- [ ] Status LEDs light after boot
- [ ] Settings → Hardware & Pins: LED count and colour order correct (5 LEDs, GRB)

### 6.4 Relays (Test Mode)

Test Mode → **Activate Test Mode**. Each relay should click and its indicator
follow. Without mains, pump-current plausibility warnings are expected.

- [ ] Heater relay on / off
- [ ] Radiator 1 pump relay on / off
- [ ] Radiator 2 pump relay on / off
- [ ] Floor pump relay on / off
- [ ] Mixer: open moves, direction relay energized
- [ ] Mixer: close moves, direction relay released
- [ ] Mixer stops on **stop**
- [ ] **Deactivate Test Mode**; all outputs return to automatic

### 6.5 Settings

- [ ] Control & Timing: floor circuit, setpoint and summer mode as recorded
      in 0.1
- [ ] Safety & Timeouts reviewed
- [ ] Network & MQTT: broker set when you are ready for MQTT
- [ ] Diagnostics → relay counters move when relays switch (one per switch-on)

---

## 7. Carry over the relay wear

- [ ] In `platformio.ini`, section `[env:esp32s2_counter_preset]`, the six
      values from 0.1 entered **unchanged** (plain numbers):

      HPN_PRESET_HEATING = Relay 0    HPN_PRESET_PUMP3 = Relay 3
      HPN_PRESET_PUMP1   = Relay 1    HPN_PRESET_MIX1  = Relay 4
      HPN_PRESET_PUMP2   = Relay 2    HPN_PRESET_MIX2  = Relay 5

- [ ] `pio run -e esp32s2_counter_preset`
- [ ] Settings → Firmware Update: upload
      `.pio/build/esp32s2_counter_preset/firmware.bin`
- [ ] After the reboot: Event Log shows **1614** (counter preset)
- [ ] Diagnostics: the six counters show the recorded values
- [ ] Optional: set the values in `platformio.ini` back to 0

---

## 8. Final firmware

- [ ] Settings → Firmware Update: upload the normal
      `.pio/build/esp32s2/firmware.bin`
- [ ] After the reboot the counters still show the preset values
- [ ] About 2 minutes later the image is confirmed (`http://<ip>/maintenance`:
      image state *valid*)
- [ ] Settings → Firmware Update: **export the configuration** as a backup
- [ ] Mains reconnected; first heating run watched on the Dashboard

---

## Recovery

| Situation | What to do |
|---|---|
| Not on Wi-Fi after step 1 | Fallback AP after 2 minutes (step 2). Without a login it returns to ESPHome after 10 minutes |
| New image crashes | The bootloader returns to the previous image by itself (if it supports rollback) |
| Migration refused | The reason is on the page; nothing was changed |
| Power lost during the migration | Usually boots on the old or the new layout. A loss in the few milliseconds of the table write needs USB: `pio run -e esp32s2 -t upload` |
| Portal shows only the maintenance page | Filesystem empty: upload `littlefs.bin` again (step 4) |
| Password forgotten | Over USB: `pio run -e esp32s2_password_reset -t upload`, log in with the factory passwords, set new ones, upload the normal firmware |
| Settings reset after a later update | Saved settings use a new format; the build's defaults apply. Re-enter them or import the exported configuration |
