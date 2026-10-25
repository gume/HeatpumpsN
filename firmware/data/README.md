# LittleFS data

Files placed here are flashed to the ESP32 filesystem with:

```bash
pio run -t buildfs
pio run -t uploadfs
```

This is where the local web dashboard's static assets (HTML/CSS/JS) will go,
matching `../plans/controller node/web_interface.md`.
