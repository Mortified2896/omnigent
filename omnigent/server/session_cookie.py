"""Validated cookie namespaces for instances sharing one browser hostname."""

import os
import re


def cookie_suffix_from_env() -> str:
    suffix = os.environ.get("OMNIGENT_SESSION_COOKIE_SUFFIX", "")
    if suffix and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", suffix):
        raise RuntimeError("OMNIGENT_SESSION_COOKIE_SUFFIX must be 1-32 letters, digits, _ or -")
    return suffix


def session_cookie_name(secure: bool, suffix: str) -> str:
    name = "__Host-ap_session" if secure else "ap_session"
    return f"{name}-{suffix}" if suffix else name
