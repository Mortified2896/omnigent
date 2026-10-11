from scripts.routing_shadow_policy import comparison, recommend


def observation(group, profile_id, success, run="run", **changes):
    return {
        "run_id": run,
        "task_id": group,
        "task_group": group,
        "task_family": "repository-change",
        "task_fingerprint": "same-input",
        "profile": {
            "id": profile_id,
            "model": "fixture-" + profile_id,
            "effort": "fixture-effort",
        },
        "success": success,
        "budget_seconds": 60,
        "verifier_validated": True,
        "offline_comparison_eligible": True,
        "verifier_hashes": {"checks": "digest"},
        "environment": {"harness": "fixture"},
        **changes,
    }


def setup():
    shared = {
        "harness": "fixture",
        "access_lane": "direct",
        "host_id": "local",
        "workspace_id": "repo",
    }
    profiles = [
        {
            "id": pid,
            "model": "fixture-" + pid,
            "effort": "fixture-effort",
            "qualified": True,
            **shared,
        }
        for pid in ("baseline", "candidate")
    ]
    context = {
        "task_family": "repository-change",
        "baseline_profile_id": "baseline",
        "current_profile_id": "baseline",
        "allowed_profile_ids": ["baseline", "candidate"],
        **shared,
    }
    return context, profiles


def test_repeats_cannot_create_independent_evidence_and_unknowns_are_not_failures():
    rows = [
        row
        for n in range(30)
        for row in (
            observation("one", "baseline", False, run=str(n)),
            observation("one", "candidate", True, run=str(n)),
        )
    ]
    rows.append(observation("missing", "baseline", None))
    context, profiles = setup()
    result = recommend(context, profiles, rows)
    assert result["recommended_profile_id"] == "baseline"
    evidence = result["comparisons"][0]
    assert evidence["task_groups"] == 1
    assert evidence["paired_trials"] == 30
    assert evidence["rejected"]["incomplete_or_unverified_record"] == 1


def test_unpaired_mismatched_budgets_and_duplicate_treatments_do_not_count():
    rows = [
        observation("one", "baseline", False),
        observation("one", "candidate", True, budget_seconds=61),
    ]
    rows += [
        observation("two", "baseline", False),
        observation("two", "candidate", True),
        observation("two", "candidate", False),
    ]
    evidence = comparison(rows, "baseline", "candidate", "repository-change")
    assert evidence["task_groups"] == 0
    assert evidence["rejected"]["unpaired_or_duplicate_treatment"] == 3


def test_policy_can_admit_grouped_paired_gains_but_never_actuates():
    rows = [
        row
        for n in range(10)
        for row in (observation(str(n), "baseline", False), observation(str(n), "candidate", True))
    ]
    context, profiles = setup()
    result = recommend(context, profiles, rows)
    assert result["recommended_profile_id"] == "candidate"
    assert result["actuated"] is False
    assert result["predicted_success_probability"] is None
    assert result["comparisons"][0]["one_sided_sign_p"] == 1 / 1024


def test_explicit_choice_active_session_and_unproven_continuation_take_priority():
    context, profiles = setup()
    assert (
        recommend({**context, "explicit_profile_id": "owner-choice"}, profiles, [])[
            "recommended_profile_id"
        ]
        == "owner-choice"
    )
    active = recommend({**context, "has_native_session": True, "idle": False}, profiles, [])
    assert active["reason"] == "preserve_active_native_session"
    idle = recommend({**context, "has_native_session": True, "idle": True}, profiles, [])
    assert idle["comparisons"] == []
    assert idle["recommended_profile_id"] == "baseline"


def test_host_lane_workspace_qualification_and_family_are_required():
    context, profiles = setup()
    profiles[1]["host_id"] = "other-host"
    assert recommend(context, profiles, [])["comparisons"] == []
    profiles[0]["qualified"] = False
    assert recommend(context, profiles, [])["reason"] == "baseline_unqualified_preserve_current"


def test_unavailable_treatments_and_profile_alias_collisions_are_excluded():
    context, profiles = setup()
    rows = [
        observation("one", "baseline", False, offline_comparison_eligible=False),
        observation("one", "candidate", True),
        observation("two", "baseline", False),
        observation(
            "two",
            "candidate",
            True,
            profile={"id": "candidate", "model": "different", "effort": "fixture-effort"},
        ),
    ]
    result = recommend(context, profiles, rows)
    evidence = result["comparisons"][0]
    assert evidence["task_groups"] == 0
    assert evidence["rejected"]["incomplete_or_unverified_record"] == 1
    assert evidence["rejected"]["requested_profile_mismatch"] == 1
