from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native import self_review as sr


def test_cache_provenance_distinguishes_hit_miss_and_unknown() -> None:
    hit = sr._cache_provenance(sr.ReviewUsage(input_tokens=1000, cache_read_tokens=750))
    assert hit["reuse_observed"] is True
    assert hit["cache_read_ratio"] == 0.75
    assert hit["prompt_cache_lineage_verified"] is None

    miss = sr._cache_provenance(sr.ReviewUsage(input_tokens=1000, cache_read_tokens=0))
    assert miss["reuse_observed"] is False
    assert miss["cache_read_ratio"] == 0.0

    unknown = sr._cache_provenance(sr.ReviewUsage(input_tokens=1000))
    assert unknown["reuse_observed"] is None
    assert unknown["cache_read_ratio"] is None


def test_self_review_claim_is_durable_and_idempotent(tmp_path: Path) -> None:
    assert sr._claim_self_review_once(
        tmp_path,
        session_id="session-1",
        primary_turn_id="turn-1",
    )
    assert not sr._claim_self_review_once(
        tmp_path,
        session_id="session-1",
        primary_turn_id="turn-1",
    )
    assert sr._claim_self_review_once(
        tmp_path,
        session_id="session-1",
        primary_turn_id="turn-2",
    )


def test_affinity_target_prefers_exact_connection() -> None:
    combo = {
        "models": [
            {
                "providerId": "google",
                "model": "google/gemini-3.8-flash",
                "connectionId": "account-a",
            },
            {
                "providerId": "google",
                "model": "google/gemini-3.8-flash",
                "connectionId": "account-b",
            },
        ]
    }
    assert (
        sr._select_affinity_target(
            combo,
            provider="google",
            model="gemini-3.8-flash",
            connection_id="account-b",
        )
        == "google/gemini-3.8-flash"
    )


def test_affinity_target_fails_closed_when_account_is_ambiguous() -> None:
    combo = {
        "models": [
            {"providerId": "google", "model": "google/gemini-3.8-flash"},
            {"providerId": "google", "model": "google/gemini-3.8-flash"},
        ]
    }
    assert (
        sr._select_affinity_target(
            combo,
            provider="google",
            model="gemini-3.8-flash",
            connection_id=None,
        )
        is None
    )


def test_affinity_target_does_not_cross_provider_or_model() -> None:
    combo = {
        "models": [
            {"providerId": "openai", "model": "openai/gpt-5.6-sol"},
            {"providerId": "google", "model": "google/gemini-3.8-flash"},
        ]
    }
    assert (
        sr._select_affinity_target(
            combo,
            provider="google",
            model="gpt-5.6-sol",
            connection_id=None,
        )
        is None
    )


async def test_missing_turn_binding_never_selects_previous_call():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    logs = AsyncMock()
    client = SimpleNamespace(list_call_logs=logs)
    now = datetime.now(timezone.utc)
    assert (
        await sr._correlated_route_call(
            client, "custom/o3-route-test", not_before=now, not_after=now, call_log_id=None
        )
        is None
    )
    logs.assert_not_called()


async def test_affinity_uses_only_bound_call_and_preserves_unknown_connection():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    now = datetime.now(timezone.utc)
    for provider, model, connection, expected_model, expected_connection in [
        ("openai", "gpt-test", "account-b", True, False),
        ("other", "gpt-test", "account-a", False, True),
        ("openai", "different", "account-a", False, True),
        ("openai", "gpt-test", None, True, None),
    ]:
        row = {
            "id": "bound",
            "path": "/v1/responses",
            "method": "POST",
            "status": 200,
            "requestedModel": "openai/gpt-test",
            "provider": provider,
            "model": model,
            "connectionId": connection,
            "timestamp": now.isoformat(),
        }
        previous = {**row, "id": "previous", "connectionId": "account-a"}
        client = SimpleNamespace(
            list_call_logs=AsyncMock(return_value=[previous, row]),
            _parse_timestamp=datetime.fromisoformat,
        )
        result = await sr._verify_backend_affinity(
            client,
            sr.ReviewBackendAffinity(
                primary_provider="openai",
                primary_model="gpt-test",
                primary_connection_id="account-a",
            ),
            route_used="openai/gpt-test",
            not_before=now - timedelta(seconds=1),
            not_after=now + timedelta(seconds=1),
            review_call_log_id="bound",
        )
        assert result.review_call_log_id == "bound"
        assert result.model_provider_match is expected_model
        assert result.connection_match is expected_connection
        assert result.exact_backend_affinity_guaranteed is False
