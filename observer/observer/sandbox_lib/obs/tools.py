"""The tool library: generic analysis functions with a manifest, tests and an audit flag.

    tools.list()                       # {name: {purpose, audited, tests_passed, origin}}
    on = tools.load("on_periods")      # the module; call on.run(...)
    tools.call("on_periods", series)   # the same in one step

Loading an unaudited tool prints a warning on stderr and is recorded in the
run: say in the report when a result depends on one.
"""
import importlib.util
import sys

from ._client import call as _call

_loaded = {}


def list():  # noqa: A001 - mirrors the portal's wording
    return _call("tools")


def load(name):
    if name in _loaded:
        return _loaded[name]
    index = _call("tools")
    info = index.get(name)
    if info is None:
        raise KeyError(f"no tool {name!r}; available: {', '.join(sorted(index))}")
    path = info["path"]
    spec = importlib.util.spec_from_file_location(f"obs_tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _call("tool_used", name=name)
    if not info.get("audited"):
        print(f"[obs] tool {name!r} is NOT audited by the owner", file=sys.stderr)
    _loaded[name] = module
    return module


def call(name, *args, **kwargs):
    return load(name).run(*args, **kwargs)
