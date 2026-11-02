import io
import tarfile

import httpx
import pytest
import yaml

from observer import updates
from observer.agent.library import Library
from observer.updates import UpdateChecker, latest_release, newer, version_key


def test_version_compare():
    assert version_key("0.10.2") > version_key("0.9.9") and newer("0.3.0", "0.2.0")
    assert not newer("0.2.0", "0.2.0") and not newer("", "0.1.0")


RELEASES = [
    {"tag_name": "observer-v0.3.0", "html_url": "u3", "draft": False, "prerelease": False, "assets": []},
    {"tag_name": "observer-v0.4.0", "html_url": "u4", "draft": True, "prerelease": False, "assets": []},
    {"tag_name": "observer-v0.3.5", "html_url": "u35", "draft": False, "prerelease": True, "assets": []},
    {"tag_name": "v0.3.0", "html_url": "lib3", "draft": False, "prerelease": False,
     "assets": [{"name": "observer-library-0.3.0.tgz", "browser_download_url": "https://x/lib-0.3.0.tgz", "size": 1}]},
]


def fake_get(url, **kw):
    return httpx.Response(200, json=RELEASES, request=httpx.Request("GET", url))


def test_latest_release_skips_drafts_prereleases_and_other_prefixes(monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get)
    assert latest_release("gume/HeatpumpsN", "observer-v")["version"] == "0.3.0"
    assert latest_release("gume/observer-library", "v")["assets"][0]["url"] == "https://x/lib-0.3.0.tgz"
    with pytest.raises(ValueError):
        latest_release("not a repo")


def pack(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = {"library.yaml": yaml.safe_dump({"name": "observer-library", "version": version,
                                                  "repository": "gume/observer-library", "tag_prefix": "v"}),
                 "skills/demo/SKILL.md": "---\nversion: 1\ntitle: Demo\ndescription: d\n---\n1. x\n"}
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_checker_reports_observer_and_pack_updates_and_installs_from_url(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "get", fake_get)
    lib = Library(str(tmp_path / "lib"))
    lib.import_archive(pack("0.2.0"))
    checker = UpdateChecker({}, "0.2.0", lambda: lib)
    r = checker.check()
    assert r["observer"]["newer"] and r["observer"]["latest"] == "0.3.0"
    assert r["packs"][0]["newer"] and r["packs"][0]["asset_url"] == "https://x/lib-0.3.0.tgz"
    assert checker.summary()["available"] == ["observer 0.3.0", "observer-library 0.3.0"]
    monkeypatch.setattr(updates, "download", lambda url, max_bytes, timeout=60: pack("0.3.0"))
    assert lib.import_url("https://x/lib-0.3.0.tgz")["version"] == "0.3.0"
    assert checker.check()["packs"][0]["newer"] is False                # installed: no more banner
    assert UpdateChecker({}, "0.3.0", lambda: lib).check()["observer"]["newer"] is False


def test_checker_survives_offline(monkeypatch, tmp_path):
    def offline(*a, **k):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(httpx, "get", offline)
    r = UpdateChecker({}, "0.2.0", lambda: Library(str(tmp_path / "lib"))).check()
    assert r["observer"] is None and "no network" in r["errors"][0]


def test_download_rejects_other_schemes():
    with pytest.raises(ValueError):
        updates.download("file:///etc/passwd", 10)
