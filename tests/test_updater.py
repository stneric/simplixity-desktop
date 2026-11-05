"""The parts of updater.py that, if wrong, break installs: unpacking and the start/rollback state."""

import json
import zipfile
from pathlib import Path

import pytest

import updater

SHELL = {"shell": "abc", "serial": 100, "version": "0.2.1"}


def make_zip(path: Path, entries: dict[str, bytes], link: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = (0o120777) << 16
            zf.writestr(info, "/etc/passwd")
    return path


def complete(**extra: bytes) -> dict[str, bytes]:
    return {f"{d}/x": b"" for d in updater.PAYLOAD_REQUIRED if d != "docker-compose.yml"} | {
        "docker-compose.yml": b"", **extra}


def test_extract_complete(tmp_path):
    updater._extract(make_zip(tmp_path / "p.zip", complete()), tmp_path / "out")
    assert (tmp_path / "out" / "app" / "x").is_file()


@pytest.mark.parametrize("bad", ["../evil", "/abs", "app/../../evil", "other/x", "app\\x", "C:x", "app/./x"])
def test_extract_refuses_escapes(tmp_path, bad):
    with pytest.raises(updater.UpdateError):
        updater._extract(make_zip(tmp_path / "p.zip", complete(**{bad: b"!"})), tmp_path / "out")


def test_extract_refuses_links_and_case_twins(tmp_path):
    with pytest.raises(updater.UpdateError):
        updater._extract(make_zip(tmp_path / "a.zip", complete(), link="app/link"), tmp_path / "a")
    with pytest.raises(updater.UpdateError):
        updater._extract(make_zip(tmp_path / "b.zip", complete(**{"app/X": b""})), tmp_path / "b")


def test_extract_refuses_incomplete_and_oversized(tmp_path, monkeypatch):
    with pytest.raises(updater.UpdateError, match="lacks"):
        updater._extract(make_zip(tmp_path / "a.zip", {"app/x": b""}), tmp_path / "a")
    monkeypatch.setattr(updater, "MAX_UNPACKED", 10)
    with pytest.raises(updater.UpdateError, match="more than"):
        updater._extract(make_zip(tmp_path / "b.zip", complete(**{"app/big": b"0" * 11})), tmp_path / "b")


def install(store: updater.Store, serial: int, shell: str = "abc") -> None:
    d = store.root / str(serial)
    d.mkdir(parents=True)
    (d / "payload.json").write_text(json.dumps({"version": f"v{serial}", "serial": serial, "shell": shell}))


def state(store: updater.Store) -> dict:
    return json.loads((store.root / "state.json").read_text())


def test_new_payload_starts_and_is_kept(tmp_path):
    s = updater.Store(tmp_path, SHELL, True)
    install(s, 200)
    install(s, 150)  # an older one nobody returns to
    s._save({"active": 200, "previous": None, "pending": True, "attempted": False})
    assert s.boot() == s.root / "200"
    assert state(s)["attempted"] is True
    s.confirm()
    assert state(s) == {"active": 200, "previous": None, "pending": False, "attempted": False}
    assert not (s.root / "150").exists() and (s.root / "200").exists()


def test_payload_that_did_not_start_is_rolled_back(tmp_path):
    install(updater.Store(tmp_path, SHELL, True), 200)
    s = updater.Store(tmp_path, SHELL, True)
    install(s, 300)
    s._save({"active": 300, "previous": 200, "pending": True, "attempted": True})
    assert s.boot() == s.root / "200"
    assert state(s)["active"] == 200 and state(s)["pending"] is False


def test_payload_for_another_shell_or_older_is_ignored(tmp_path):
    s = updater.Store(tmp_path, SHELL, True)
    install(s, 200, shell="other")
    s._save({"active": 200, "previous": None, "pending": False, "attempted": False})
    assert s.boot() is None
    install(s, 50)
    s._save({"active": 50, "previous": None, "pending": False, "attempted": False})
    assert s.boot() is None and state(s)["active"] is None


def test_offer_needs_installer_when_shell_differs(tmp_path):
    s = updater.Store(tmp_path, SHELL, True)
    s.found = {"manifest": {"version": "0.3", "serial": 500, "shell": "new"}, "page": "p"}
    assert s.status()["available"] == {"version": "0.3", "installer_only": True, "page": "p"}
    s.found["manifest"]["serial"] = 100
    assert s.status()["available"] is None


def test_fingerprint_ignores_line_endings(tmp_path):
    for name in updater.SHELL_FILES:
        (tmp_path / name).write_bytes(b"a\nb\n")
    lf = updater.fingerprint(tmp_path)
    for name in updater.SHELL_FILES:
        (tmp_path / name).write_bytes(b"a\r\nb\r\n")
    assert updater.fingerprint(tmp_path) == lf


def test_install_then_next_start_runs_it(tmp_path, monkeypatch):
    """The whole path a click on "Install" takes, with GitHub replaced by a local file."""
    zip_path = make_zip(tmp_path / "release.zip", complete())
    found = {"manifest": {"version": "0.3.0", "serial": 300, "shell": "abc"}, "zip": {"name": "z"}, "page": "p"}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(updater, "_client", Client)
    monkeypatch.setattr(updater, "_latest", lambda client: found)
    monkeypatch.setattr(updater, "_download", lambda client, asset, out, limit: out.write(zip_path.read_bytes()))

    data = tmp_path / "data"
    s = updater.Store(data, SHELL, True)
    assert s.install()["staged"] == "0.3.0"
    assert not list(s.root.glob(".incoming-*"))

    nxt = updater.Store(data, SHELL, True)  # the next start
    assert nxt.boot() == nxt.root / "300"
    nxt.confirm()
    assert nxt.status()["version"] == "0.3.0" and nxt.status()["available"] is None
    with pytest.raises(updater.UpdateError, match="no newer"):
        nxt.install()
