#!/usr/bin/env python3
"""Collect the final HIL plant parameter set into simulation/plant_parameters.json.

Each step writes its own parameter JSON; this script copies the final
(accepted) parameter block of every finished step into one file, together
with provenance (source file, SHA-256 of the source, report version and the
acceptance status stated by that step).

Run: .venv/bin/python simulation/collect_parameters.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

SIM = Path(__file__).resolve().parent
OUT = SIM / "plant_parameters.json"

# step key -> (parameter file, report, extractor)
SOURCES = {
    "step2_hx": dict(
        file="step2/step2_params_v71.json",
        report="step2/step2_hx_dynamics_report_v71.md",
        model="step2/hx_model_v71.py",
        describe="HX outlet / T_incoming model (inputs: T_boiler, T_return, T_room, "
                 "mixer position, burner, primary pump, DHW flag, pump relays)",
    ),
    "step3_mixer_static": dict(
        file="step3/step3_params_v1.json",
        report="step3/step3_mixer_static_report_v1.md", model="step3/step3_mixer_static_v1.py",
        describe="3-way mixer static mixing law",
    ),
    "step4_mixer_dynamics": dict(
        file="step4/step4_params.json",
        report=None, model=None,
        describe="mixer actuator, transport delay and forward-sensor dynamics",
    ),
    "step5_floor_slab": dict(
        file="step5/step5_params.json",
        report=None, model=None,
        describe="floor loop / slab / room dynamics (T_forward -> T_return)",
    ),
    "step6_assembly": dict(
        file="step6/step6_params.json",
        report=None, model=None,
        describe="boiler emulator and integration constants",
    ),
}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    out = dict(
        description="Final parameter set of the floor-heating HIL plant simulator. "
                    "Each block is copied verbatim from the step that identified it; "
                    "see the referenced report for definitions, units and validation.",
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        steps={},
        missing=[],
    )
    for key, src in SOURCES.items():
        f = SIM / src["file"]
        if not f.exists():
            out["missing"].append(key)
            continue
        d = json.loads(f.read_text())
        block = dict(
            describe=src["describe"],
            source=src["file"], source_sha256=sha256(f),
            report=src.get("report") or d.get("report"),
            model=src.get("model") or d.get("model"),
            version=d.get("version"),
            parameters=d["parameters"],
            units=d.get("parameter_units"),
            constants=d.get("constants"),
            acceptance=d.get("acceptance") or {
                k: d[k] for k in ("holdout_official_v70", "holdout_observed") if k in d},
        )
        out["steps"][key] = block
    OUT.write_text(json.dumps(out, indent=2, default=float))
    print(f"wrote {OUT} with steps {list(out['steps'])}; missing {out['missing']}")


if __name__ == "__main__":
    main()
