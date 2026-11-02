"""HeatpumpsN observer."""
try:
    from importlib.metadata import version as _version

    __version__ = _version("observer")
except Exception:                     # running from a source tree without installation
    __version__ = "unknown"
