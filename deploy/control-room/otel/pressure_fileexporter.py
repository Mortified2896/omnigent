#!/usr/bin/env python3
"""Negative bound test against a real pinned Collector in an isolated Mac root.

This deliberately demonstrates that asynchronous backup cleanup is not admission.
No production ports, archive, configuration or filesystem flags are changed.
"""

import argparse
import json
import os
import socket
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def run(binary):
    with tempfile.TemporaryDirectory(prefix="collector-write-pressure-") as temporary:
        root = Path(temporary).resolve()
        archive = root / "archive"
        archive.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = root / "collector.yaml"
        config.write_text(f"""receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{port}
exporters:
  file:
    path: {archive}/logs.otlp.json
    format: json
    flush_interval: 1ms
    rotation: {{max_megabytes: 1, max_backups: 1, max_days: 60}}
service:
  telemetry:
    logs:
      level: error
    metrics:
      level: none
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [file]
""")
        child = subprocess.Popen(
            [str(binary), "--config", str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        protected = []
        peak = 0
        successful_requests = 0
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("isolated Collector did not start") from None
                    time.sleep(0.05)
            for _ in range(24):
                payload = json.dumps(
                    {
                        "resourceLogs": [
                            {
                                "scopeLogs": [
                                    {"logRecords": [{"body": {"stringValue": "fixture" * 57000}}]}
                                ]
                            }
                        ]
                    }
                ).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/logs",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    successful_requests += response.status == 200
                # Hold real closed files against the actual asynchronous remover.
                for path in archive.glob("logs.otlp-*.json"):
                    if path not in protected:
                        try:
                            os.chflags(path, stat.UF_IMMUTABLE)
                            protected.append(path)
                        except FileNotFoundError:
                            pass
                peak = max(
                    peak,
                    sum(
                        max(p.stat().st_size, p.stat().st_blocks * 512)
                        for p in archive.iterdir()
                        if p.is_file()
                    ),
                )
                time.sleep(0.03)
            oversized = json.dumps(
                {
                    "resourceLogs": [
                        {
                            "scopeLogs": [
                                {"logRecords": [{"body": {"stringValue": "o" * (2 * 1024**2)}}]}
                            ]
                        }
                    ]
                }
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/logs",
                data=oversized,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    oversized_status = response.status
            except urllib.error.HTTPError as error:
                oversized_status = error.code
            result = {
                "collector_version": subprocess.check_output(
                    [str(binary), "--version"], text=True
                ).strip(),
                "nominal_active_plus_backup_bytes": 2 * 1024**2,
                "peak_observed_bytes_during_writes": peak,
                "protected_closed_segments": len(protected),
                "successful_requests": successful_requests,
                "oversized_http_status": oversized_status,
                "hard_ceiling_enforced": False,
                "mechanism": "actual fileexporter; actual immutable-file cleanup failures",
            }
            if peak <= 2 * 1024**2 or not protected:
                raise AssertionError("fixture did not demonstrate asynchronous cleanup overshoot")
            return result
        finally:
            child.terminate()
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            for path in protected:
                if path.exists():
                    os.chflags(path, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().collector), indent=2))
