"""Offline policy tests; fixture scores/models are invented, not real TB4 claims."""

import json
import math
from dataclasses import replace

import pytest

from omnigent.model_advisor_tb4 import (
    FloorAdvice,
    FloorCandidate,
    FloorCatalog,
    FloorPreferences,
    FrozenFloorRound,
    TB4ContractError,
    TB4Evidence,
    assign_floor,
    build_floor_request,
    parse_floor_advice,
    require_dispatchable,
    resolve_floor,
)


def candidate(key, access="chatgpt_plan", effort="high", **changes):
    row = FloorCandidate(key, "lane-" + key, "provider", "connection-" + key,
                         "test-harness", "test-model-" + key, effort, access)
    return replace(row, **changes)


def evidence(row, score, **changes):
    item = TB4Evidence("e-" + row.candidate_id, row.model_id, row.reasoning_effort,
                       row.harness, row.harness, score,
                       "https://example.org/synthetic-fixture", "fixture-v1")
    return replace(item, **changes)


def catalog():
    rows = (candidate("weak"), candidate("strong", "glm_plan"),
            candidate("free", "free"), candidate("unknown", "unknown"))
    return FloorCatalog("cat-v1", "evidence-v1", "test-priority-v1", rows,
                        tuple(evidence(row, score) for row, score in zip(rows, (40, 80, 60, 99))),
                        tuple(row.candidate_id for row in rows))


def frozen(**changes):
    prefs = FloorPreferences(True, "paid_only", ("weak", "strong"), "strong")
    row = FrozenFloorRound("owner", "host", "default", "round", "settings-v1",
                           "Change a function and run its tests.", 40, prefs, catalog())
    return replace(row, **changes)


def no_draw(_):
    raise AssertionError("RNG should not run")


@pytest.mark.parametrize("bad", [True, False, None, "40", -1, 101, math.nan, math.inf, -math.inf,
                                  10**1000])
def test_floor_rejects_nonliteral_or_out_of_range_values(bad):
    with pytest.raises(TB4ContractError):
        FloorAdvice(bad, "test")


@pytest.mark.parametrize("value", [0, 0.0, -0.0, 38.5, 40, 40.00001, 100])
def test_literal_floor_accepted_without_rounding(value):
    assert FloorAdvice(value, "test").floor_percent == value


@pytest.mark.parametrize("raw", [
    "not JSON", "[]", "null", '{}', '{"floor_percent":40}',
    '{"floor_percent":40,"rationale":"ok","candidate_id":"strong"}',
    '{"floor_percent":40,"floor_percent":80,"rationale":"ok"}',
    '{"floor_percent":true,"rationale":"ok"}',
    '{"floor_percent":NaN,"rationale":"ok"}',
    '{"floor_percent":40,"rationale":" "}',
    '{"floor_percent":40,"rationale":[]}', "x" * 8193,
])
def test_advisor_json_is_strict(raw):
    with pytest.raises(TB4ContractError):
        parse_floor_advice(raw)


def test_parse_valid_advice():
    assert parse_floor_advice('{"floor_percent":38.5,"rationale":"test"}') == FloorAdvice(38.5, "test")


def test_outbound_request_does_not_depend_on_catalog_mode_human_pick_or_ranking():
    first = frozen()
    second = replace(first, human_floor_percent=99,
                     preferences=first.preferences.with_pool_mode("free_only"),
                     catalog=replace(first.catalog, evidence_revision="another",
                                     priority_ids=tuple(reversed(first.catalog.priority_ids))))
    assert first.advisor_input() == second.advisor_input() == build_floor_request(first.task)
    assert first.fingerprint != second.fingerprint
    payload = json.dumps(first.advisor_input())
    for secret in ("connection-strong", "test-model-strong", "settings-v1", "catalog_revision",
                   "human_floor_percent", "allowed_paid_candidate_ids"):
        assert secret not in payload
    with pytest.raises(TypeError):
        build_floor_request(first.task, catalog=first.catalog)


def test_round_snapshot_roundtrips_but_is_not_an_advisor_payload():
    first = frozen()
    saved = json.loads(json.dumps(first.to_payload()))
    second = FrozenFloorRound.from_payload(saved)
    assert second == first
    assert second.fingerprint == first.fingerprint
    assert "catalog" in saved and "catalog" not in second.advisor_input()
    saved["policy_version"] = "different"
    with pytest.raises(TB4ContractError):
        FrozenFloorRound.from_payload(saved)


