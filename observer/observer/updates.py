"""Checks GitHub releases for newer versions of the observer and of installed library packs.

* the observer: releases of ``updates.observer_repository`` (default
  gume/HeatpumpsN) tagged ``observer-v<version>``;
* library packs: each pack's ``library.yaml`` names its ``repository`` and
  ``tag_prefix``; a newer release with a ``.tgz`` asset can be installed in one
  step (Library tab).

Unauthenticated GitHub API calls (60 an hour) are enough for a check every
few hours; ``OBSERVER_GITHUB_TOKEN`` is used when set (private repositories).
The check never changes anything by itself.
"""
from __future__ import annotations

import os
import re
import threading
import time

import httpx

API = "https://api.github.com"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def version_key(text: str) -> tuple:
    """'0.10.2' -> (0, 10, 2); non-numeric parts are ignored, so 'v0.3.0-rc1' -> (0, 3, 0, 1)."""
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def newer(candidate: str, current: str) -> bool:
    return bool(candidate) and version_key(candidate) > version_key(current)


def latest_release(repository: str, tag_prefix: str = "v", token: str | None = None, timeout: float = 20) -> dict | None:
    """The newest non-draft, non-prerelease release whose tag starts with tag_prefix."""
    if not _REPO.match(repository or ""):
        raise ValueError(f"repository {repository!r}: owner/name")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(f"{API}/repos/{repository}/releases", params={"per_page": 30}, headers=headers, timeout=timeout,
                  follow_redirects=True)
    r.raise_for_status()
    best = None
    for rel in r.json():
        tag = rel.get("tag_name") or ""
        if rel.get("draft") or rel.get("prerelease") or not tag.startswith(tag_prefix):
            continue
        version = tag[len(tag_prefix):]
        if not version_key(version):
            continue
        if best is None or version_key(version) > version_key(best["version"]):
            best = {"version": version, "tag": tag, "url": rel.get("html_url"), "published": rel.get("published_at"),
                    "notes": (rel.get("body") or "")[:4000],
                    "assets": [{"name": a.get("name"), "url": a.get("browser_download_url"), "size": a.get("size")}
                               for a in rel.get("assets") or []]}
    return best


class UpdateChecker:
    def __init__(self, cfg: dict, current_version: str, library_fn):
        u = cfg.get("updates") or {}
        self.enabled = bool(u.get("enabled", True))
        self.repository = u.get("observer_repository", "gume/HeatpumpsN")
        self.tag_prefix = u.get("observer_tag_prefix", "observer-v")
        self.interval_s = float(u.get("interval_h", 12)) * 3600
        self.current = current_version
        self.library_fn = library_fn            # () -> Library, for the installed packs
        self.result: dict = {"checked": None, "observer": None, "packs": [], "errors": []}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def check(self) -> dict:
        token = os.environ.get("OBSERVER_GITHUB_TOKEN") or None
        out = {"checked": time.strftime("%Y-%m-%d %H:%M"), "observer": None, "packs": [], "errors": []}
        try:
            rel = latest_release(self.repository, self.tag_prefix, token)
            out["observer"] = {"repository": self.repository, "current": self.current,
                               "latest": rel["version"] if rel else None, "url": rel["url"] if rel else None,
                               "published": rel["published"] if rel else None, "notes": rel["notes"] if rel else "",
                               "newer": bool(rel and newer(rel["version"], self.current))}
        except Exception as exc:                 # offline, rate limit: reported, never fatal
            out["errors"].append(f"observer ({self.repository}): {type(exc).__name__}: {exc}")
        try:
            sources = self.library_fn().sources()
        except Exception as exc:
            sources = []
            out["errors"].append(f"library: {exc}")
        for s in sources:
            if s.kind != "pack" or not s.repository:
                continue
            try:
                rel = latest_release(s.repository, s.tag_prefix or "v", token)
            except Exception as exc:
                out["errors"].append(f"pack {s.name} ({s.repository}): {type(exc).__name__}: {exc}")
                continue
            asset = next((a for a in (rel or {}).get("assets", []) if (a["name"] or "").endswith((".tgz", ".tar.gz"))),
                         None)
            out["packs"].append({"pack": s.id[5:], "name": s.name, "repository": s.repository, "current": s.version,
                                 "latest": rel["version"] if rel else None, "url": rel["url"] if rel else None,
                                 "asset_url": asset["url"] if asset else None,
                                 "newer": bool(rel and asset and newer(rel["version"], s.version))})
        with self._lock:
            self.result = out
        return out

    def summary(self) -> dict:
        with self._lock:
            r = dict(self.result)
        r["enabled"] = self.enabled
        r["available"] = ([f"observer {r['observer']['latest']}"] if r.get("observer") and r["observer"]["newer"] else []) + \
            [f"{p['name']} {p['latest']}" for p in r.get("packs") or [] if p["newer"]]
        return r

    def start(self) -> None:
        if not self.enabled:
            return

        def loop():
            if self._stop.wait(30):              # let the service settle first
                return
            while True:
                self.check()
                if self._stop.wait(self.interval_s):
                    return

        threading.Thread(target=loop, name="update-check", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def download(url: str, max_bytes: int, timeout: float = 60) -> bytes:
    """An archive from an http(s) URL (redirects followed), at most max_bytes."""
    if not re.match(r"^https?://", url or ""):
        raise ValueError("URL: http:// or https:// only")
    data = bytearray()
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as r:
        if r.status_code != 200:
            raise ValueError(f"download failed: HTTP {r.status_code}")
        for chunk in r.iter_bytes():
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError(f"download larger than {max_bytes // 1024 // 1024} MB")
    return bytes(data)
