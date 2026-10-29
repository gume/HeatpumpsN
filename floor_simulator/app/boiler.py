"""Boiler + DHW emulator producing the plant's boiler inputs every 10 s tick.

The controller's `heater` relay is the call for heat. Three modes:

* observed - firing cycles drawn from the Step 6 emulator statistics
  (firing band P10/median/P90, burn duration median/P90, CH modulation median);
* static   - fixed T_boiler while the burner is on;
* off      - burner forced to 0 %, the boiler water decays towards ambient.

DHW (manual or timed) diverts the boiler: the plant's diverter logic removes
heating flow while T_boiler rises towards `tb_dhw_max_median`.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .plant import DT

MODES = ("observed", "static", "off")

TAU_HEAT_S = 240.0         # burner-on approach to the firing target (not identified)
TAU_DHW_S = 420.0          # approach to the DHW peak (not identified)
TAU_IDLE_S = 2123.6        # idle cooling: Step 2 tau_fn
PUMP_OVERRUN_S = 60.0      # primary pump keeps running after the burner stops
OFF_MEDIAN_S = 300.0       # pause between observed firing cycles (not identified)
DHW_BURNER_PCT = 70.0


@dataclass
class BoilerOutput:
    tb: float
    burner_pct: float
    pump_pct: float
    dhw: bool


class BoilerEmulator:
    def __init__(self, stats: dict, seed: int | None = None):
        self.s = stats
        self.rng = random.Random(seed)
        self.mode = "observed"
        self.static_temp = float(stats["tb_ch_steady_median"])
        self.tb = 40.0
        self.burner_on = False
        self.phase_left = 0.0          # remaining seconds of the current burn / pause
        self.fire_target = float(stats["tb_ch_steady_median"])
        self.overrun = 0.0
        self.dhw_on = False
        self.dhw_left: float | None = None   # None = manual (until switched off)

    # -- distributions -------------------------------------------------
    def _lognormal(self, median, p90):
        sigma = math.log(p90 / median) / 1.2816
        return median * math.exp(self.rng.gauss(0.0, sigma))

    def _sample_burn(self):
        return min(self._lognormal(self.s["burner_on_median_s"], self.s["burner_on_p90_s"]), 4 * 3600.0)

    def _sample_band(self):
        return self.rng.triangular(self.s["tb_ch_steady_p10"], self.s["tb_ch_steady_p90"],
                                   self.s["tb_ch_steady_median"])

    # -- controls ------------------------------------------------------
    def set_mode(self, mode: str, static_temp: float | None = None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        if static_temp is not None:
            self.static_temp = min(max(float(static_temp), 20.0), 85.0)
        self.phase_left = 0.0

    def dhw_start(self, duration_s: float | None):
        self.dhw_on = True
        self.dhw_left = float(duration_s) if duration_s else None

    def dhw_stop(self):
        self.dhw_on = False
        self.dhw_left = None

    # -- tick ----------------------------------------------------------
    def step(self, heat_call: bool, t_amb: float) -> BoilerOutput:
        if self.dhw_on and self.dhw_left is not None:
            self.dhw_left -= DT
            if self.dhw_left <= 0:
                self.dhw_stop()
        dhw = self.dhw_on and self.mode != "off"

        burner = 0.0
        if dhw:
            burner = DHW_BURNER_PCT
            self._approach(self.s["tb_dhw_max_median"], TAU_DHW_S)
            self.burner_on = False
            self.phase_left = 0.0
        elif self.mode == "off" or not heat_call:
            self.burner_on = False
            self.phase_left = 0.0
        elif self.mode == "static":
            burner = float(self.s["bp_ch_steady_median"])
            self.tb = self.static_temp
            self.burner_on = True
        else:  # observed cycling
            self.phase_left -= DT
            if self.phase_left <= 0:
                self.burner_on = not self.burner_on
                if self.burner_on:
                    self.phase_left = self._sample_burn()
                    self.fire_target = self._sample_band()
                else:
                    self.phase_left = self._lognormal(OFF_MEDIAN_S, 3 * OFF_MEDIAN_S)
            if self.burner_on:
                burner = float(self.s["bp_ch_steady_median"])
                self._approach(self.fire_target, TAU_HEAT_S)

        if burner <= 0.0:
            self._approach(t_amb, TAU_IDLE_S)
            self.overrun = max(self.overrun - DT, 0.0)
        else:
            self.overrun = PUMP_OVERRUN_S
        pump = 100.0 if (burner > 0.0 or self.overrun > 0.0) else 0.0
        return BoilerOutput(tb=self.tb, burner_pct=burner, pump_pct=pump, dhw=dhw)

    def _approach(self, target, tau):
        self.tb += DT / (tau + DT) * (target - self.tb)

    def status(self) -> dict:
        return dict(mode=self.mode, static_temp=self.static_temp, tb=self.tb,
                    burner_on=self.burner_on, phase_left_s=max(self.phase_left, 0.0),
                    fire_target=self.fire_target, dhw_on=self.dhw_on,
                    dhw_left_s=self.dhw_left)
