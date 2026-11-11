import os
import sys

import pytest

import desktop_api


def scope(**headers: str) -> dict:
    return {"type": "http", "server": ("127.0.0.1", 3210),
            "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]}


@pytest.mark.parametrize("headers", [
    {"host": "127.0.0.1:3210"},
    {"host": "localhost:3210", "sec_fetch_site": "same-origin"},
    {"host": "127.0.0.1:3210", "origin": "http://127.0.0.1:3210", "sec_fetch_site": "same-origin"},
    {"host": "127.0.0.1:3210", "sec_fetch_site": "none"},
])
def test_own_window_is_allowed(headers):
    assert desktop_api.request_allowed(scope(**headers))


@pytest.mark.parametrize("headers", [
    {},
    {"host": "evil.example:3210"},  # DNS rebinding
    {"host": "127.0.0.1:9999"},
    {"host": "127.0.0.1:3210", "origin": "https://evil.example"},  # form posted from another site
    {"host": "127.0.0.1:3210", "origin": "null"},
    {"host": "127.0.0.1:3210", "sec_fetch_site": "cross-site"},
    {"host": "127.0.0.1:3210", "sec_fetch_site": "same-site"},  # another port on 127.0.0.1
])
def test_other_pages_are_refused(headers):
    assert not desktop_api.request_allowed(scope(**headers))


def test_write_env_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# note\nOTHER=1\nexport OPENROUTER_API_KEY=old\nOPENROUTER_API_KEY=dup\n")
    desktop_api.write_env(env, {"OPENROUTER_API_KEY": "new", "GROQ_API_KEY": "g"})
    assert env.read_text() == "# note\nOTHER=1\nOPENROUTER_API_KEY=new\nGROQ_API_KEY=g\n"
    desktop_api.write_env(env, {"GROQ_API_KEY": ""})
    assert env.read_text() == "# note\nOTHER=1\nOPENROUTER_API_KEY=new\n"
    assert not (tmp_path / ".env.tmp").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="no Unix permissions")
def test_write_env_is_private(tmp_path):
    env = tmp_path / ".env"
    desktop_api.write_env(env, {"A": "1"})
    assert os.stat(env).st_mode & 0o777 == 0o600



def _file(path, oid="a" * 64, size=10):
    return {"type": "file", "path": path, "size": 100, "lfs": {"oid": oid, "size": size}}


MANIFEST = {"setting": "title", "label": "Title", "tag": "simplixity-title:0.8b", "quant": "Q4_K_M",
            "ollama": {"renderer": "qwen3.5", "parser": "qwen3.5", "parameters": {"num_ctx": 8192}, "evil": 1}}


def test_newest_version_of_the_quant_is_used():
    tree = [_file("x-v1-Q4_K_M.gguf", "1" * 64), _file("x-v4-Q4_K_M.gguf", "4" * 64, 541),
            _file("x-v10-Q8_0.gguf", "8" * 64), _file("README.md"), {"type": "directory", "path": "training"}]
    m = desktop_api._model_from("stneric/simplixity-title-0.8b-GGUF", MANIFEST, tree)
    assert (m["file"], m["version"], m["digest"], m["size"]) == ("x-v4-Q4_K_M.gguf", "v4", "sha256:" + "4" * 64, 541)
    assert m["key"] == "simplixity-title-0.8b" and m["tag"] == "simplixity-title:0.8b"
    assert m["create"] == {"renderer": "qwen3.5", "parser": "qwen3.5", "parameters": {"num_ctx": 8192}}
    assert desktop_api.SLOTS[m["setting"]] == "title-model"


@pytest.mark.parametrize("manifest", [
    {**MANIFEST, "setting": "nonsense"},
    {**MANIFEST, "tag": "no-tag"},
    {**MANIFEST, "quant": "Q2_K"},   # no file of that quant
    "not a dict",
])
def test_unusable_repos_are_skipped(manifest):
    assert desktop_api._model_from("r/x", manifest, [_file("x-v1-Q4_K_M.gguf")]) is None


class FakeStore:
    def __init__(self):
        self.staged = None

    def status(self):
        return {"available": {"version": "v9"}, "staged": self.staged, "busy": True}

    def install(self):
        self.staged = "v9"


def test_finished_install_is_not_reported_busy():
    p = desktop_api.UpdateProgress(FakeStore())
    p.start()
    p.thread.join(2)
    st = p.status()
    assert st["busy"] is False and st["progress"] is None and st["staged"] == "v9"


def test_failed_install_says_why():
    store = FakeStore()
    store.install = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
    p = desktop_api.UpdateProgress(store)
    p.start()
    p.thread.join(2)
    assert p.status()["progress"]["phase"] == "error" and p.status()["progress"]["error"] == "disk full"


def test_relaunch_waits_for_this_process(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("APPIMAGE", "/home/u/Apps/Simplixity x.AppImage")
    cmd = desktop_api.relaunch_command(4242)
    assert cmd[:2] == ["/bin/sh", "-c"] and "kill -0 4242" in cmd[2] and "'/home/u/Apps/Simplixity x.AppImage'" in cmd[2]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/Applications/Simplixity.app/Contents/MacOS/Simplixity")
    assert "open -n /Applications/Simplixity.app" in desktop_api.relaunch_command(1)[2]
