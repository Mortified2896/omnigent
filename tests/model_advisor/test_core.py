"""Provider-free tests of the additive advisor contract, not live integration."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from omnigent.model_advisor_core import (
    AdvisorContractError,
    Candidate,
    PoolSnapshot,
    RoundChoices,
    build_advisor_request,
    parse_advisor_result,
    prepare_assignment,
    require_exact_available,
    task_fingerprint,
)


def candidate(candidate_id: str = "plan-small-low", **overrides: str) -> Candidate:
    values = {
        "candidate_id": candidate_id,
        "lane_id": "chatgpt-direct",
        "provider_id": "codex",
        "connection_id": "fixture-chatgpt-account",
        "harness": "codex-native",
        "model_id": "fixture-small",
        "reasoning_effort": "low",
        "access_class": "chatgpt_plan",
    }
    values.update(overrides)
    return Candidate(**values)


def must_not_draw() -> int:
    raise AssertionError("Replay/agreement must not randomize")


class AdvisorCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.human = candidate()
        self.advice = candidate(
            "glm-high",
            lane_id="zai-direct",
            provider_id="zai",
            connection_id="fixture-glm-account",
            model_id="fixture-glm",
            reasoning_effort="high",
            access_class="glm_plan",
        )
        self.pool = PoolSnapshot("catalog-1", (self.human, self.advice))
        self.round = RoundChoices(
            "owner-1",
            "host-1",
            "round-1",
            "settings-1",
            task_fingerprint("Explain this task."),
            self.pool,
            self.advice,
            self.human.candidate_id,
            self.advice.candidate_id,
        )

    def test_only_two_entitlement_classes(self) -> None:
        for access in ("openai_api", "free", "unknown", "subscription"):
            with self.subTest(access=access), self.assertRaises(AdvisorContractError):
                candidate(access_class=access)

    def test_checkpoint_namespace_does_not_choose_access_lane(self) -> None:
        row = replace(self.advice, model_id="openai/fixture-checkpoint")
        self.assertEqual(row.access_class, "glm_plan")
        self.assertEqual(row.lane_id, "zai-direct")

    def test_router_and_default_aliases_are_rejected(self) -> None:
        for model in ("auto", "default", "Smart", "auto/best", "custom/combo"):
            with self.subTest(model=model), self.assertRaises(AdvisorContractError):
                candidate(model_id=model)
        with self.assertRaises(AdvisorContractError):
            candidate(reasoning_effort="default")

    def test_noncanonical_identifiers_are_rejected(self) -> None:
        for model in (" auto ", "model\nname", " model"):
            with self.subTest(model=model), self.assertRaises(AdvisorContractError):
                candidate(model_id=model)

    def test_explicit_no_effort_supported(self) -> None:
        self.assertEqual(
            candidate(reasoning_effort="not_applicable").reasoning_effort, "not_applicable"
        )

    def test_pool_requires_nonempty_immutable_membership(self) -> None:
        for rows in ((), [self.human], (object(),)):
            with self.subTest(rows=rows), self.assertRaises(AdvisorContractError):
                PoolSnapshot("catalog-1", rows)

    def test_pool_rejects_duplicate_ids(self) -> None:
        with self.assertRaises(AdvisorContractError):
            PoolSnapshot(
                "catalog-1",
                (self.human, replace(self.advice, candidate_id=self.human.candidate_id)),
            )

    def test_pool_rejects_duplicate_route_under_alias(self) -> None:
        with self.assertRaises(AdvisorContractError):
            PoolSnapshot("catalog-1", (self.human, replace(self.human, candidate_id="alias")))

    def test_same_checkpoint_different_lanes_remain_distinct(self) -> None:
        other = replace(self.advice, model_id=self.human.model_id, reasoning_effort="low")
        pool = PoolSnapshot("catalog-1", (self.human, other))
        result = prepare_assignment(replace(self.round, pool=pool), draw_bit=lambda: 1)
        self.assertEqual(result.arm, "advisor")
        self.assertNotEqual(pool.candidates[0].route_identity, pool.candidates[1].route_identity)

    def test_different_efforts_are_distinct_choices(self) -> None:
        other = replace(self.human, candidate_id="plan-small-high", reasoning_effort="high")
        self.assertEqual(len(PoolSnapshot("catalog-1", (self.human, other)).candidates), 2)

    def test_both_proposals_must_be_in_same_pool(self) -> None:
        for field in ("human_candidate_id", "advisor_candidate_id"):
            with self.subTest(field=field), self.assertRaises(AdvisorContractError):
                replace(self.round, **{field: "outside"})

    def test_advisor_model_can_be_outside_answer_pool(self) -> None:
        judge = candidate("advisor-only", model_id="fixture-advisor")
        result = prepare_assignment(replace(self.round, advisor=judge), draw_bit=lambda: 0)
        self.assertEqual(result.selected, self.human)

    def test_advisor_payload_has_no_human_choice_or_review_metadata(self) -> None:
        request = build_advisor_request("Explain this task.", self.pool)
        self.assertEqual(
            set(request), {"schema_version", "instructions", "task", "candidates", "output_schema"}
        )
        wire = json.dumps(request)
        for forbidden in (
            "human_candidate_id",
            "owner-1",
            "fixture-chatgpt-account",
            "review_source",
        ):
            self.assertNotIn(forbidden, wire)
        self.assertEqual(request, build_advisor_request("Explain this task.", self.pool))

    def test_output_schema_contains_only_frozen_ids(self) -> None:
        schema = build_advisor_request("task", self.pool)["output_schema"]
        self.assertEqual(
            schema["properties"]["candidate_id"]["enum"], ["plan-small-low", "glm-high"]
        )
        self.assertFalse(schema["additionalProperties"])

    def test_valid_advisor_json_selects_exact_tuple(self) -> None:
        row, reason = parse_advisor_result(
            '{"candidate_id":"glm-high","rationale":"Hard task"}', self.pool
        )
        self.assertEqual(row, self.advice)
        self.assertEqual(reason, "Hard task")

    def test_invalid_or_extended_advisor_json_fails(self) -> None:
        cases = (
            "not json",
            "[]",
            "null",
            "{}",
            '{"candidate_id":"outside","rationale":"x"}',
            '{"candidate_id":"glm-high","rationale":"x","model":"other"}',
            '{"candidate_id":"glm-high","candidate_id":"plan-small-low","rationale":"x"}',
            '{"candidate_id":null,"rationale":"x"}',
            '{"candidate_id":"glm-high","rationale":""}',
            json.dumps({"candidate_id": "glm-high", "rationale": "x" * 601}),
            "x" * 8193,
        )
        for raw in cases:
            with self.subTest(raw=raw[:100]), self.assertRaises(AdvisorContractError):
                parse_advisor_result(raw, self.pool)

    def test_zero_draw_uses_human_at_half_probability(self) -> None:
        result = prepare_assignment(self.round, draw_bit=lambda: 0)
        self.assertEqual((result.arm, result.selected), ("human", self.human))
        self.assertEqual((result.probability_numerator, result.probability_denominator), (1, 2))

    def test_one_draw_uses_advisor_at_half_probability(self) -> None:
        result = prepare_assignment(self.round, draw_bit=lambda: 1)
        self.assertEqual((result.arm, result.selected), ("advisor", self.advice))
        self.assertEqual((result.probability_numerator, result.probability_denominator), (1, 2))

    def test_agreement_does_not_draw_or_claim_arm_win(self) -> None:
        choices = replace(self.round, advisor_candidate_id=self.human.candidate_id)
        result = prepare_assignment(choices, draw_bit=must_not_draw)
        self.assertEqual((result.arm, result.probability_denominator), ("same", 1))
        self.assertEqual(result.selected, self.human)

    def test_replay_does_not_rerandomize(self) -> None:
        for bit in (0, 1):
            saved = prepare_assignment(self.round, draw_bit=lambda b=bit: b)
            self.assertIs(
                prepare_assignment(self.round, draw_bit=must_not_draw, existing=saved), saved
            )

    def test_changed_request_cannot_reuse_assignment(self) -> None:
        saved = prepare_assignment(self.round, draw_bit=lambda: 0)
        variants = (
            replace(self.round, owner_id="other-owner"),
            replace(self.round, host_id="other-host"),
            replace(self.round, round_id="other-round"),
            replace(self.round, settings_revision="settings-2"),
            replace(self.round, task_digest=task_fingerprint("Edited task")),
            replace(self.round, pool=replace(self.pool, catalog_revision="catalog-2")),
            replace(self.round, advisor=self.human),
            replace(self.round, advisor_candidate_id=self.human.candidate_id),
        )
        for changed in variants:
            with (
                self.subTest(changed=changed.fingerprint),
                self.assertRaises(AdvisorContractError),
            ):
                prepare_assignment(changed, draw_bit=must_not_draw, existing=saved)

    def test_tampered_assignment_cannot_replay(self) -> None:
        saved = prepare_assignment(self.round, draw_bit=lambda: 0)
        variants = (
            replace(saved, selected=self.advice),
            replace(saved, arm="same"),
            replace(saved, probability_denominator=1),
        )
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(AdvisorContractError):
                prepare_assignment(self.round, draw_bit=must_not_draw, existing=changed)

    def test_invalid_rng_output_rejected(self) -> None:
        for bit in (True, False, 0.5, -1, 2, "1"):
            with self.subTest(bit=bit), self.assertRaises(AdvisorContractError):
                prepare_assignment(self.round, draw_bit=lambda b=bit: b)

    def test_task_hash_binds_exact_text_and_context(self) -> None:
        original = task_fingerprint("task")
        self.assertNotEqual(original, task_fingerprint("task "))
        self.assertNotEqual(
            original, task_fingerprint("task", execution_context_digest="different")
        )

    def test_empty_task_and_invalid_digest_rejected(self) -> None:
        with self.assertRaises(AdvisorContractError):
            build_advisor_request(" ", self.pool)
        with self.assertRaises(AdvisorContractError):
            replace(self.round, task_digest="not-a-digest")

    def test_exact_route_readiness_never_selects_another_choice(self) -> None:
        saved = prepare_assignment(self.round, draw_bit=lambda: 1)
        self.assertEqual(require_exact_available(saved, self.pool.candidates), self.advice)
        for available in ((self.human,), (), (replace(self.advice, reasoning_effort="low"),)):
            with self.subTest(available=available), self.assertRaises(AdvisorContractError):
                require_exact_available(saved, available)

    def test_reused_id_with_changed_connection_is_not_exact_route(self) -> None:
        saved = prepare_assignment(self.round, draw_bit=lambda: 1)
        changed = replace(self.advice, connection_id="different-account")
        with self.assertRaises(AdvisorContractError):
            require_exact_available(saved, (changed,))


if __name__ == "__main__":
    unittest.main()
