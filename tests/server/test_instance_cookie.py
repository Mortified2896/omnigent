"""Cookie namespaces must remain secure and backward compatible."""

from dataclasses import replace

import pytest

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.session_cookie import cookie_suffix_from_env


def test_independent_cookie_names(monkeypatch):
    monkeypatch.delenv("OMNIGENT_SESSION_COOKIE_SUFFIX", raising=False)
    base = AccountsConfig(b"x" * 32, 8, "https://peer.example:1111", None, 72, 10)
    assert base.session_cookie_name == "__Host-ap_session"
    monkeypatch.setenv("OMNIGENT_SESSION_COOKIE_SUFFIX", "O1")
    o1 = AccountsConfig(b"x" * 32, 8, "https://peer.example:1111", None, 72, 10)
    monkeypatch.setenv("OMNIGENT_SESSION_COOKIE_SUFFIX", "O2")
    o2 = AccountsConfig(b"y" * 32, 8, "https://peer.example:2222", None, 72, 10)
    assert o1.session_cookie_name == "__Host-ap_session-O1"
    assert o2.session_cookie_name == "__Host-ap_session-O2"
    assert o1.secure_cookies and o2.secure_cookies
    assert replace(o1, base_url="http://localhost:1111").session_cookie_name == "ap_session-O1"


@pytest.mark.parametrize("suffix", ["a;b", "x y", "x\n", "a" * 33, "__Host-x/", "é"])
def test_invalid_cookie_suffix_fails_closed(monkeypatch, suffix):
    monkeypatch.setenv("OMNIGENT_SESSION_COOKIE_SUFFIX", suffix)
    with pytest.raises(RuntimeError, match="OMNIGENT_SESSION_COOKIE_SUFFIX"):
        cookie_suffix_from_env()
