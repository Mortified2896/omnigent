#!/usr/bin/env python3
"""Read-only, privacy-safe preflight for two Codex OTLP/HTTP config files.

This checks supplied TOML, not Codex config precedence, runtime export, producer
identity, archive location, or retention. A passing result is not live proof.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_CONFIG_BYTES = 1_048_576
SIGNALS = {
    "logs": "exporter",
    "traces": "trace_exporter",
    "metrics": "metrics_exporter",
}


def local_origin(value: str) -> tuple[str, str, int]:
    """Validate an explicit loopback origin without making a network request."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        is_local = host == "localhost"
        if host and not is_local:
            is_local = ipaddress.ip_address(host).is_loopback
        if (
            parsed.scheme not in {"http", "https"}
            or not is_local
            or port is None
            or port == 0
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
    except ValueError:
        raise ValueError("expected an explicit loopback HTTP(S) origin and port") from None
    assert host is not None
    return parsed.scheme, host, port


def endpoint_matches(value: object, origin: tuple[str, str, int], signal: str) -> bool:
    """Compare the destination and exact OTLP signal path without echoing URLs."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            (parsed.scheme, parsed.hostname, parsed.port) == origin
            and parsed.path == f"/v1/{signal}"
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def read_config(path: Path) -> tuple[dict[str, Any] | None, str, str | None]:
    """Bound file reads and never expose parser errors, paths, or config values."""
    try:
        # Nonblocking open avoids hanging on a FIFO accidentally used as a path.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None, "not_regular_file", None
            if before.st_size > MAX_CONFIG_BYTES:
                return None, "config_too_large", None
            content = stream.read(MAX_CONFIG_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(content) > MAX_CONFIG_BYTES:
            return None, "config_too_large", None
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None, "changed_during_read", None
    except FileNotFoundError:
        return None, "missing", None
    except OSError:
        return None, "unreadable", None
    try:
        document = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return None, "invalid_toml", None
    return document, "ok", hashlib.sha256(content).hexdigest()


def inspect_config(path: Path, origin: tuple[str, str, int]) -> dict[str, Any]:
    """Return only allowlisted categorical findings and a valid-file digest."""
    document, read_status, digest = read_config(path)
    result: dict[str, Any] = {"read_status": read_status, "issues": []}
    if document is None:
        result["issues"].append("config_not_inspected")
        return result
    result["config_sha256"] = digest
    otel = document.get("otel")
    if not isinstance(otel, dict):
        result["issues"].append("missing_otel_table")
        return result
    if otel.get("log_user_prompt") is not False:
        result["issues"].append("explicit_prompt_privacy_required")
    signals: dict[str, Any] = {}
    for signal, field in SIGNALS.items():
        findings: list[str] = []
        exporter = otel.get(field)
        if not isinstance(exporter, dict) or set(exporter) != {"otlp-http"}:
            findings.append("explicit_otlp_http_exporter_required")
        else:
            config = exporter["otlp-http"]
            if not isinstance(config, dict):
                findings.append("invalid_exporter_shape")
            else:
                if not endpoint_matches(config.get("endpoint"), origin, signal):
                    findings.append("collector_endpoint_mismatch")
                if config.get("protocol") not in ("binary", "json"):
                    findings.append("explicit_supported_http_protocol_required")
        signals[signal] = {"matches": not findings, "issues": findings}
    result["signals"] = signals
    if any(item["issues"] for item in signals.values()):
        result["issues"].append("signal_configuration_mismatch")
    return result


def preflight(desktop: Path, omnigent: Path, collector_origin: str) -> dict[str, Any]:
    """Check both supplied configs against the operator's verified Collector."""
    origin = local_origin(collector_origin)
    producers = {
        "desktop": inspect_config(desktop, origin),
        "omnigent_codex": inspect_config(omnigent, origin),
    }
    return {
        "schema_version": "codex.otel.config-preflight/v1",
        "scope": "supplied_config_files_only",
        "status": "pass" if all(not row["issues"] for row in producers.values()) else "fail",
        "runtime_verified": False,
        "producers": producers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-config", type=Path, required=True)
    parser.add_argument("--omnigent-config", type=Path, required=True)
    parser.add_argument("--collector-origin", required=True)
    args = parser.parse_args(argv)
    try:
        report = preflight(args.desktop_config, args.omnigent_config, args.collector_origin)
    except ValueError:
        parser.error("--collector-origin must be a loopback HTTP(S) origin with an explicit port")
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
