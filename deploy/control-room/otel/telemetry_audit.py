# ruff: noqa: E501
#!/usr/bin/env python3
"""Privacy-safe, read-only audit of the local Mac Codex lean OTLP archive."""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_OTEL_HOME = Path(
    os.environ.get(
        "CONTROL_ROOM_OTEL_HOME",
        Path.home() / "Library/Application Support/ControlRoom/otel",
    )
)
SIGNALS = {
    "logs": ("resourceLogs", "scopeLogs", "logRecords"),
    "traces": ("resourceSpans", "scopeSpans", "spans"),
    "metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
}
POINT_KINDS = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")
SENSITIVE_KEYS = {
    "arguments",
    "output",
    "user.email",
    "user.account_id",
    "error.message",
    "prompt",
    "tool.arguments",
    "tool.output",
    "gen_ai.prompt",
    "gen_ai.completion",
}
FIELD_SPECS = {
    "conversation_id": ("conversation.id",),
    "session_id": ("session.id", "thread.id", "thread_id"),
    "turn_id": ("turn.id", "turn_id"),
    "requested_model": (
        "requested_model",
        "model.requested",
        "gen_ai.request.model",
        "codex.request.model",
        "request.model",
    ),
    "reported_model": ("model",),
    "actual_model": ("actual_model", "gen_ai.response.model", "response.model"),
    "provider": ("provider", "provider_name", "gen_ai.provider.name"),
    "reasoning_effort": (
        "reasoning_effort",
        "model_reasoning_effort",
        "codex.request.reasoning_effort",
        "codex.turn.reasoning_effort",
        "gen_ai.request.reasoning_effort",
    ),
    "token_usage": ("token_count", "token_usage", "usage.input_tokens", "usage.output_tokens"),
    "tool_identity": ("tool_name", "tool.name", "mcp.server.name", "mcp_server"),
    "tool_outcome": ("success", "outcome", "tool.status", "tool.error"),
    "duration": ("duration_ms", "ttft_ms", "initial_duration_ms"),
    "error": (
        "error.type",
        "error.code",
        "error.message",
        "exception.type",
        "codex.mcp.error.code",
    ),
    "status": ("status", "status.code", "http.response.status_code", "startup.status"),
    "sandbox_permission": ("sandbox_policy", "approval_policy", "permission", "decision"),
    "app_version": ("app.version", "app_server.client_version"),
    "service_version": ("service.version",),
    "capture_source": (
        "controlroom.capture_node_id",
        "controlroom.source.role",
        "controlroom.collector.version",
        "deployment.environment.name",
    ),
    "workspace": (
        "cwd",
        "workspace",
        "workspace.id",
        "workspace.path",
        "workspace_root",
        "worktree",
        "worktree.path",
        "project_root",
    ),
    "repository": (
        "repository",
        "repository.name",
        "repository.url",
        "repo",
        "repo.root",
        "git.repository",
        "git.remote",
        "git.remote.url",
        "git.branch",
        "vcs.repository.name",
        "vcs.repository.url",
        "vcs.ref.head.revision",
    ),
    "project": (
        "project",
        "project.id",
        "project.name",
        "codex.project",
        "codex.project.id",
        "chatgpt.project",
        "chatgpt.project.id",
    ),
}
SAFE_CATEGORICAL_FIELDS = (
    "reported_model",
    "actual_model",
    "requested_model",
    "provider",
    "reasoning_effort",
    "sandbox_permission",
    "app_version",
    "service_version",
)


def _value(value):
    """Decode an OTLP AnyValue without ever rendering it by default."""
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            a.get("key"): _value(a.get("value")) for a in value["kvlistValue"].get("values", [])
        }
    return None


def _attributes(items):
    result = {}
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            result[item["key"]] = _value(item.get("value"))
    return result


