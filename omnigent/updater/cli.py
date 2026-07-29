"""Operator CLI for the external updater (issue #38 §10).

A small click-style CLI exposing the updater as ``omnigent-updater``
(the binary lives at ``/opt/omnigent/updater/bin/omnigent-updater``
in production and is wired into ``omnigent-updater.service``).

Subcommands:

* ``record`` — file a new update request and return the id.
* ``status <id>`` — print the current state of a request, plus
  optional web-service status.
* ``run <id>`` — process a recorded request end-to-end.
* ``list`` — enumerate all known request ids.
* ``recover`` — run the crash-recovery scan.
* ``sweep-locks`` — release stale lock files.

The CLI never accepts arbitrary shell commands. Every command line
flag is parsed by Python and forwarded to the controller's narrow
APIs — the same APIs the agent's ``sys_update_request`` tool uses.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn

from omnigent.updater.controller import (
    ControllerConfig,
    UpdaterController,
)
from omnigent.updater.request_tool import (
    RequestInterfaceError,
    record_request,
)
from omnigent.updater.store import UpdaterStore

USAGE = """\
omnigent-updater — operator CLI for the external self-update controller.

Usage:
  omnigent-updater record --target-sha <sha> --expected-current-sha <sha>
                          [--origin-session-id SID] [--origin-conversation-id CID]
                          [--operator NAME] [--notes "..."]
  omnigent-updater status <request_id>
  omniquet-updater run <request_id> [--dry-run]
  omnigent-updater list
  omnigent-updater recover
  omnigent-updater sweep-locks [--stale-seconds N]
  omnigent-updater show-result <request_id>
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnigent-updater",
        description=USAGE.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(USAGE.splitlines()[2:]),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="file a new update request")
    rec.add_argument("--target-sha", required=True)
    rec.add_argument("--expected-current-sha", required=True)
    rec.add_argument("--origin-session-id", default=None)
    rec.add_argument("--origin-conversation-id", default=None)
    rec.add_argument("--operator", default=None)
    rec.add_argument("--notes", default=None)
    rec.add_argument(
        "--requested-by",
        default=None,
        help=(
            'Override the requested_by field. Defaults to "operator:<name>" '
            "when --operator is set."
        ),
    )

    p_status = sub.add_parser("status", help="show the current state of a request")
    p_status.add_argument("request_id")

    p_run = sub.add_parser("run", help="process a recorded request end-to-end")
    p_run.add_argument("request_id")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--db-url", default=None)
    p_run.add_argument("--service-port", type=int, default=None)

    sub.add_parser("list", help="list every known request id")
    sub.add_parser("recover", help="run the crash-recovery scan")
    p_sweep = sub.add_parser("sweep-locks", help="release stale locks")
    p_sweep.add_argument("--stale-seconds", type=int, default=3600)
    p_show = sub.add_parser("show-result", help="print the terminal result for a request")
    p_show.add_argument("request_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except RequestInterfaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "recover":
        return _cmd_recover(args)
    if args.command == "sweep-locks":
        return _cmd_sweep(args)
    if args.command == "show-result":
        return _cmd_show_result(args)
    return _usage_error(f"unknown command {args.command!r}")


def _cmd_record(args: argparse.Namespace) -> int:
    operator = args.operator
    requested_by = args.requested_by or (f"operator:{operator}" if operator else "operator:cli")
    payload: dict[str, Any] = {
        "target_sha": args.target_sha,
        "expected_current_sha": args.expected_current_sha,
        "requested_by": requested_by,
        "authorization": {"kind": "operator"},
    }
    if operator:
        payload["authorization"]["operator"] = operator
    if args.origin_session_id:
        payload["origin_session_id"] = args.origin_session_id
    if args.origin_conversation_id:
        payload["origin_conversation_id"] = args.origin_conversation_id
    if args.notes:
        payload["notes"] = args.notes

    outcome = record_request(payload)
    print(
        json.dumps(
            {
                "request_id": outcome.request_id,
                "created_at": outcome.created_at,
                "request_path": outcome.request_path,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = UpdaterStore()
    req = store.load_request(args.request_id)
    if req is None:
        print(f"no such request: {args.request_id}", file=sys.stderr)
        return 1
    payload: dict[str, Any] = {
        "request": req.to_dict(),
        "checkpoint": None,
        "result": None,
    }
    checkpoint = store.load_checkpoint(args.request_id)
    if checkpoint is not None:
        payload["checkpoint"] = {
            "phase": checkpoint.phase.value,
            "updated_at": checkpoint.updated_at,
            "context": checkpoint.context,
        }
    result = store.load_result(args.request_id)
    if result is not None:
        payload["result"] = result.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    store = UpdaterStore()
    req = store.load_request(args.request_id)
    if req is None:
        print(f"no such request: {args.request_id}", file=sys.stderr)
        return 1
    cfg = ControllerConfig(dry_run=args.dry_run)
    if args.db_url:
        cfg.db_url = args.db_url
    if args.service_port:
        cfg.service_port = args.service_port
    controller = UpdaterController(cfg, store=store)
    result = controller.run(req)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.final_status in {"succeeded", "rejected"} else 1


def _cmd_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    store = UpdaterStore()
    ids = store.list_request_ids()
    for rid in ids:
        result = store.load_result(rid)
        if result is None:
            status = "in-flight"
        else:
            status = result.final_status
        print(f"{rid}\t{status}")
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:  # noqa: ARG001
    store = UpdaterStore()
    cfg = ControllerConfig()
    controller = UpdaterController(cfg, store=store)
    decisions = controller.recover_non_terminal()
    print(json.dumps([d.__dict__ for d in decisions], indent=2, sort_keys=True))
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    from omnigent.updater.locking import UpdateLock

    swept = UpdateLock.sweep(stale_seconds=args.stale_seconds)
    for path in swept:
        print(str(path))
    return 0


def _cmd_show_result(args: argparse.Namespace) -> int:
    store = UpdaterStore()
    result = store.load_result(args.request_id)
    if result is None:
        print(f"no result for {args.request_id}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def _usage_error(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
