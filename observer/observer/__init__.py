"""HeatpumpsN observer."""
import os
import subprocess


def _version() -> str:
    """The released version; a development checkout adds its git state: 0.3.1+2.gaf7372f (.dirty)."""
    try:
        from importlib.metadata import version

        released = version("observer")
    except Exception:                     # running from a source tree without installation
        released = "unknown"
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, "..", "..", ".git")):
        return released                   # an installed release (container, bundle)
    try:
        d = subprocess.run(["git", "describe", "--tags", "--match", "observer-v*", "--dirty"], cwd=here,
                           capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return released
    if not d.startswith("observer-v"):
        return released
    d = d[len("observer-v"):]
    dirty = d.endswith("-dirty")
    d = d.removesuffix("-dirty")
    parts = d.rsplit("-", 2)              # "0.3.1" on the tag, "0.3.1-2-gaf7372f" after it
    out = f"{parts[0]}+{parts[1]}.{parts[2]}" if len(parts) == 3 else d
    return out + ("+dirty" if dirty and len(parts) != 3 else ".dirty" if dirty else "")


__version__ = _version()