@pytest.mark.parametrize("field,value", [("owner_id", "other"), ("host_id", "other"),
                                          ("profile", "other"), ("round_id", "other"),
                                          ("settings_revision", "other"),
                                          ("task", "Different task"), ("human_floor_percent", 41)])
def test_fingerprint_binds_scope_context_and_treatment(field, value):
    row = frozen()
    assert replace(row, **{field: value}).fingerprint != row.fingerprint


def test_explicit_preferences_conversion_preserves_paid_pool_advisor_and_balance():
    old = {"schema_version": 1, "enabled": True, "allowed_candidate_ids": ["strong"],
           "advisor_candidate_id": "weak", "human_probability_percent": 37}
    prefs = FloorPreferences.from_concrete_preferences(old)
    assert prefs.allowed_paid_candidate_ids == ("strong",)
    assert prefs.advisor_candidate_id == "weak"
    assert prefs.human_probability_percent == 37
    assert prefs.pool_mode == "paid_only"
    assert FloorPreferences.from_payload(prefs.to_payload()) == prefs
    assert prefs.with_pool_mode("free_only").with_pool_mode("paid_only") == prefs
    assert old["schema_version"] == 1


@pytest.mark.parametrize("change", [{"schema_version": True}, {"extra": "x"},
                                    {"human_probability_percent": True},
                                    {"allowed_paid_candidate_ids": ["weak", "weak"]},
                                    {"pool_mode": "mixed"}])
def test_invalid_floor_preferences_rejected(change):
    data = frozen().preferences.to_payload()
    data.update(change)
    with pytest.raises(TB4ContractError):
        FloorPreferences.from_payload(data)


def test_free_mode_uses_connection_class_not_model_name_and_retains_paid_advisor():
    row = frozen()
    cat = row.catalog
    free = replace(cat.get("free"), model_id="codex/looks-paid-but-is-free")
    cat = replace(cat, candidates=tuple(free if c.candidate_id == "free" else c for c in cat.candidates),
                  evidence=tuple(e for e in cat.evidence if e.evidence_id != "e-free") + (evidence(free, 60),))
    row = replace(row, catalog=cat, preferences=row.preferences.with_pool_mode("free_only"))
    result = resolve_floor(row, 0)
    assert result.admitted_ids == ("free",)
    assert result.selected == free
    assert row.preferences.advisor_candidate_id == "strong"
    assert ("unknown", "outside_execution_pool") in result.excluded
    assert row.preferences.allowed_paid_candidate_ids == ("weak", "strong")


@pytest.mark.parametrize("key", ["free", "unknown", "missing"])
def test_paid_pool_never_admits_nonplan_or_stale_choices(key):
    row = frozen()
    with pytest.raises(TB4ContractError):
        replace(row, preferences=replace(row.preferences, allowed_paid_candidate_ids=(key,)))


def test_paid_allowlist_cannot_expand_when_catalog_refreshes():
    row = frozen()
    row = replace(row, preferences=replace(row.preferences, allowed_paid_candidate_ids=("weak",)))
    assert resolve_floor(row, 0).admitted_ids == ("weak",)
    assert resolve_floor(row, 41).selected is None


def test_higher_floor_only_removes_candidates_not_unlocks_stronger_ones():
    row = frozen()
    low = resolve_floor(row, 40)
    high = resolve_floor(row, math.nextafter(40, math.inf))
    assert low.admitted_ids == ("weak", "strong")
    assert high.admitted_ids == ("strong",)
    assert set(high.admitted_ids) <= set(low.admitted_ids)
    assert low.selected.candidate_id == "weak"
    assert high.selected.candidate_id == "strong"
    assert resolve_floor(row, 81).selected is None
    previous = set(resolve_floor(row, 0).admitted_ids)
    for score in range(101):
        current = set(resolve_floor(row, score).admitted_ids)
        assert current <= previous
        previous = current


def test_router_priority_not_highest_tb4_wins():
    row = frozen()
    assert resolve_floor(row, 0).selected.candidate_id == "weak"
    reverse = replace(row, catalog=replace(row.catalog, priority_ids=tuple(reversed(row.catalog.priority_ids))))
    assert resolve_floor(reverse, 0).selected.candidate_id == "strong"


