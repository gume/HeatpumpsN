"""Rootless sandbox for agent-written Python.

The default runtime is bubblewrap (``bwrap``): unprivileged user namespaces,
no root anywhere. Each run sees only

* ``/usr`` and the sandbox's own Python environment, read-only;
* the run's workspace at ``/work``, read-write (data files, plots, notes);
* the ``obs`` client library (``/opt/obs/lib``) and the tool folders of
  every library source (``/opt/obs/tools/<source>``), read-only;
* the run's data socket (``/run/obs/data.sock``, ``OBS_SOCKET``): the only
  way out, to the observer's read-only data service (``broker.py``);
* an empty ``/tmp``, ``/proc`` and a minimal ``/dev``;

with **no network** (all namespaces unshared), a cleared environment, and
limits: memory and number of processes through a systemd user scope (cgroup
v2), CPU seconds, file size and open files through rlimits, and a wall-clock
timeout. The home directory, the observer's data directory (``.env`` with
the tokens) and the database are not reachable from inside.

``docker`` and ``podman`` are alternative runtimes (``--network none``,
read-only root, all capabilities dropped, no-new-privileges, nobody user),
for hosts without bwrap; podman rootless is the stronger of the two.

The sandbox's Python environment is created by ``observer sandbox-setup``
(numpy, pandas, scipy, matplotlib) in the data directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox_lib")
PACKAGES = ("numpy", "pandas", "scipy", "matplotlib", "pytest")
OUTPUT_LIMIT = 12_000


@dataclass
class Limits:
    wall_s: int = 120
    cpu_s: int = 120
    memory_mb: int = 1024
    tasks: int = 64
    file_mb: int = 200
    open_files: int = 256


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    new_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out, "duration_s": round(self.duration_s, 2),
                "stdout": self.stdout, "stderr": self.stderr, "new_files": self.new_files}


def _user_systemd() -> bool:
    """systemd-run --user needs a running user manager (not in containers or CI runners)."""
    return bool(shutil.which("systemd-run")) and os.path.exists(f"/run/user/{os.getuid()}/systemd/private")


def _tail(text: str, limit: int = OUTPUT_LIMIT) -> str:
    return text if len(text) <= limit else f"... ({len(text) - limit} characters cut)\n" + text[-limit:]


def _files(root: str) -> dict[str, float]:
    out = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            p = os.path.join(base, n)
            out[os.path.relpath(p, root)] = os.path.getmtime(p)
    return out


class Sandbox:
    def __init__(self, venv: str, runtime: str = "bwrap", limits: Limits | None = None,
                 image: str = "python:3.11-slim", tool_mounts=None):
        self.venv = os.path.abspath(venv)
        self.runtime = runtime
        self.limits = limits or Limits()
        self.image = image
        # Read-only mounts: the obs client library and, from `tool_mounts()` at
        # run time (sources change when packs are imported), the tool folders.
        self.tool_mounts = tool_mounts

    @property
    def mounts(self) -> dict[str, str]:
        out = {LIB_DIR: "/opt/obs/lib"}
        for host, target in ((self.tool_mounts() if callable(self.tool_mounts) else self.tool_mounts) or {}).items():
            if host and os.path.isdir(host):
                out[os.path.abspath(host)] = target
        return out

    # --- environment ---------------------------------------------------------------
    @property
    def python(self) -> str:
        return os.path.join(self.venv, "bin", "python")

    def base_prefix(self) -> str:
        """Where the venv's interpreter lives (its standard library must be visible inside)."""
        out = subprocess.run([self.python, "-c", "import sys; print(sys.base_prefix)"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    def status(self) -> dict:
        problems = []
        if self.runtime == "bwrap" and not shutil.which("bwrap"):
            problems.append("bwrap is not installed")
        if self.runtime in ("docker", "podman") and not shutil.which(self.runtime):
            problems.append(f"{self.runtime} is not installed")
        if self.runtime == "bwrap" and not os.path.exists(self.python):
            problems.append("sandbox environment missing: run `observer sandbox-setup`")
        cgroup = self.runtime == "bwrap" and _user_systemd()
        return {"runtime": self.runtime, "ready": not problems, "problems": problems, "venv": self.venv,
                "network": "none", "limits": self.limits.__dict__,
                "memory_limit": "cgroup (systemd user scope)" if cgroup else
                ("container" if self.runtime != "bwrap" else "address-space rlimit"),
                "packages": list(PACKAGES)}

    @staticmethod
    def setup(venv: str) -> None:
        """Create the sandbox's Python environment (uses uv when available)."""
        uv = shutil.which("uv")
        if uv:
            subprocess.run([uv, "venv", "--quiet", venv], check=True)
            subprocess.run([uv, "pip", "install", "--quiet", "--python", os.path.join(venv, "bin", "python"),
                            *PACKAGES], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            subprocess.run([os.path.join(venv, "bin", "pip"), "install", "--quiet", *PACKAGES], check=True)

    # --- running code ----------------------------------------------------------------
    def _bwrap(self, workspace: str, script: str, socket_dir: str | None = None) -> list[str]:
        lim = self.limits
        binds = ["--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                 "--symlink", "usr/bin", "/bin"]
        for path in {self.venv, self.base_prefix()}:
            if path and not path.startswith("/usr/"):
                binds += ["--ro-bind", path, path]
        for host, target in self.mounts.items():
            binds += ["--ro-bind", host, target]
        env = ["--setenv", "PYTHONPATH", "/opt/obs/lib"]
        if socket_dir:
            binds += ["--bind", socket_dir, "/run/obs"]
            env += ["--setenv", "OBS_SOCKET", "/run/obs/data.sock"]
        bw = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
              *binds, "--ro-bind-try", "/etc/fonts", "/etc/fonts", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
              "--bind", workspace, "/work", "--chdir", "/work",
              "--setenv", "HOME", "/work", "--setenv", "PATH", f"{self.venv}/bin:/usr/bin",
              "--setenv", "MPLBACKEND", "Agg", "--setenv", "MPLCONFIGDIR", "/tmp",
              "--setenv", "PYTHONDONTWRITEBYTECODE", "1", *env,
              # -s -B (no user site, no bytecode); the environment is already
              # cleared, so PYTHONPATH is ours and -I is not needed.
              self.python, "-s", "-B", f"/work/{script}"]
        rl = ["prlimit", f"--cpu={lim.cpu_s}", f"--fsize={lim.file_mb * 1024 * 1024}",
              f"--nofile={lim.open_files}", "--core=0"]
        if _user_systemd():
            scope = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                     "-p", f"MemoryMax={lim.memory_mb}M", "-p", "MemorySwapMax=0", "-p", f"TasksMax={lim.tasks}"]
            return scope + rl + ["--"] + bw
        return rl + [f"--as={lim.memory_mb * 1024 * 1024 * 4}", f"--nproc={lim.tasks}", "--"] + bw

    def _container(self, workspace: str, script: str, socket_dir: str | None = None) -> list[str]:
        lim = self.limits
        mounts = []
        for host, target in self.mounts.items():
            mounts += ["-v", f"{host}:{target}:ro"]
        if socket_dir:
            mounts += ["-v", f"{socket_dir}:/run/obs", "-e", "OBS_SOCKET=/run/obs/data.sock"]
        return [self.runtime, "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                *mounts, "-e", "PYTHONPATH=/opt/obs/lib",
                "--security-opt", "no-new-privileges", "--user", "65534:65534",
                "--memory", f"{lim.memory_mb}m", "--memory-swap", f"{lim.memory_mb}m",
                "--pids-limit", str(lim.tasks), "--cpus", "1", "--tmpfs", "/tmp",
                "--ulimit", f"cpu={lim.cpu_s}", "--ulimit", f"fsize={lim.file_mb * 1024 * 1024}",
                "-e", "MPLBACKEND=Agg", "-e", "MPLCONFIGDIR=/tmp", "-e", "HOME=/work",
                "-v", f"{workspace}:/work", "-w", "/work", self.image, "python", "-s", "-B", f"/work/{script}"]

    def run(self, code: str, workspace: str, name: str = "snippet", socket_dir: str | None = None) -> Result:
        """Run `code` with the workspace as /work; the script is kept in .runs/ for the audit."""
        workspace = os.path.abspath(workspace)
        runs = os.path.join(workspace, ".runs")
        os.makedirs(runs, exist_ok=True)
        script = os.path.join(".runs", f"{time.strftime('%H%M%S')}_{name}.py")
        with open(os.path.join(workspace, script), "w", encoding="utf-8") as fp:
            fp.write(code)
        before = _files(workspace)
        cmd = (self._bwrap(workspace, script, socket_dir) if self.runtime == "bwrap"
               else self._container(workspace, script, socket_dir))
        # systemd-run --user needs the user session even when the caller (an
        # MCP client) started us with a minimal environment.
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        start = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.limits.wall_s,
                               stdin=subprocess.DEVNULL, env=env)
            code_, out, err, timed_out = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as exc:
            code_, timed_out = -9, True
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = (exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")) + \
                f"\nkilled: wall-clock limit of {self.limits.wall_s} s"
        if code_ in (137, -9) and not timed_out:
            err += f"\nkilled: probably the memory limit ({self.limits.memory_mb} MB)"
        after = _files(workspace)
        new = sorted(p for p, m in after.items() if before.get(p) != m)
        return Result(code_, _tail(out), _tail(err), time.monotonic() - start, timed_out, new)
