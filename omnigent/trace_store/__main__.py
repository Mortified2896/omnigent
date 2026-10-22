"""python -m omnigent.trace_store collect --config SOURCES.json --db analysis.sqlite"""

import argparse
import glob
import json
import os
import sqlite3
import time
from pathlib import Path

from .adapters import ADAPTERS, jsonl, otlp_record
from .store import Store


def collect(store: Store, config: dict):
    failures = []
    for spec in config["sources"]:
        paths = sorted(glob.glob(spec["path"], recursive=True))
        if not paths and spec.get("optional"):
            continue
        if not paths:
            paths = [spec["path"]]
        for raw_path in paths:
            path = Path(raw_path)
            source = spec["id"] + (
                ":" + str(path) if len(paths) > 1 or glob.has_magic(spec["path"]) else ""
            )
            try:
                with store.db:
                    checkpoint = (
                        jsonl(store, source, path, otlp_record, quarantine=True)
                        if spec.get("quarantine_malformed") and spec["kind"] == "otlp"
                        else ADAPTERS[spec["kind"]](store, source, path)
                    )
                    store.finish(source, spec["kind"], path, checkpoint)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                # No contents, provider responses or potentially secret exception text in logs.
                store.upsert(
                    "sources",
                    id=source,
                    kind=spec["kind"],
                    path=str(path),
                    last_scan=time.time(),
                    status="error",
                    error=type(exc).__name__,
                )
                store.db.commit()
                failures.append({"source": source, "error": type(exc).__name__})
    with store.db:
        store.expire_details()
        # Link only exact durable identifiers; do not guess using text/time similarity.
        store.db.execute("""INSERT OR IGNORE INTO session_links
            SELECT a.source,a.id,b.source,b.id,'external_session_id'
            FROM sessions a JOIN sessions b ON a.external_id=b.id
            WHERE a.source != b.source""")
        store.db.execute("""INSERT OR IGNORE INTO session_links
            SELECT a.source,a.id,b.source,b.id,'same_native_session_id'
            FROM sessions a JOIN sessions b ON a.id=b.id AND a.harness=b.harness
            WHERE a.source < b.source AND a.harness IN ('codex','zcode')""")
    return failures


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "snapshot", "status"])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    store = Store(args.db)
    try:
        if args.command == "collect":
            failures = collect(store, json.loads(args.config.read_text()))
            print(
                json.dumps(
                    {
                        "failures": failures,
                        "events": store.db.execute("SELECT count(*) FROM events").fetchone()[0],
                    }
                )
            )
            if failures:
                raise SystemExit(1)
        elif args.command == "snapshot":
            store.snapshot(args.destination)
        else:
            print(json.dumps([dict(r) for r in store.db.execute("SELECT * FROM source_health")]))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
