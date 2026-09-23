"""Synthetic, provider-free v2 contracts. No inference or live catalog claims."""
from dataclasses import replace
import json

import pytest

from omnigent.model_advisor_provider_policy import (
    LogicalChoice, ProviderPolicyError, ProviderPreferences, ProviderSelection,
    QualifiedRoute, advisor_input, effective_pool, migrate_v1, permitted_fallback,
    plan_transport, resolve_advisor,
)

OPENAI_LOW = LogicalChoice("openai", "test-openai", "low")
OPENAI_HIGH = LogicalChoice("openai", "test-openai", "high")
GLM_HIGH = LogicalChoice("glm", "test-glm", "high")
CATALOG = (OPENAI_LOW, OPENAI_HIGH, GLM_HIGH)
PREFS = ProviderPreferences(
    True, ProviderSelection(selected_choice_ids=(OPENAI_LOW.choice_id, OPENAI_HIGH.choice_id)),
    ProviderSelection(selected_choice_ids=(GLM_HIGH.choice_id,)), OPENAI_HIGH.choice_id,
)


def route(transport="omniroute", choice=OPENAI_LOW, **kwargs):
    data = dict(
        choice=choice, transport=transport, route_id=f"fixture-{transport}",
        wire_model="gateway/test-openai" if transport == "omniroute" else "test-openai",
        wire_effort=choice.reasoning_effort,
        entitlement_kind="chatgpt_plan" if choice.provider == "openai" else "glm_plan",
        entitlement_key="fixture-same-plan-account",
        equivalence_key="fixture-same-checkpoint-effort-harness",
        catalog_revision="fixture-v1", ready=True,
    )
    data.update(kwargs)
    return QualifiedRoute(**data)


def test_off_on_restores_only_selected_combinations():
    before = PREFS.to_payload()
    off = PREFS.toggle_provider("openai", False)
    assert off.openai.selected_choice_ids == PREFS.openai.selected_choice_ids
    assert effective_pool(off, CATALOG) == (GLM_HIGH,)
    assert off.toggle_provider("openai", True).to_payload() == before


def test_pause_retains_advisor_and_ignores_collapse():
    off = PREFS.toggle_provider("openai", False)
    assert resolve_advisor(off, CATALOG) == OPENAI_HIGH
    collapsed = replace(PREFS, openai=replace(PREFS.openai, collapsed=True))
    assert effective_pool(collapsed, CATALOG) == effective_pool(PREFS, CATALOG)


def test_new_catalog_does_not_enroll_models():
    extra = LogicalChoice("openai", "new-model", "high")
    assert extra not in effective_pool(PREFS, (*CATALOG, extra))


def test_model_efforts_independent_and_shared_pool():
    reduced = replace(
        PREFS, openai=replace(PREFS.openai, selected_choice_ids=(OPENAI_LOW.choice_id,))
    )
    assert set(effective_pool(reduced, CATALOG)) == {OPENAI_LOW, GLM_HIGH}
    assert resolve_advisor(reduced, CATALOG) == OPENAI_HIGH


def test_transport_does_not_change_advisor_payload_or_candidate_ids():
    changed = PREFS.choose_transport("openai", "direct_only")
    assert advisor_input("Task", effective_pool(PREFS, CATALOG)) == advisor_input("Task",
         effective_pool(changed, CATALOG))
    encoded = json.dumps(advisor_input("Task", effective_pool(PREFS, CATALOG)))
    for forbidden in ("omniroute", "direct", "route_id", "entitlement_key", "fallback",
         "human_choice"):
        assert forbidden not in encoded


def test_advisor_payload_order_is_stable():
    assert advisor_input("Task", CATALOG) == advisor_input("Task", tuple(reversed(CATALOG)))


@pytest.mark.parametrize(
    "payload", [{}, [], None, {**PREFS.to_payload(), "credential": "not-real"}]
)
def test_strict_preferences_shape(payload):
    with pytest.raises(ProviderPolicyError):
        ProviderPreferences.from_payload(payload)


def test_roundtrip_restores_paused_provider():
    paused = PREFS.toggle_provider("openai", False)
    assert ProviderPreferences.from_payload(json.loads(json.dumps(paused.to_payload()))) == paused


@pytest.mark.parametrize(
    "key,value", [("enabled", 1), ("schema_version", True),
                  ("human_probability_percent", 101), ("human_probability_percent", True)]
)
def test_preferences_strict_scalars(key, value):
    with pytest.raises(ProviderPolicyError):
        ProviderPreferences.from_payload({**PREFS.to_payload(), key: value})


@pytest.mark.parametrize("model,effort", [("auto", "high"), ("custom/best", "high"),
     ("model", "default"), (" model", "low"), ("model", "")])
def test_no_automatic_or_ambiguous_choices(model, effort):
    with pytest.raises(ProviderPolicyError):
        LogicalChoice("openai", model, effort)


def test_disabled_feature_has_no_effective_pool():
    assert effective_pool(replace(PREFS, enabled=False), CATALOG) == ()


def test_empty_enabled_pool_is_explicit_failure():
    with pytest.raises(ProviderPolicyError):
        effective_pool(PREFS.toggle_provider("openai", False).toggle_provider("glm",
             False), CATALOG)


