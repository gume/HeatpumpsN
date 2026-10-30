# Partition layout and migration from ESPHome

The controller replaces an ESPHome installation over the air, in two stages
and without a separate loader image. Code: `src/partition_layout.cpp`,
`src/maintenance.html`, `src/wifi_client.cpp` (fallback AP).

## 1. The tables

All 4 MB builds use one table (`partitions.csv`, copied to `src/`). Its
otadata and both app slots are where ESPHome's 4 MB layout has them, so an
image from either system boots from either slot under either table.

| Partition | ESPHome (esp-idf, 4 MB) | This firmware | Previous bench layout |
|---|---|---|---|
| otadata | 0x9000 · 8 KB | 0x9000 · 8 KB | 0xE000 · 8 KB |
| phy_init | 0xB000 · 4 KB | — | — |
| nvs | 0x390000 · 436 KB | 0xB000 · 20 KB | 0x9000 · 20 KB |
| app0 | 0x10000 · 1.75 MB | 0x10000 · 1.75 MB | 0x10000 · 1.56 MB |
| app1 | 0x1D0000 · 1.75 MB | 0x1D0000 · 1.75 MB | 0x1A0000 · 1.56 MB |
| spiffs (LittleFS) | — | 0x390000 · 384 KB | 0x330000 · 768 KB |
| coredump | — | 0x3F0000 · 64 KB | 0x3F0000 · 64 KB |

* NVS at 0xB000 takes the place of phy_init and the free space after it. The
  PHY calibration data is built into the image (the default
  `CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION=n`), so the partition is not needed.
  20 KB holds the configuration, the security blob, the MPC parameter sets
  and the event log with room to spare.
* The filesystem holds only the gzip copies of the portal files
  (`remote_upload.json` builds `data_fs` from `data/`). The emulation pages
  are left out of builds without `HPN_IO_EMULATION`.
* The last 4 KB sector (0x3FF000) briefly holds the settings stash during a
  migration. It lies in the coredump partition, so coredumps lose nothing that
  matters.

## 2. The two stages

```
ESPHome ──(1) ESPHome OTA──▶ this firmware on the ESPHome table
        ──(2) "Migrate" on /maintenance──▶ this firmware on its own table
        ──(3) upload littlefs.bin──▶ full portal
```

### Stage 1: ESPHome uploads this firmware

From a machine in the controller's network, with the ESPHome project:

```
esphome upload heatpumps.yaml --device <controller-ip> --file firmware.bin
```

`firmware.bin` is `.pio/build/esp32s2/firmware.bin` (LOLIN S2 mini). ESPHome
asks for its OTA password; it is not stored in this repository. ESPHome's
`web_server` has OTA disabled, so the native OTA (port 3232) is the only way.

The new firmware then starts on the ESPHome table:

* NVS is ESPHome's partition at 0x390000; ESPHome's keys are ignored and the
  controller starts with its default configuration (Wi-Fi `Testing`).
* There is no filesystem, so `/` serves the built-in **maintenance page**
  (also always at `/maintenance`).
* The controller runs normally; the board pin map matches the ESPHome
  configuration (HomeNet Heating control v0.1).

If Wi-Fi does not connect, the **fallback AP** opens (section 4).

### Stage 2: migration

On `/maintenance`, log in as `admin` and press **Migrate partition table and
reboot**, or call `POST /api/system/partitions/migrate` (HPN1-signed, admin).
`GET /api/system/partitions` shows the layout, the running slot, the image
state and, as hex, the current and the built-in target table.

The migration refuses (409 with the reason) when:

* the table is already the target table;
* the running image is not at 0x10000 or 0x1D0000 (on the previous bench
  layout an image in app1 at 0x1A0000: upload the firmware once more, it lands
  in app0);
* the running image is larger than 1.75 MB;
* the running image is still `pending_verify` (section 3).

Steps, in this order:

1. stash `hpncfg/config`, `hpnsec/blob`, `hpnsec/seq` and `hpnmpc/blob` in the
   sector at 0x3FF000 (magic, length, CRC32);