@pytest.mark.parametrize("change", [{"reasoning_effort": "low"}, {"model_id": "other"},
                                    {"executor_harness": "other", "benchmark_harness": "other"}])
def test_no_model_effort_or_harness_evidence_inheritance(change):
    row = frozen()
    cat = row.catalog
    changed = replace(cat.evidence[0], **change)
    row = replace(row, catalog=replace(cat, evidence=(changed,) + cat.evidence[1:]))
    assert ("weak", "missing_tb4_evidence") in resolve_floor(row, 0).excluded


def test_missing_and_ambiguous_evidence_fail_closed_even_at_zero_floor():
    row = frozen()
    missing = replace(row, catalog=replace(row.catalog, evidence=()))
    assert resolve_floor(missing, 0).selected is None
    duplicate = replace(row.catalog.evidence[0], evidence_id="second-source")
    ambiguous = replace(row, catalog=replace(row.catalog, evidence=row.catalog.evidence + (duplicate,)))
    assert ("weak", "ambiguous_tb4_evidence") in resolve_floor(ambiguous, 0).excluded


def test_cross_harness_transfer_must_be_explicit_and_recorded():
    item = catalog().evidence[0]
    with pytest.raises(TB4ContractError):
        replace(item, benchmark_harness="another-harness")
    changed = replace(item, benchmark_harness="another-harness", harness_transfer_policy="reviewed-v1")
    assert changed.benchmark_harness != changed.executor_harness
    assert changed.harness_transfer_policy == "reviewed-v1"


@pytest.mark.parametrize("change", [{"benchmark_version": "2.0.0"}, {"slice_id": "tb4.software"}])
def test_other_benchmark_versions_or_slices_rejected(change):
    with pytest.raises(TB4ContractError):
        replace(catalog().evidence[0], **change)


def test_fair_coin_boundaries_and_agreement_do_not_reroll():
    row = frozen()
    advice = FloorAdvice(70, "test")
    human = assign_floor(row, advice, draw=lambda _: 49)
    advisor = assign_floor(row, advice, draw=lambda _: 50)
    assert (human.arm, human.propensity_percent, human.floor_percent) == ("human", 50, 40)
    assert (advisor.arm, advisor.propensity_percent, advisor.floor_percent) == ("advisor", 50, 70)
    assert assign_floor(row, advice, draw=no_draw, existing=advisor) == advisor
    same = assign_floor(row, FloorAdvice(40, "same"), draw=no_draw)
    assert (same.arm, same.propensity_percent) == ("same", 100)


@pytest.mark.parametrize("probability,arm", [(0, "advisor"), (100, "human")])
def test_preserved_balance_endpoints_need_no_rng(probability, arm):
    row = frozen()
    row = replace(row, preferences=replace(row.preferences, human_probability_percent=probability))
    assignment = assign_floor(row, FloorAdvice(70, "test"), draw=no_draw)
    assert assignment.arm == arm and assignment.propensity_percent == 100


@pytest.mark.parametrize("bad", [-1, 100, True, 49.5, "49"])
def test_bad_rng_values_rejected(bad):
    with pytest.raises(TB4ContractError):
        assign_floor(frozen(), FloorAdvice(70, "test"), draw=lambda _: bad)


@pytest.mark.parametrize("change", [{"floor_percent": 39}, {"propensity_percent": 37}, {"arm": "same"}])
def test_tampered_saved_assignment_cannot_replay(change):
    row = frozen()
    advice = FloorAdvice(70, "test")
    assignment = assign_floor(row, advice, draw=lambda _: 0)
    with pytest.raises(TB4ContractError):
        assign_floor(row, advice, draw=no_draw, existing=replace(assignment, **change))


def test_changed_round_or_advice_cannot_replay():
    row = frozen()
    advice = FloorAdvice(70, "test")
    assignment = assign_floor(row, advice, draw=lambda _: 0)
    with pytest.raises(TB4ContractError):
        assign_floor(replace(row, task="changed"), advice, draw=no_draw, existing=assignment)
    with pytest.raises(TB4ContractError):
        assign_floor(row, replace(advice, rationale="changed"), draw=no_draw, existing=assignment)


def test_different_floors_can_resolve_to_same_executor_without_being_agreement():
    row = frozen(human_floor_percent=41)
    assignment = assign_floor(row, FloorAdvice(70, "test"), draw=lambda _: 0)
    assert assignment.arm == "human"
    assert resolve_floor(row, 41).selected == resolve_floor(row, 70).selected


