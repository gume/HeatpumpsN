"""Inside the sandbox: read-only access to the house's data and the tool library.

    from obs import data, tools        # and obs.sim: the controller's mixer control, simulated
    df = data.series(["heatpumpsn/controller/state/temp_mix"], start="-7d", every="1m")
    starts = tools.load("on_periods").run(df["..."])

There is no network in the sandbox. ``data`` talks to the observer through a
Unix socket bound into the sandbox (``OBS_SOCKET``); the observer checks every
request and reads the database with a read-only token the code never sees.
"""
from . import data, tools  # noqa: F401
