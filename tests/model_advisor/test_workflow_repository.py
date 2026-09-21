"""Offline workflow + real SQLite/SQLAlchemy persistence tests (no provider calls)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from sqlalchemy import create_engine, inspect

from omnigent.model_advisor_core import AdvisorContractError, Candidate, PoolSnapshot
from omnigent.model_advisor_repository import AdvisorConflict, AdvisorRepository, metadata
from omnigent.model_advisor_workflow import (
    AdvisorPreferences, FrozenRound, ReviewDecision, confirm_review,
    freeze_round, prepare_review, stable_candidate_id,
)


def never_draw(_: int) -> int:
    raise AssertionError("Duplicate/agreement/deterministic decision must not draw")


class _Fixture:
    def setUp(self) -> None:
        self.human = Candidate("small-low", "codex-direct", "openai", "fixture-chatgpt", "codex-native", "fixture-small", "low", "chatgpt_plan")
        self.glm = Candidate("glm-high", "glm-direct", "z.ai", "fixture-glm", "codex-native", "fixture-glm", "high", "glm_plan")
        self.judge = replace(self.human, candidate_id="advisor-high", model_id="fixture-advisor", reasoning_effort="high")
        self.catalog = PoolSnapshot("catalog-1", (self.human, self.glm, self.judge))
        self.prefs = AdvisorPreferences(True, ("small-low", "glm-high"), "advisor-high")
        self.round = freeze_round(owner_id="owner", host_id="host", round_id="round-1", settings_revision="settings-1", task="Explain the task.", preferences=self.prefs, catalog=self.catalog, human_candidate_id="small-low")
        self.advice = json.dumps({"candidate_id": "glm-high", "rationale": "The task benefits from more reasoning."})



class WorkflowTests(_Fixture, unittest.TestCase):
    def test_default_is_disabled_and_fifty_fifty(self) -> None:
        self.assertFalse(AdvisorPreferences().enabled)
        self.assertEqual(AdvisorPreferences().human_probability_percent, 50)

    def test_settings_payload_roundtrip(self) -> None:
        self.assertEqual(AdvisorPreferences.from_payload(self.prefs.to_payload()), self.prefs)

    def test_extra_settings_fields_are_rejected(self) -> None:
        with self.assertRaises(AdvisorContractError):
            AdvisorPreferences.from_payload({**self.prefs.to_payload(), "api_key": "not-a-real-key"})

    def test_strict_settings_types(self) -> None:
        cases = ({"schema_version": True}, {"enabled": 1}, {"human_probability_percent": True}, {"allowed_candidate_ids": "small-low"})
        for patch in cases:
            with self.subTest(patch=patch), self.assertRaises(AdvisorContractError):
                AdvisorPreferences.from_payload({**self.prefs.to_payload(), **patch})

    def test_enabled_requires_pool_and_advisor(self) -> None:
        for patch in ({"allowed_candidate_ids": ()}, {"advisor_candidate_id": None}):
            with self.subTest(patch=patch), self.assertRaises(AdvisorContractError):
                replace(self.prefs, **patch)

    def test_probability_bounds(self) -> None:
        for number in (-1, 101, 0.5, True):
            with self.subTest(number=number), self.assertRaises(AdvisorContractError):
                replace(self.prefs, human_probability_percent=number)

    def test_pool_duplicates_rejected(self) -> None:
        with self.assertRaises(AdvisorContractError):
            replace(self.prefs, allowed_candidate_ids=("small-low", "small-low"))

    def test_advisor_can_be_outside_answer_pool(self) -> None:
        self.assertEqual(self.round.advisor, self.judge)
        self.assertNotIn(self.judge, self.round.pool.candidates)

    def test_independent_input_does_not_include_human_choice(self) -> None:
        other = replace(self.round, human_candidate_id="glm-high")
        self.assertEqual(self.round.advisor_input(), other.advisor_input())
        self.assertNotIn("fixture-chatgpt", json.dumps(self.round.advisor_input()))
        self.assertNotIn("human_candidate_id", self.round.advisor_input())

    def test_missing_saved_choice_does_not_silently_fallback(self) -> None:
        with self.assertRaises(AdvisorContractError):
            freeze_round(owner_id="owner", host_id="host", round_id="r", settings_revision="s", task="Task", preferences=self.prefs, catalog=PoolSnapshot("v2", (self.human, self.judge)), human_candidate_id="small-low")

    def test_unallowed_human_choice_rejected(self) -> None:
        with self.assertRaises(AdvisorContractError):
            replace(self.round, human_candidate_id="advisor-high")

    def test_disabled_preferences_cannot_start_round(self) -> None:
        with self.assertRaises(AdvisorContractError):
            freeze_round(owner_id="o", host_id="h", round_id="r", settings_revision="s", task="Task", preferences=replace(self.prefs, enabled=False), catalog=self.catalog, human_candidate_id="small-low")

    def test_frozen_snapshot_roundtrip(self) -> None:
        self.assertEqual(FrozenRound.from_payload(self.round.to_payload()), self.round)

    def test_stable_candidate_id_ignores_display_copy(self) -> None:
        self.assertEqual(stable_candidate_id(self.human), stable_candidate_id(replace(self.human, capability_summary="New wording", candidate_id="other-handle")))

    def test_candidate_id_binds_lane_and_effort(self) -> None:
        for other in (replace(self.human, lane_id="other"), replace(self.human, reasoning_effort="high"), replace(self.human, connection_id="another-account")):
            self.assertNotEqual(stable_candidate_id(self.human), stable_candidate_id(other))

    def test_fingerprint_binds_prompt_settings_and_probability(self) -> None:
        for other in (replace(self.round, task="Edited"), replace(self.round, settings_revision="s2"), replace(self.round, human_probability_percent=60)):
            self.assertNotEqual(self.round.fingerprint, other.fingerprint)

    def test_fifty_fifty_uses_existing_core(self) -> None:
        for bit, arm in ((0, "human"), (1, "advisor")):
            bounds = []
            def draw(bound: int) -> int:
                bounds.append(bound)
                return bit
            review = prepare_review(self.round, self.advice, randbelow=draw)
            self.assertEqual(bounds, [2])
            self.assertEqual(review.original_assignment.arm, arm)
            self.assertEqual(review.original_assignment.probability_denominator, 2)
            self.assertEqual(review.comparison_group, "randomized_unblinded")

    def test_agreement_is_not_an_arm_win(self) -> None:
        raw = json.dumps({"candidate_id": "small-low", "rationale": "Suitable"})
        review = prepare_review(self.round, raw, randbelow=never_draw)
        self.assertEqual(review.original_assignment.arm, "same")
        self.assertEqual(review.comparison_group, "agreement")

    def test_weighted_probability_records_real_propensity(self) -> None:
        frozen = replace(self.round, human_probability_percent=30)
        human = prepare_review(frozen, self.advice, randbelow=lambda _: 29)
        advisor = prepare_review(frozen, self.advice, randbelow=lambda _: 30)
        self.assertEqual((human.original_assignment.arm, human.original_assignment.probability_numerator), ("human", 30))
        self.assertEqual((advisor.original_assignment.arm, advisor.original_assignment.probability_numerator), ("advisor", 70))

    def test_extreme_probabilities_do_not_randomize(self) -> None:
        for pct, arm in ((0, "advisor"), (100, "human")):
            review = prepare_review(replace(self.round, human_probability_percent=pct), self.advice, randbelow=never_draw)
            self.assertEqual(review.original_assignment.arm, arm)
            self.assertEqual(review.comparison_group, "deterministic")

    def test_bad_rng_does_not_make_assignment(self) -> None:
        with self.assertRaises(AdvisorContractError):
            prepare_review(replace(self.round, human_probability_percent=30), self.advice, randbelow=lambda _: 100)

    def test_outside_advice_rejected(self) -> None:
        for key in ("invented", "advisor-high"):
            with self.assertRaises(AdvisorContractError):
                prepare_review(self.round, json.dumps({"candidate_id": key, "rationale": "x"}), randbelow=never_draw)

    def test_review_roundtrip(self) -> None:
        review = prepare_review(self.round, self.advice, randbelow=lambda _: 1)
        self.assertEqual(ReviewDecision.from_payload(review.to_payload()), review)

    def test_override_retains_draw_and_original_picks(self) -> None:
        review = prepare_review(self.round, self.advice, randbelow=lambda _: 1)
        override = confirm_review(self.round, review, self.catalog, override_candidate_id="small-low", reason="Prefer lower usage")
        self.assertEqual(override.original_assignment, review.original_assignment)
        self.assertEqual(override.human_candidate_id, "small-low")
        self.assertEqual(override.advisor_candidate_id, "glm-high")
        self.assertEqual(override.execution_candidate, self.human)
        self.assertEqual(override.comparison_group, "manual_override")

    def test_override_requires_reason_and_allowed_candidate(self) -> None:
        review = prepare_review(self.round, self.advice, randbelow=lambda _: 1)
        for key, reason in (("small-low", None), ("advisor-high", "manual")):
            with self.assertRaises(AdvisorContractError):
                confirm_review(self.round, review, self.catalog, override_candidate_id=key, reason=reason)

    def test_confirm_missing_exact_route_never_falls_back(self) -> None:
        review = prepare_review(self.round, self.advice, randbelow=lambda _: 1)
        with self.assertRaises(AdvisorContractError):
            confirm_review(self.round, review, PoolSnapshot("new", (self.human,)))

    def test_other_round_cannot_reuse_review(self) -> None:
        review = prepare_review(self.round, self.advice, randbelow=lambda _: 1)
        with self.assertRaises(AdvisorContractError):
            confirm_review(replace(self.round, task="Edited"), review, self.catalog)


class RepositoryTests(_Fixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.url = "sqlite:///" + str(Path(self.temp.name) / "fixture.db")
        self.engine = create_engine(self.url, connect_args={"timeout": 10})
        self.repo = AdvisorRepository(self.engine)
        metadata.create_all(self.engine)  # TEST ONLY: runtime constructor never creates tables.

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def test_constructor_does_not_create_schema(self) -> None:
        other = create_engine("sqlite://")
        AdvisorRepository(other)
        self.assertEqual(inspect(other).get_table_names(), [])
        other.dispose()

    def test_preferences_survive_connection_restart(self) -> None:
        stored = self.repo.save_preferences("owner", "host", self.prefs, expected_version=0)
        self.engine.dispose()
        restarted = create_engine(self.url)
        loaded = AdvisorRepository(restarted).load_preferences("owner", "host")
        self.assertEqual(loaded, stored)
        self.assertEqual(AdvisorPreferences.from_payload(loaded.payload), self.prefs)
        restarted.dispose()

    def test_preferences_scope_is_owner_host_profile(self) -> None:
        self.repo.save_preferences("owner", "host", self.prefs, expected_version=0)
        for owner, host, profile in (("other", "host", "default"), ("owner", "other", "default"), ("owner", "host", "other")):
            self.assertIsNone(self.repo.load_preferences(owner, host, profile))

    def test_stale_save_rejected(self) -> None:
        first = self.repo.save_preferences("owner", "host", self.prefs, expected_version=0)
        second = self.repo.save_preferences("owner", "host", replace(self.prefs, human_probability_percent=30), expected_version=first.version)
        with self.assertRaises(AdvisorConflict):
            self.repo.save_preferences("owner", "host", self.prefs, expected_version=first.version)
        self.assertEqual(self.repo.load_preferences("owner", "host"), second)
        self.assertNotEqual(first.etag, second.etag)

    def test_duplicate_reservation_gets_no_provider_claim(self) -> None:
        first = self.repo.reserve_round(self.round)
        replay = self.repo.reserve_round(self.round)
        self.assertTrue(first.acquired)
        self.assertFalse(replay.acquired)
        self.assertEqual(first.record, replay.record)

    def test_round_id_reuse_with_changed_prompt_rejected(self) -> None:
        self.repo.reserve_round(self.round)
        with self.assertRaises(AdvisorConflict):
            self.repo.reserve_round(replace(self.round, task="Edited"))

    def test_new_defaults_do_not_change_existing_round(self) -> None:
        self.repo.save_preferences("owner", "host", self.prefs, expected_version=0)
        reserved = self.repo.reserve_round(self.round)
        self.repo.save_preferences("owner", "host", replace(self.prefs, human_probability_percent=0), expected_version=1)
        self.assertEqual(self.repo.load_round("owner", "host", "round-1"), reserved.record)

    def test_assignment_survives_restart_without_draw(self) -> None:
        self.repo.reserve_round(self.round)
        first = self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=lambda _: 1)
        self.engine.dispose()
        restarted = create_engine(self.url)
        replay = AdvisorRepository(restarted).finish_advice("owner", "host", "round-1", self.advice, randbelow=never_draw)
        self.assertFalse(replay.acquired)
        self.assertEqual(replay.record, first.record)
        restarted.dispose()

    def test_changed_advice_cannot_replace_committed_result(self) -> None:
        self.repo.reserve_round(self.round)
        self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=lambda _: 1)
        with self.assertRaises(AdvisorConflict):
            self.repo.finish_advice("owner", "host", "round-1", '{"candidate_id":"small-low","rationale":"other"}', randbelow=never_draw)

    def test_invalid_advice_has_no_committed_assignment(self) -> None:
        original = self.repo.reserve_round(self.round)
        with self.assertRaises(AdvisorContractError):
            self.repo.finish_advice("owner", "host", "round-1", "not JSON", randbelow=never_draw)
        self.assertEqual(self.repo.load_round("owner", "host", "round-1"), original.record)

    def test_confirmation_claims_execution_only_once(self) -> None:
        self.repo.reserve_round(self.round)
        review = self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=lambda _: 1)
        kwargs = dict(expected_version=review.record.version, live_catalog=self.catalog)
        first = self.repo.confirm("owner", "host", "round-1", **kwargs)
        replay = self.repo.confirm("owner", "host", "round-1", **kwargs)
        self.assertTrue(first.acquired)
        self.assertFalse(replay.acquired)
        self.assertEqual(first.record, replay.record)
        self.assertIsNone(first.record.payload["actual_execution"])

    def test_changed_confirmation_is_conflict(self) -> None:
        self.repo.reserve_round(self.round)
        review = self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=lambda _: 1)
        self.repo.confirm("owner", "host", "round-1", expected_version=review.record.version, live_catalog=self.catalog)
        with self.assertRaises(AdvisorConflict):
            self.repo.confirm("owner", "host", "round-1", expected_version=review.record.version, live_catalog=self.catalog, override_candidate_id="small-low", reason="change")

    def test_unavailable_assigned_model_does_not_claim_execution(self) -> None:
        self.repo.reserve_round(self.round)
        review = self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=lambda _: 1)
        with self.assertRaises(AdvisorContractError):
            self.repo.confirm("owner", "host", "round-1", expected_version=review.record.version, live_catalog=PoolSnapshot("new", (self.human,)))
        self.assertEqual(self.repo.load_round("owner", "host", "round-1"), review.record)

    def test_preexecution_cancel_blocks_advice_completion(self) -> None:
        reserved = self.repo.reserve_round(self.round)
        self.repo.cancel("owner", "host", "round-1", expected_version=reserved.record.version)
        with self.assertRaises(AdvisorConflict):
            self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=never_draw)

    def test_other_owner_cannot_read_or_confirm_round(self) -> None:
        self.repo.reserve_round(self.round)
        self.assertIsNone(self.repo.load_round("other", "host", "round-1"))
        with self.assertRaises(AdvisorConflict):
            self.repo.confirm("other", "host", "round-1", expected_version=1, live_catalog=self.catalog)

    def test_concurrent_reservation_has_one_winner(self) -> None:
        barrier = threading.Barrier(6)
        results, errors = [], []
        def work() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(self.repo.reserve_round(self.round).acquired)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=work) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False] * 5 + [True])

    def test_concurrent_finish_has_one_committed_draw(self) -> None:
        self.repo.reserve_round(self.round)
        barrier = threading.Barrier(6)
        draws, results, errors = [], [], []
        def draw(bound: int) -> int:
            draws.append(bound)
            return 1
        def work() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(self.repo.finish_advice("owner", "host", "round-1", self.advice, randbelow=draw).acquired)
            except AdvisorConflict:
                results.append(False)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=work) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(draws, [2])
        self.assertEqual(sorted(results), [False] * 5 + [True])


if __name__ == "__main__":
    unittest.main()