@pytest.mark.parametrize("change", [{"available": False}, {"connection_id": "other"},
                                    {"reasoning_effort": "low"}, {"access_class": "free"}])
def test_dispatch_blocks_unavailability_reclassification_or_route_drift(change):
    row = frozen()
    assignment = assign_floor(row, FloorAdvice(70, "test"), draw=lambda _: 0)
    live = replace(row.catalog, candidates=(replace(row.catalog.candidates[0], **change),) + row.catalog.candidates[1:])
    with pytest.raises(TB4ContractError):
        require_dispatchable(row, assignment, live)


def test_dispatch_blocks_changed_evidence_and_empty_pool_but_not_irrelevant_reordering():
    row = frozen()
    assignment = assign_floor(row, FloorAdvice(70, "test"), draw=lambda _: 0)
    live = replace(row.catalog, priority_ids=tuple(reversed(row.catalog.priority_ids)))
    assert require_dispatchable(row, assignment, live).candidate_id == "weak"
    live = replace(live, evidence=(replace(live.evidence[0], score_percent=99),) + live.evidence[1:])
    with pytest.raises(TB4ContractError):
        require_dispatchable(row, assignment, live)
    blocked = assign_floor(row, FloorAdvice(100, "test"), draw=lambda _: 99)
    with pytest.raises(TB4ContractError):
        require_dispatchable(row, blocked, row.catalog)


def test_unavailable_executor_may_be_skipped_before_assignment_not_after():
    row = frozen()
    cat = replace(row.catalog, candidates=(replace(row.catalog.candidates[0], available=False),) + row.catalog.candidates[1:])
    row = replace(row, catalog=cat)
    assert resolve_floor(row, 0).selected.candidate_id == "strong"


def test_unavailable_advisor_never_silently_uses_another_one():
    row = frozen()
    cat = replace(row.catalog, candidates=tuple(replace(c, available=False) if c.candidate_id == "strong" else c
                                                for c in row.catalog.candidates))
    with pytest.raises(TB4ContractError):
        replace(row, catalog=cat)


@pytest.mark.parametrize("field,value", [("model_id", "auto"), ("model_id", "custom/combo"),
                                          ("reasoning_effort", "default"), ("candidate_id", " bad")])
def test_dynamic_alias_or_noncanonical_id_rejected(field, value):
    with pytest.raises(TB4ContractError):
        replace(catalog().candidates[0], **{field: value})


def test_invalid_catalog_priority_duplicates_and_routes_rejected():
    cat = catalog()
    for keys in [("weak",), ("weak", "weak", "free", "unknown")]:
        with pytest.raises(TB4ContractError):
            replace(cat, priority_ids=keys)
    with pytest.raises(TB4ContractError):
        replace(cat, evidence=cat.evidence + (cat.evidence[0],))
    with pytest.raises(TB4ContractError):
        replace(cat, candidates=(cat.candidates[0], replace(cat.candidates[0], candidate_id="strong")) + cat.candidates[2:])


def test_review_records_floor_pool_route_and_unknown_actual_separately():
    from omnigent.model_advisor_tb4 import floor_review_payload

    row = frozen(human_floor_percent=41)
    assignment = assign_floor(row, FloorAdvice(70, "test"), draw=lambda _: 0)
    review = floor_review_payload(row, assignment)
    assert review["different_floors_same_executor"] is True
    assert review["assigned_arm"] == "human"
    assert review["configured_execution"]["candidate_id"] == "strong"
    assert review["actual_execution"] is None
    assert review["advisor_usage"] is None and review["executor_usage"] is None
    assert "task" not in review
    assert review["assigned_admitted_ids"] == ["strong"]
    assert "catalog" not in row.advisor_input()


def test_review_empty_admission_is_blocked_not_a_silent_manual_fallback():
    from omnigent.model_advisor_tb4 import floor_review_payload

    row = frozen()
    assignment = assign_floor(row, FloorAdvice(100, "test"), draw=lambda _: 99)
    review = floor_review_payload(row, assignment)
    assert review["execution_blocked"] is True
    assert review["configured_execution"] is None
    assert review["assigned_floor_percent"] == 100
    assert review["assigned_admitted_ids"] == []
