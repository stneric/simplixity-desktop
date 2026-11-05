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