def _pct(numerator, denominator):
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _matches(key, candidates):
    if key in candidates:
        return True
    if "token_count" in candidates or "token_usage" in candidates:
        return "token" in key.lower() and ("count" in key.lower() or "usage" in key.lower())
    return False


def _keys_for_field(attributes, field):
    return [
        key
        for key, value in attributes.items()
        if _matches(key, FIELD_SPECS[field]) and value not in (None, "", [], {})
    ]


def _safe_root(path):
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
        return "~/" + relative.as_posix()
    except (OSError, ValueError):
        return path.name or "."


def _lean_root(path):
    """Accept the documented OTel home, its data directory, or data/lean itself."""
    path = path.expanduser()
    if (path / "data/lean").is_dir():
        return path / "data/lean"
    if (path / "lean").is_dir():
        return path / "lean"
    return path


def _context_id(value):
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def _status_code(span):
    status = span.get("status")
    return status.get("code") if isinstance(status, dict) else None


def _timestamp_ns(signal, item):
    keys = (
        ("startTimeUnixNano", "endTimeUnixNano")
        if signal == "traces"
        else ("timeUnixNano", "observedTimeUnixNano")
    )
    for key in keys:
        try:
            return int(item[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _parse_start_time(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset or Z")
    return int(parsed.timestamp() * 1_000_000_000)


def _signal_files(root, signal):
    directory = root / signal
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.glob("*.json*") if path.is_file() and not path.is_symlink()
    )


class Audit:
    def __init__(self, root, start_time_ns=None):
        self.root = root
        self.start_time_ns = start_time_ns
        self.time_filter_skipped_before = Counter()
        self.time_filter_skipped_missing = Counter()
        self.signal = {
            name: {
                "files": 0,
                "bytes": 0,
                "export_records": 0,
                "items": 0,
                "malformed_records": 0,
                "unreadable_files": 0,
            }
            for name in SIGNALS
        }
        self.file_details = []
        self.malformed = []
        self.unreadable = []
        self.resource_keys = {name: Counter() for name in SIGNALS}
        self.scope_keys = {name: Counter() for name in SIGNALS}
        self.item_keys = {name: Counter() for name in SIGNALS}
        self.field_counts = {name: Counter() for name in SIGNALS}
        self.field_key_counts = {field: Counter() for field in FIELD_SPECS}
        self.safe_values = {field: Counter() for field in SAFE_CATEGORICAL_FIELDS}
        self.ids = {name: defaultdict(set) for name in SIGNALS}
        self.trace_ids = set()
        self.span_keys = set()
        self.parent_refs = []
        self.duplicate_spans = 0
        self.span_roots = 0
        self.span_status_present = 0
        self.span_error_status = 0
        self.span_duration_count = 0
        self.span_duration_total_ms = 0.0
        self.span_duration_min_ms = None
        self.span_duration_max_ms = None
        self.repo_contexts = defaultdict(set)
        self.attributed_identities = defaultdict(set)
        self.trace_contexts = defaultdict(set)
        self.trace_identities = defaultdict(lambda: defaultdict(set))
        self.attribution_items = 0
        self.sensitive_occurrences = Counter()
        self.sensitive_by_signal = {name: Counter() for name in SIGNALS}
        self.prompt_redacted = 0
        self.prompt_non_redacted = 0
        self.tool_identity_occurrences = 0
        self.tool_items = 0
        self.tool_items_with_outcome = 0
        self.tool_success_items = 0
        self.tool_failure_items = 0
        self.tool_name_values = set()
        self.metric_names = Counter()

    def run(self):
        for signal in SIGNALS:
            for path in _signal_files(self.root, signal):
                self._read_file(signal, path)
        return self._report()

    def _read_file(self, signal, path):
        stats = self.signal[signal]
        stats["files"] += 1
        try:
            size = path.stat().st_size
            stats["bytes"] += size
            file_stat = {
                "signal": signal,
                "file": path.name,
                "bytes": size,
                "records": 0,
                "malformed": 0,
            }
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    try:
                        request = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        stats["malformed_records"] += 1
                        file_stat["malformed"] += 1
                        if len(self.malformed) < 50:
                            self.malformed.append(
                                {"signal": signal, "file": path.name, "line": line_number}
                            )
                        continue
                    stats["export_records"] += 1
                    file_stat["records"] += 1
                    self._request(signal, request)
            self.file_details.append(file_stat)
        except OSError as exc:
            stats["unreadable_files"] += 1
            self.unreadable.append(
                {"signal": signal, "file": path.name, "error_type": type(exc).__name__}
            )

    def _request(self, signal, request):
        top, scopes_key, items_key = SIGNALS[signal]
        for resource_group in request.get(top, []) if isinstance(request, dict) else []:
            resource = _attributes(resource_group.get("resource", {}).get("attributes", []))
            self.resource_keys[signal].update(resource.keys())
            for scope_group in resource_group.get(scopes_key, []):
                scope = scope_group.get("scope", {})
                scope_attributes = {}
                if isinstance(scope, dict):
                    self.scope_keys[signal].update(k for k in scope if k != "attributes")
                    scope_attributes = _attributes(scope.get("attributes", []))
                    self.scope_keys[signal].update(scope_attributes.keys())
                inherited = dict(resource)
                inherited.update(scope_attributes)
                if signal == "metrics":
                    for metric in scope_group.get(items_key, []):
                        self._metric(inherited, metric)
                else:
                    for item in scope_group.get(items_key, []):
                        self._item(signal, inherited, item)

    def _metric(self, resource, metric):
        name = metric.get("name") if isinstance(metric, dict) else None
        if isinstance(name, str):
            self.metric_names[name] += 1
        metric_attributes = (
            _attributes(metric.get("attributes", [])) if isinstance(metric, dict) else {}
        )
        found_points = False
        for kind in POINT_KINDS:
            payload = metric.get(kind) if isinstance(metric, dict) else None
            if not isinstance(payload, dict):
                continue
            for point in payload.get("dataPoints", []):
                found_points = True
                if not self._include("metrics", point):
                    continue
                attrs = dict(resource)
                attrs.update(metric_attributes)
                attrs.update(_attributes(point.get("attributes", [])))
                trace_ids = set()
                for exemplar in point.get("exemplars", []):
                    if exemplar.get("traceId"):
                        trace_ids.add(exemplar["traceId"])
                self._observe("metrics", attrs, point, trace_ids=trace_ids, metric_name=name)
        if not found_points:
            # A metric definition without a point is structural, not an analytical item.
            return

    def _item(self, signal, resource, item):
        if not self._include(signal, item):
            return
        attrs = dict(resource)
        attrs.update(_attributes(item.get("attributes", [])))
        if signal == "traces":
            for event in item.get("events", []):
                attrs.update(_attributes(event.get("attributes", [])))
        trace_ids = {item["traceId"]} if item.get("traceId") else set()
        span_ids = {item["spanId"]} if item.get("spanId") else set()
        self._observe(signal, attrs, item, trace_ids=trace_ids, span_ids=span_ids)
        if signal != "traces":
            return
        trace_id, span_id = item.get("traceId"), item.get("spanId")
        if trace_id and span_id:
            key = (trace_id, span_id)
            if key in self.span_keys:
                self.duplicate_spans += 1
            self.span_keys.add(key)
            self.trace_ids.add(trace_id)
        parent_id = item.get("parentSpanId")
        if parent_id:
            self.parent_refs.append((trace_id, parent_id))
        else:
            self.span_roots += 1
        status = _status_code(item)
        if status is not None:
            self.span_status_present += 1
            if status in (2, "STATUS_CODE_ERROR", "ERROR"):
                self.span_error_status += 1
        try:
            duration = (int(item["endTimeUnixNano"]) - int(item["startTimeUnixNano"])) / 1e6
            if duration >= 0:
                self.span_duration_count += 1
                self.span_duration_total_ms += duration
                self.span_duration_min_ms = (
                    duration
                    if self.span_duration_min_ms is None
                    else min(self.span_duration_min_ms, duration)
                )
                self.span_duration_max_ms = (
                    duration
                    if self.span_duration_max_ms is None
                    else max(self.span_duration_max_ms, duration)
                )
        except (KeyError, TypeError, ValueError):
            pass

    def _include(self, signal, item):
        if self.start_time_ns is None:
            return True
        timestamp = _timestamp_ns(signal, item)
        if timestamp is None:
            self.time_filter_skipped_missing[signal] += 1
            return False
        if timestamp < self.start_time_ns:
            self.time_filter_skipped_before[signal] += 1
            return False
        return True

    def _observe(self, signal, attrs, item, trace_ids=None, span_ids=None, metric_name=None):
        stats = self.signal[signal]
        stats["items"] += 1
        self.item_keys[signal].update(attrs.keys())
        trace_ids = trace_ids or set()
        span_ids = span_ids or set()
        self.ids[signal]["trace"].update(trace_ids)
        self.ids[signal]["span"].update(span_ids)
        has_attribution = any(
            _keys_for_field(attrs, field) for field in ("workspace", "repository", "project")
        )
        if has_attribution:
            self.attribution_items += 1
            for field in ("workspace", "repository", "project"):
                for key in _keys_for_field(attrs, field):
                    context = _context_id(attrs[key])
                    for trace_id in trace_ids:
                        self.trace_contexts[trace_id].add(context)
        tool_keys = _keys_for_field(attrs, "tool_identity")
        outcome_keys = _keys_for_field(attrs, "tool_outcome")
        if tool_keys:
            self.tool_items += 1
            if outcome_keys:
                self.tool_items_with_outcome += 1
            success_values = [attrs[key] for key in outcome_keys if key == "success"]
            if any(value is False or str(value).lower() == "false" for value in success_values):
                self.tool_failure_items += 1
            elif any(value is True or str(value).lower() == "true" for value in success_values):
                self.tool_success_items += 1
        for field in FIELD_SPECS:
            keys = _keys_for_field(attrs, field)
            intrinsic = False
            if field == "duration" and signal == "traces":
                intrinsic = bool(item.get("startTimeUnixNano") and item.get("endTimeUnixNano"))
            if field == "duration" and metric_name:
                intrinsic = "duration" in metric_name.lower()
            if field == "token_usage" and metric_name:
                intrinsic = "token" in metric_name.lower() and "usage" in metric_name.lower()
            if field == "status" and signal == "traces":
                intrinsic = _status_code(item) is not None
            if keys or intrinsic:
                self.field_counts[signal][field] += 1
            for key in keys:
                self.field_key_counts[field][key] += 1
                value = attrs.get(key)
                if field in SAFE_CATEGORICAL_FIELDS and isinstance(value, (str, bool, int, float)):
                    self.safe_values[field][str(value)] += 1
                if field in ("conversation_id", "session_id", "turn_id") and value not in (
                    None,
                    "",
                ):
                    self.ids[signal][field].add(str(value))
                    for trace_id in trace_ids:
                        self.trace_identities[trace_id][field].add(str(value))
                    if has_attribution:
                        self.attributed_identities[field].add(str(value))
                if field in ("workspace", "repository", "project") and value not in (None, ""):
                    self.repo_contexts[field].add(_context_id(value))
                if field == "tool_identity" and value not in (None, ""):
                    self.tool_identity_occurrences += 1
                    self.tool_name_values.add(_context_id(value))
        for key, value in attrs.items():
            if key in SENSITIVE_KEYS:
                self.sensitive_occurrences[key] += 1
                self.sensitive_by_signal[signal][key] += 1
            if key == "prompt":
                if value == "[REDACTED]":
                    self.prompt_redacted += 1
                else:
                    self.prompt_non_redacted += 1

    def _report(self):
        fields = {}
        searched_not_observed = []
        warnings = []
        for field in FIELD_SPECS:
            by_signal = {}
            total_present = total_items = 0
            for signal, stats in self.signal.items():
                present = self.field_counts[signal][field]
                items = stats["items"]
                total_present += present
                total_items += items
                by_signal[signal] = {
                    "present": present,
                    "items": items,
                    "completeness_percent": _pct(present, items),
                }
            status = (
                "not_observed"
                if total_present == 0
                else ("consistent" if total_present == total_items else "intermittent")
            )
            if status == "not_observed":
                searched_not_observed.append(field)
            elif status == "intermittent":
                active = [s for s, x in by_signal.items() if x["present"]]
                warnings.append(
                    f"{field} appears only on a subset of items/signals ({', '.join(active)})."
                )
            fields[field] = {
                "status": status,
                "searched_keys": list(FIELD_SPECS[field]),
                "observed_key_counts": dict(sorted(self.field_key_counts[field].items())),
                "by_signal": by_signal,
            }
        safe_values = {}
        for field, values in self.safe_values.items():
            safe_values[field] = {
                "distinct_values": len(values),
                "counts": dict(values.most_common()),
            }
        correlations = {}
        for kind in ("trace", "conversation_id", "session_id", "turn_id"):
            sets = {signal: self.ids[signal][kind] for signal in SIGNALS}
            union = set().union(*sets.values())
            multi = {
                value for value in union if sum(value in values for values in sets.values()) >= 2
            }
            correlations[kind] = {
                "unique_by_signal": {signal: len(values) for signal, values in sets.items()},
                "unique_total": len(union),
                "identifiers_in_multiple_signals": len(multi),
                "cross_signal_coverage_percent": _pct(len(multi), len(union)),
            }
        log_trace_ids = self.ids["logs"]["trace"]
        metric_trace_ids = self.ids["metrics"]["trace"]
        correlations["logs_to_spans"] = {
            "trace_bearing_logs_unique_traces": len(log_trace_ids),
            "matched_trace_ids": len(log_trace_ids & self.trace_ids),
            "coverage_percent": _pct(len(log_trace_ids & self.trace_ids), len(log_trace_ids)),
        }
        correlations["metrics_to_spans"] = {
            "trace_bearing_metric_unique_traces": len(metric_trace_ids),
            "matched_trace_ids": len(metric_trace_ids & self.trace_ids),
            "coverage_percent": _pct(
                len(metric_trace_ids & self.trace_ids), len(metric_trace_ids)
            ),
        }
        resolved = sum((trace, parent) in self.span_keys for trace, parent in self.parent_refs)
        parents = len(self.parent_refs)
        total_items = sum(x["items"] for x in self.signal.values())
        direct_repo = sum(self.field_counts[s]["repository"] for s in SIGNALS)
        direct_project = sum(self.field_counts[s]["project"] for s in SIGNALS)
        direct_workspace = sum(self.field_counts[s]["workspace"] for s in SIGNALS)
        trace_linked = {}
        for field, label in (
            ("conversation_id", "conversations"),
            ("session_id", "sessions"),
            ("turn_id", "turns"),
        ):
            identity_contexts = defaultdict(set)
            for trace_id, identities in self.trace_identities.items():
                contexts = self.trace_contexts.get(trace_id, set())
                for identity in identities[field]:
                    identity_contexts[identity].update(contexts)
            trace_linked[label] = {
                "with_context": sum(bool(values) for values in identity_contexts.values()),
                "with_one_context": sum(len(values) == 1 for values in identity_contexts.values()),
                "with_multiple_contexts": sum(
                    len(values) > 1 for values in identity_contexts.values()
                ),
            }
        return {
            "audit_version": 1,
            "mode": "read_only_lean_archive",
            "archive_root": _safe_root(self.root),
            "time_filter": {
                "start_time": (
                    dt.datetime.fromtimestamp(
                        self.start_time_ns / 1_000_000_000, dt.timezone.utc
                    ).isoformat()
                    if self.start_time_ns is not None
                    else None
                ),
                "skipped_before_start": {
                    signal: self.time_filter_skipped_before[signal] for signal in SIGNALS
                },
                "skipped_missing_timestamp": {
                    signal: self.time_filter_skipped_missing[signal] for signal in SIGNALS
                },
            },
            "signals": self.signal,
            "files": {
                "inspected": self.file_details,
                "malformed_records": self.malformed,
                "unreadable_files": self.unreadable,
            },
            "schema": {
                signal: {
                    "resource_attribute_keys": sorted(self.resource_keys[signal]),
                    "scope_fields": sorted(self.scope_keys[signal]),
                    "item_attribute_keys": sorted(self.item_keys[signal]),
                    **({"metric_names": sorted(self.metric_names)} if signal == "metrics" else {}),
                }
                for signal in SIGNALS
            },
            "identity": {
                "unique_conversations": len(
                    set().union(*(self.ids[s]["conversation_id"] for s in SIGNALS))
                ),
                "unique_sessions": len(set().union(*(self.ids[s]["session_id"] for s in SIGNALS))),
                "unique_turns": len(set().union(*(self.ids[s]["turn_id"] for s in SIGNALS))),
                "unique_traces": len(self.trace_ids),
                "unique_spans": len(self.span_keys),
                "duplicate_span_records": self.duplicate_spans,
            },
            "fields": fields,
            "fields_searched_but_not_observed": searched_not_observed,
            "safe_categorical_values": safe_values,
            "correlation": correlations,
            "parent_integrity": {
                "root_spans": self.span_roots,
                "spans_with_parent_id": parents,
                "parents_present_in_lean_archive": resolved,
                "orphan_parent_references": parents - resolved,
                "parent_resolution_percent": _pct(resolved, parents),
            },
            "runtime": {
                "spans_with_calculable_duration": self.span_duration_count,
                "coverage_percent": _pct(self.span_duration_count, self.signal["traces"]["items"]),
                "mean_ms": round(self.span_duration_total_ms / self.span_duration_count, 3)
                if self.span_duration_count
                else None,
                "min_ms": round(self.span_duration_min_ms, 3)
                if self.span_duration_min_ms is not None
                else None,
                "max_ms": round(self.span_duration_max_ms, 3)
                if self.span_duration_max_ms is not None
                else None,
            },
            "status_and_errors": {
                "spans_with_status": self.span_status_present,
                "error_status_spans": self.span_error_status,
            },
            "tools": {
                "tool_identity_occurrences": self.tool_identity_occurrences,
                "items_with_tool_identity": self.tool_items,
                "tool_items_with_outcome": self.tool_items_with_outcome,
                "tool_outcome_coverage_percent": _pct(
                    self.tool_items_with_outcome, self.tool_items
                ),
                "tool_items_explicit_success": self.tool_success_items,
                "tool_items_explicit_failure": self.tool_failure_items,
                "distinct_tool_identity_hashes": len(self.tool_name_values),
                "tool_values_emitted": False,
            },
            "repository_project_attribution": {
                "items_with_any_attribution": self.attribution_items,
                "all_items": total_items,
                "coverage_percent": _pct(self.attribution_items, total_items),
                "direct_workspace_items": direct_workspace,
                "direct_repository_items": direct_repo,
                "direct_project_items": direct_project,
                "unique_hashed_contexts": {
                    key: len(value) for key, value in self.repo_contexts.items()
                },
                "unique_identities_with_attribution": {
                    "conversations": len(self.attributed_identities["conversation_id"]),
                    "sessions": len(self.attributed_identities["session_id"]),
                    "turns": len(self.attributed_identities["turn_id"]),
                },
                "trace_linked_identity_attribution": trace_linked,
                "raw_paths_or_identifiers_emitted": False,
                "evidence": "direct"
                if direct_repo or direct_project
                else ("workspace_only" if direct_workspace else "absent"),
                "interpretation": (
                    "Repository/project identity is directly recorded."
                    if direct_repo or direct_project
                    else "Working-directory evidence can distinguish some contexts by hash, but repository/project identity is inference."
                    if direct_workspace
                    else "No repository, project, workspace, or working-directory attribution was observed."
                ),
            },
            "warnings": warnings,
            "privacy": {
                "raw_prompt_content_emitted": False,
                "tool_arguments_or_output_emitted": False,
                "account_identifiers_emitted": False,
                "arbitrary_attribute_values_emitted": False,
                "sensitive_attribute_occurrences_by_key": dict(
                    sorted(self.sensitive_occurrences.items())
                ),
                "sensitive_attribute_occurrences_by_signal": {
                    signal: dict(sorted(values.items()))
                    for signal, values in self.sensitive_by_signal.items()
                },
                "prompt_redaction_marker_occurrences": self.prompt_redacted,
                "non_redaction_prompt_values_observed": self.prompt_non_redacted,
                "note": "Sensitive values are counted in memory only and are never rendered.",
            },
        }


def render_human(report):
    signals = report["signals"]
    print("Mac Codex lean telemetry audit (read-only)")
    print(f"Archive: {report['archive_root']}")
    if report["time_filter"]["start_time"]:
        print(f"Start time filter: {report['time_filter']['start_time']}")
    for name in SIGNALS:
        item = signals[name]
        print(
            f"{name}: files={item['files']} exports={item['export_records']} items={item['items']} malformed={item['malformed_records']} unreadable={item['unreadable_files']}"
        )
    identity = report["identity"]
    print(
        "Identity: conversations={unique_conversations} sessions={unique_sessions} turns={unique_turns} traces={unique_traces} spans={unique_spans}".format(
            **identity
        )
    )
    parent = report["parent_integrity"]
    print(
        f"Parent integrity: resolved={parent['parents_present_in_lean_archive']}/{parent['spans_with_parent_id']} ({parent['parent_resolution_percent']}%), orphans={parent['orphan_parent_references']}"
    )
    for kind in ("trace", "conversation_id", "session_id", "turn_id"):
        item = report["correlation"][kind]
        print(
            f"{kind} cross-signal correlation: {item['identifiers_in_multiple_signals']}/{item['unique_total']} identifiers ({item['cross_signal_coverage_percent']}%)"
        )
    attribution = report["repository_project_attribution"]
    print(
        f"Repository/project attribution: {attribution['evidence']}; any-attribution coverage={attribution['coverage_percent']}%; workspace={attribution['direct_workspace_items']} repository={attribution['direct_repository_items']} project={attribution['direct_project_items']}"
    )
    print("Field availability:")
    for field, detail in report["fields"].items():
        present = sum(v["present"] for v in detail["by_signal"].values())
        print(f"  {field}: {detail['status']} ({present} item occurrences)")
    privacy = report["privacy"]
    print(
        f"Privacy: no raw payload values emitted; prompt redaction markers={privacy['prompt_redaction_marker_occurrences']}; non-redaction prompt values observed={privacy['non_redaction_prompt_values_observed']}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_OTEL_HOME,
        help="OTel home, data directory, or lean archive root (default: local Mac Control Room OTel home)",
    )
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    parser.add_argument(
        "--start-time",
        type=_parse_start_time,
        help="include only items at or after this ISO-8601 timestamp (must include offset or Z)",
    )
    args = parser.parse_args(argv)
    root = _lean_root(args.root)
    report = Audit(root, start_time_ns=args.start_time).run()
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        render_human(report)
    return 1 if any(x["unreadable_files"] for x in report["signals"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
