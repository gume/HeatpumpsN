# Handover Document — Project State & Unfinished Tasks

**Date:** 2026-10-27  
**Model Policy:** Instructions explicitly specify **DO NOT write any further code with this model.** This document outlines what was accomplished, what remains unfinished, and instructions for handover.

---

## 1. Summary of Completed Work (Recent Commits)
- **Overhauled Hardware Settings & Diagnostics Cards:** Updated the portal UI (`app.js`), firmware configuration structures, and documentation to properly support the v0.1 and v1.x hardware profiles, polarity mapping, and status monitoring.
- **Fixed v1.x Interlock Fault Polarity:** Corrected the interlock fault pin logic (`mix_fault_active_low = false`) to reflect active-high assertion (`FAULT = NOR(#M+, #M-)`).
- **Comprehensive Board & Schematic Documentation:** Updated `firmware/docs/boards.md` with explicit notes referencing the schematic files (`plans/schematics/`) and detailing open hardware items for the v1.x design.

---

## 2. Unfinished Tasks & Undone Changes
As per instructions, all unfinished experimental or partially implemented code tasks have been cleaned up / undone, leaving the working tree clean on `main`.

The following items are **unfinished and pending implementation** in future coding sessions:
1. **Physical v1.x Board Fabrication & Final Testing:**
   - The v1.x schematic (`SCH_HomeNet Pumper Schematic v1.1.pdf`) contains open items (such as uncommitted pin mappings for the ESP32-S3 module, floating analog bias references for the TLV9064 current-sense stage, and missing mains protection components).
   - Once the hardware layout is finalised, validate the PCF8574 expansion bus, ADS1115 measurements, and interlock fault line on physical hardware.
2. **Additional Sensor / Peripheral Integration:**
   - Local mixer encoder / I2C peripherals reserved in the UI and documentation.
   - Fine-tuning current calibration slopes against clamp meter measurements for the new MT9221 / TLV9064 sensor front ends.

---

## 3. Instructions for Continuing Work
- **Model constraint:** When resuming development, ensure coding tasks are assigned to a capable coding model if code generation is required, adhering strictly to the prompt guidelines.
- **ESP32 Remote Build & Flash:**
  ```bash
  pio remote run --target upload
  timeout 10s pio remote device monitor -b 115200 || true
  ```