2. write otadata at 0x9000 for the running slot, state valid;
3. erase 0xB000–0x10000 (the new NVS) and 0x390000–0x3FF000 (the new
   filesystem and coredump), one 4 KB sector at a time with a short pause:
   64 KB block erases stall flash access long enough to trip the T_io
   watchdog (seen on the bench: reset during the first rehearsal);
4. write the table sector at 0x8000 through the flash driver with its
   "dangerous write" guard (the chip's `region_protected` hook) cleared for
   this one write, read it back and compare; on a mismatch it tries once
   more, then puts the old table back. The ROM flash routines are not used:
   under IDF 5 they hang (seen on the bench);
5. reboot. During start-up the stash is written into the new NVS and erased
   (`restoredAfterMigration: true` in the partitions status).

What a power loss leaves behind:

| Lost during | Result |
|---|---|
| step 1 | nothing has changed yet |
| steps 2–3 | the old table still applies; the running image boots (its otadata, or the ota_0 default) and restores this firmware's settings from the stash into the old NVS; migrate again |
| step 4 | the 4 KB table sector is half written; the bootloader finds no valid table and the device needs USB. The window is a few tens of milliseconds |
| after step 4 | boots the new layout normally, restores the stash |

A failed migration is logged (events 1711 erase, 1712 table write); 1710 marks
the start and 1713 a successful write. The last step reached (1 stash,
2 otadata, 3 NVS, 4 data areas, 5 table, 6 old table back, 7 restart), the
flash address and the elapsed time are kept in RTC memory across a reset and
reported as `lastMigration` in `GET /api/system/partitions`. During the
migration the T_io health check is suspended for 30 s
(`tasks::maintenance_grace`), because the erases stall T_control.

### Stage 3: portal files

Upload `littlefs.bin` (`.pio/build/esp32s2/littlefs.bin`) on the maintenance
page (target "Portal files"), or `pio run -e esp32s2 -t uploadfs` with
`HPN_PORTAL_PASSWORD` set. After the reboot `/` is the full portal again.

## 3. OTA image confirmation and rollback

The bootloader in the Arduino core is built with
`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. After an OTA it starts the new image
as `pending_verify`, and the firmware (not the Arduino start-up code:
`verifyRollbackLater()` returns true) decides:

* marked **valid** after 2 minutes of running, as soon as Wi-Fi is connected
  or an admin has logged in (event 1720);
* **rolled back** to the previous image if neither happens within 10 minutes
  (event 1721). A crash loop before that also ends in the previous image,
  because the bootloader never boots a `pending_verify` image twice.

The bootloader is never updated over the air. A board that came from ESPHome
keeps ESPHome's bootloader; if that one was built without rollback support,
new images stay in state `new` and are never rolled back. The migration treats
`new` as confirmed.

## 4. Wi-Fi fallback AP

The controller opens an access point (`ap_ssid`, default `HeatpumpsN-Setup`,
WPA2 password `ap_password`, default `heatpumps-setup`; never open) when:

* no SSID is configured;
* the station has not connected within 2 minutes after boot;
* the station has been down for 5 minutes.

The portal (and the maintenance page) is at `http://192.168.4.1/`. Station
retries continue in the background, every 2 minutes while a client is on the
AP so the AP channel stays usable. The AP closes once the station is connected
and no client has been on the AP for a minute. Events: 1012 AP started, 1013
AP closed.

## 5. Bench rehearsal (development ESP32)

The bench devkit started on the previous layout. Reproduce:

```
export HPN_PORTAL_PASSWORD=<admin password>
pio run -e esp32dev_sim_ota -t upload      # until the running slot is app0 (0x10000)
# wait for "image state: valid" (≤ 2 min)
python3 tools/partition_check.py           # compares the device's target table with partitions.bin
# Migrate on /maintenance (or POST /api/system/partitions/migrate)
pio run -e esp32dev_sim_ota -t uploadfs
```

## 6. Password recovery

The keys live only in NVS (`hpnsec/blob`) and a migration carries them over.
A forgotten password needs the USB cable and the one-shot recovery build
`esp32s2_password_reset`; see `security.md`, "Forgotten password".
