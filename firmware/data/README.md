# LittleFS portal assets

These files are flashed to the ESP32 filesystem with:

```bash
pio run -t buildfs
pio run -t uploadfs
```

For the remote target:

```bash
pio remote run --force-remote -d firmware --target uploadfs
```

`index.html` is the main single-page app. `css/style.css` and `js/app.js` are
shared by every page; `emulation.html` and `js/emulation.js` are the separate
bench-only carrier page, served only when the firmware is built with
`HPN_IO_EMULATION` and deliberately not linked from the main menu.

Every asset has a precompressed `.gz` sibling. The HTTP server serves the
gzip variant when the client sends `Accept-Encoding: gzip`, so regenerate it
after editing the source file:

```bash
gzip -9 -c js/app.js > js/app.js.gz
```

The portal reads live data from `/ws` and REST endpoints under `/api/`; see
[`../README.md`](../README.md) and
[`../../plans/controller node/web_interface.md`](../../plans/controller%20node/web_interface.md).
