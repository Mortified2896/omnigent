from scripts.routing_prediction_dataset import build_record, canonical_hash, packet, request_start


def trace():
    return [
        {"kind": "user", "text": "Earlier task", "timestamp": "2026-10-01T00:00:00Z"},
        {
            "kind": "assistant",
            "text": "Earlier completed answer",
            "model": "earlier",
            "effort": "low",
        },
        {
            "kind": "user",
            "text": (
                "Implement the requested feature with a saved artifact "
                "and verify its acceptance conditions."
            )
            * 2,
            "timestamp": "2026-10-02T00:00:00Z",
        },
        {
            "kind": "function_call_output",
            "text": "SECRET_EXECUTION_RESULT",
            "model": "chosen",
            "effort": "max",
        },
        {"kind": "assistant", "text": "SECRET_FINAL_OUTCOME", "model": "chosen", "effort": "max"},
    ]


def proposal(**changes):
    return {
        "checkpoint": 4,
        "checkpoint_digest": "bound",
        "scope": {"start_index": 2},
        "coverage": {},
        "evidence": [{"index": 2}, {"index": 4}],
        "outcome": "partial",
        "confidence": "high",
        **changes,
    }


def test_preexecution_view_hides_executor_and_future_evidence():
    value = packet(trace(), 2, "case")
    assert value["context_available_before_request"][1]["text"] == "Earlier completed answer"
    assert "SECRET" not in str(value)
    assert "chosen" not in str(value)
    assert "effort" not in str(value)
    assert canonical_hash(value) == canonical_hash(dict(reversed(list(value.items()))))


def test_target_changes_do_not_change_blind_packet_or_task_group():
    entry = {"source_id": "codex:case", "shard": "project"}
    a, reason = build_record(entry, proposal(), trace())
    b, _ = build_record(entry, proposal(outcome="success", confidence="low"), trace())
    assert reason == "included"
    assert a["input_packet"] == b["input_packet"]
    assert a["task_group"] == b["task_group"]
    assert a["primary_scope"] is True


def test_multiple_owner_requests_and_model_changes_are_excluded():
    values = trace()
    values[3] = {"kind": "user", "text": "Actually use a different acceptance target"}
    assert (
        build_record({"source_id": "codex:c", "shard": "p"}, proposal(), values)[1]
        == "later_owner_steering_or_multiple_requests"
    )
    values = trace()
    values.insert(4, {"kind": "assistant", "model": "other", "effort": "low"})
    assert (
        build_record({"source_id": "codex:c", "shard": "p"}, proposal(checkpoint=5), values)[1]
        == "mixed_or_missing_executor_configuration"
    )


def test_nearest_evidence_request_is_sensitivity_only():
    values = trace()
    p = proposal(scope="latest answer", evidence=[{"index": 2}, {"index": 4}])
    assert request_start(p, values) == (2, "inferred_evidence_request_sensitivity_only")
    row, _ = build_record({"source_id": "codex:c", "shard": "p"}, p, values)
    assert row["primary_scope"] is False


def test_delegation_and_non_request_scope_cannot_be_attributed_to_one_model():
    values = trace()
    values[3]["kind"] = "collaboration.spawn_agent"
    assert (
        build_record({"source_id": "codex:c", "shard": "p"}, proposal(), values)[1]
        == "observed_delegated_execution"
    )
    assert (
        build_record(
            {"source_id": "codex:c", "shard": "p"}, proposal(scope={"start_index": 3}), trace()
        )[1]
        == "declared_scope_does_not_start_at_owner_request"
    )