def test_missing_active_choice_fails_but_paused_choice_is_kept():
    with pytest.raises(ProviderPolicyError):
        effective_pool(PREFS, (GLM_HIGH,))
    assert effective_pool(PREFS.toggle_provider("openai", False), (GLM_HIGH,)) == (GLM_HIGH,)


def test_wrong_provider_cannot_admit_candidate():
    wrong = replace(PREFS, openai=ProviderSelection(selected_choice_ids=(GLM_HIGH.choice_id,)))
    with pytest.raises(ProviderPolicyError):
        effective_pool(wrong, CATALOG)


def test_duplicate_catalog_not_extra_weight():
    with pytest.raises(ProviderPolicyError):
        effective_pool(PREFS, (*CATALOG, OPENAI_LOW))


def test_route_aliases_are_server_attested_not_model_name_guesses():
    a, b = route(), route("direct")
    assert a.wire_model != b.wire_model
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", (a, b))
    assert plan.primary == a and plan.fallback == b
    assert plan.choice == OPENAI_LOW


def test_direct_mode_never_falls_back_to_gateway():
    a, b = route(), route("direct")
    plan = plan_transport(OPENAI_LOW, "direct_only", (a, b))
    assert plan.primary == b and plan.fallback is None


def test_gateway_unavailable_uses_qualified_direct_before_dispatch():
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", (route(ready=False), route("direct")))
    assert plan.primary.transport == "direct"
    assert plan.reason == "omniroute_unavailable_before_dispatch"


@pytest.mark.parametrize("routes", [(), (route(ready=False),), (route("direct", ready=False),
    ), (route(choice=OPENAI_HIGH),)])
def test_missing_selected_model_route_is_not_replaced(routes):
    with pytest.raises(ProviderPolicyError):
        plan_transport(OPENAI_LOW, "omniroute_preferred", routes)


@pytest.mark.parametrize("field", ["entitlement_key", "equivalence_key"])
def test_gateway_fallback_must_be_same_plan_and_execution_contract(field):
    with pytest.raises(ProviderPolicyError):
        plan_transport(OPENAI_LOW, "omniroute_preferred", (route(), route("direct",
             **{field: "other"})))


def test_paid_api_cannot_enter_subscription_route():
    with pytest.raises(ProviderPolicyError):
        route(entitlement_kind="openai_api")


def test_ambiguous_duplicate_accounts_require_explicit_binding():
    with pytest.raises(ProviderPolicyError):
        plan_transport(
            OPENAI_LOW, "omniroute_preferred", (route(), route(route_id="another-proxy"))
        )


@pytest.mark.parametrize(
    "cause", ["proxy_connect_failed", "proxy_circuit_open", "proxy_rejected_before_forward"]
)
def test_safe_pre_execution_gateway_failure_allows_exact_fallback(cause):
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", (route(), route("direct")))
    fallback = permitted_fallback(plan, cause=cause, upstream_not_started=True,
         output_seen=False, tools_started=False, thread_bound=False)
    assert fallback == plan.fallback


@pytest.mark.parametrize("cause", ["timeout", "http_502", "quota_exhausted", "partial_stream",
     "unknown", "provider_401"])
def test_ambiguous_or_account_failure_never_replays(cause):
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", (route(), route("direct")))
    assert permitted_fallback(plan, cause=cause, upstream_not_started=True, output_seen=False,
         tools_started=False, thread_bound=False) is None


@pytest.mark.parametrize("patch", [{"upstream_not_started": False}, {"output_seen": True},
     {"tools_started": True}, {"thread_bound": True}])
def test_never_replay_partial_started_or_existing_thread(patch):
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", (route(), route("direct")))
    evidence = dict(cause="proxy_connect_failed", upstream_not_started=True, output_seen=False,
         tools_started=False, thread_bound=False)
    assert permitted_fallback(plan, **{**evidence, **patch}) is None


def test_no_invented_fallback_when_direct_unqualified():
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", (route(),))
    assert permitted_fallback(plan, cause="proxy_connect_failed", upstream_not_started=True,
         output_seen=False, tools_started=False, thread_bound=False) is None


def test_migration_deduplicates_lanes_without_erasing_old_settings():
    old = dict(schema_version=1, enabled=True, allowed_candidate_ids=["gateway-old",
         "direct-old", "lost"], advisor_candidate_id="advisor-old", human_probability_percent=50)
    before = json.dumps(old)
    migrated = migrate_v1(old, {"gateway-old": OPENAI_LOW, "direct-old": OPENAI_LOW,
         "advisor-old": GLM_HIGH})
    assert migrated.openai.selected_choice_ids == (OPENAI_LOW.choice_id,)
    assert migrated.unresolved_legacy_ids == ("lost",)
    assert migrated.advisor_choice_id == GLM_HIGH.choice_id
    assert migrated.route_review_required == ("openai", "glm")
    assert json.dumps(old) == before
    with pytest.raises(ProviderPolicyError):
        effective_pool(migrated, CATALOG)


def test_migration_requires_explicit_policy_confirmation():
    old = dict(
        schema_version=1, enabled=True, allowed_candidate_ids=["old"],
        advisor_candidate_id="old", human_probability_percent=50,
    )
    migrated = migrate_v1(old, {"old": OPENAI_LOW})
    with pytest.raises(ProviderPolicyError):
        effective_pool(migrated, CATALOG)
    confirmed = migrated.choose_transport("openai", "direct_only")
    assert effective_pool(confirmed, CATALOG) == (OPENAI_LOW,)
