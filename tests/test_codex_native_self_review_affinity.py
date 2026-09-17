from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native import self_review as sr


def test_cache_provenance_distinguishes_hit_miss_and_unknown() -> None:
    hit = sr._cache_provenance(  # noqa: SLF001 - focused measurement regression.
        sr.ReviewUsage(input_tokens=1000, cache_read_tokens=750)
    )
    assert hit["reuse_observed"] is True
    assert hit["cache_read_ratio"] == 0.75
    assert hit["prompt_cache_lineage_verified"] is None

    miss = sr._cache_provenance(  # noqa: SLF001
        sr.ReviewUsage(input_tokens=1000, cache_read_tokens=0)
    )
    assert miss["reuse_observed"] is False
    assert miss["cache_read_ratio"] == 0.0

    unknown = sr._cache_provenance(sr.ReviewUsage(input_tokens=1000))  # noqa: SLF001
    assert unknown["reuse_observed"] is None
    assert unknown["cache_read_ratio"] is None


def test_self_review_claim_is_durable_and_idempotent(tmp_path: Path) -> None:
    assert sr._claim_self_review_once(  # noqa: SLF001
        tmp_path,
        session_id="session-1",
        primary_turn_id="turn-1",
    )
    assert not sr._claim_self_review_once(  # noqa: SLF001
        tmp_path,
        session_id="session-1",
        primary_turn_id="turn-1",
    )
    assert sr._claim_self_review_once(  # noqa: SLF001
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
        sr._select_affinity_target(  # noqa: SLF001
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
        sr._select_affinity_target(  # noqa: SLF001
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
        sr._select_affinity_target(  # noqa: SLF001
            combo,
            provider="google",
            model="gpt-5.6-sol",
            connection_id=None,
        )
        is None
    )
