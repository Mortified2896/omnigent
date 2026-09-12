"""Offline shared-budget/metadata fixtures; no live files or database are changed."""

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import managed_budget as budget

NOW = dt.datetime(2026, 9, 11, 6, tzinfo=dt.timezone.utc)


class PolicyTests(unittest.TestCase):
    def test_approved_total_and_unchanged_age_limits(self):
        policy = budget.load_policy()
        self.assertEqual(policy["total_max_bytes"], 50_000_000_000)
        self.assertEqual(policy["forensic_max_bytes"], 4_000_000_000)
        self.assertEqual(
            policy["retention_days"], {"lean": 60, "forensic": 3, "captures": 30, "metadata": 30}
        )

    def test_invalid_policy_cannot_silently_default(self):
        for policy in (None, [], {}, {"schema_version": 99}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                budget.validate_policy(policy)

    def test_strict_integer_limits(self):
        for key in ("schema_version", "total_max_bytes", "forensic_max_bytes"):
            for value in (True, None, -1, 1.5, "50"):
                policy = budget.load_policy()
                policy[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    budget.validate_policy(policy)

    def test_subbudget_cannot_exceed_total(self):
        policy = budget.load_policy()
        policy["forensic_max_bytes"] = policy["total_max_bytes"] + 1
        with self.assertRaises(ValueError):
            budget.validate_policy(policy)

    def test_retention_policy_must_be_complete(self):
        for value in (
            {"metadata": 30},
            [],
            {"lean": 60, "forensic": 3, "captures": 30, "metadata": False},
        ):
            policy = budget.load_policy()
            policy["retention_days"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                budget.validate_policy(policy)

    def test_loader_rejects_duplicate_keys_and_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            for text in (
                '{"schema_version":1,"schema_version":1}',
                '{"secret":"unterminated',
                "[]",
            ):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError) as raised:
                    budget.load_policy(path)
                self.assertNotIn("secret", str(raised.exception))

    def test_loader_rejects_oversized_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_bytes(b" " * 65_537)
            with self.assertRaises(ValueError):
                budget.load_policy(path)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.policy = budget.load_policy()
        self.snapshot = {
            "complete": True,
            "observed_at": NOW.isoformat(),
            "reserved_bytes": 0,
            "components": {name: {"bytes": 0, "protected_bytes": 0} for name in budget.COMPONENTS},
        }

    def assess(self, growth=1):
        return budget.assess_budget(
            self.snapshot,
            policy=self.policy,
            now=NOW,
            max_age_seconds=300,
            requested_growth_bytes=growth,
        )

    def test_empty_verified_snapshot_does_not_claim_enforcement(self):
        result = self.assess()
        self.assertEqual(result["used_bytes"], 0)
        self.assertTrue(result["fits_snapshot"])
        self.assertFalse(result["enforcement_verified"])
        self.assertFalse(result["reservation_created"])

    def test_every_component_counts_in_total(self):
        for item in self.snapshot["components"].values():
            item["bytes"] = 100
        self.assertEqual(self.assess()["used_bytes"], 700)

    def test_former_separate_50gb_plus_10gib_is_over_budget(self):
        self.snapshot["components"]["otel_archive"]["bytes"] = 50_000_000_000
        self.snapshot["components"]["provenance_captures"]["bytes"] = 10 * 1024**3
        result = self.assess()
        self.assertEqual(result["status"], "OVER_BUDGET")
        self.assertEqual(result["overshoot_bytes"], 10 * 1024**3)
        self.assertFalse(result["fits_snapshot"])

    def test_frozen_bytes_are_not_subtracted(self):
        item = self.snapshot["components"]["provenance_captures"]
        item.update(bytes=49_000_000_000, protected_bytes=49_000_000_000)
        result = self.assess(2_000_000_000)
        self.assertEqual(result["protected_bytes"], 49_000_000_000)
        self.assertEqual(result["used_bytes"], 49_000_000_000)
        self.assertFalse(result["fits_snapshot"])

    def test_reservations_reduce_headroom(self):
        self.snapshot["components"]["otel_archive"]["bytes"] = 48_000_000_000
        self.snapshot["reserved_bytes"] = 1_500_000_000
        self.assertEqual(self.assess()["available_bytes"], 500_000_000)
        self.assertFalse(self.assess(500_000_001)["fits_snapshot"])

    def test_full_budget_cannot_accept_a_byte(self):
        self.snapshot["components"]["other_managed"]["bytes"] = 50_000_000_000
        self.assertFalse(self.assess(1)["fits_snapshot"])
        self.assertTrue(self.assess(0)["fits_snapshot"])

    def test_exact_growth_boundary(self):
        self.snapshot["components"]["otel_archive"]["bytes"] = 49_999_999_900
        self.assertTrue(self.assess(100)["fits_snapshot"])
        self.assertFalse(self.assess(101)["fits_snapshot"])

    def test_unbounded_reservation_pressure_cannot_be_ignored(self):
        self.snapshot["reserved_bytes"] = 50_000_000_001
        self.assertFalse(self.assess(0)["fits_snapshot"])
        self.assertEqual(self.assess()["available_bytes"], 0)

    def test_missing_or_extra_component_rejects_coverage(self):
        del self.snapshot["components"]["telemetry_backups"]
        self.assertEqual(self.assess()["status"], "INCOMPLETE")
        self.snapshot["components"]["forensic_double_count"] = {"bytes": 0, "protected_bytes": 0}
        self.assertFalse(self.assess()["fits_snapshot"])

    def test_forensic_is_not_a_second_top_level_budget(self):
        self.snapshot["components"]["forensic"] = {"bytes": 0, "protected_bytes": 0}
        self.assertEqual(self.assess()["reason"], "component_coverage_mismatch")

    def test_incomplete_inventory_blocks_admission(self):
        for value in (False, None, "true", 1):
            self.snapshot["complete"] = value
            with self.subTest(value=value):
                self.assertEqual(self.assess()["status"], "INCOMPLETE")
                self.assertFalse(self.assess()["fits_snapshot"])

    def test_unknown_component_is_not_zero(self):
        for value in (None, True, -1, 1.5, "100"):
            self.snapshot["components"]["provenance_database"]["bytes"] = value
            with self.subTest(value=value):
                self.assertEqual(self.assess()["reason"], "invalid_component_measurement")

    def test_protected_must_be_known_subset(self):
        for value in (None, True, -1, 1):
            self.snapshot["components"]["telemetry_backups"]["protected_bytes"] = value
            with self.subTest(value=value):
                self.assertFalse(self.assess()["fits_snapshot"])

    def test_unknown_reservations_fail_closed(self):
        del self.snapshot["reserved_bytes"]
        self.assertEqual(self.assess()["reason"], "unknown_reservations")

    def test_future_stale_and_unscoped_time(self):
        for stamp in (
            NOW + dt.timedelta(seconds=1),
            NOW - dt.timedelta(seconds=301),
            NOW.replace(tzinfo=None),
        ):
            self.snapshot["observed_at"] = stamp.isoformat()
            with self.subTest(stamp=stamp):
                self.assertFalse(self.assess()["fits_snapshot"])

    def test_freshness_boundary(self):
        self.snapshot["observed_at"] = (NOW - dt.timedelta(seconds=300)).isoformat()
        self.assertTrue(self.assess()["fits_snapshot"])

    def test_invalid_evaluation_arguments(self):
        for overrides in (
            {"requested_growth_bytes": -1},
            {"requested_growth_bytes": True},
            {"max_age_seconds": 0},
            {"now": NOW.replace(tzinfo=None)},
        ):
            args = {"policy": self.policy, "now": NOW, "max_age_seconds": 300} | overrides
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                budget.assess_budget(self.snapshot, **args)

    def test_snapshot_is_not_mutated_or_published_in_result(self):
        self.snapshot["private_extra"] = "sensitive-canary"
        before = copy.deepcopy(self.snapshot)
        with patch.object(Path, "unlink", side_effect=AssertionError("must not delete")):
            result = self.assess()
        self.assertEqual(before, self.snapshot)
        self.assertNotIn("sensitive-canary", json.dumps(result))


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.policy = budget.load_policy()
        self.record = {
            "completed": True,
            "completed_at": (NOW - dt.timedelta(days=31)).isoformat(),
            "frozen": False,
            "in_flight": False,
            "needed_for_retained_evidence": False,
            "needed_for_rollback": False,
            "captures_pruned": True,
        }

    def decision(self):
        return budget.metadata_cleanup_decision(self.record, policy=self.policy, now=NOW)

    def test_old_unreferenced_completed_metadata_is_eligible(self):
        self.assertTrue(self.decision()["eligible"])
        self.assertFalse(self.decision()["deletion_performed"])

    def test_exact_30day_boundary_is_retained(self):
        self.record["completed_at"] = (NOW - dt.timedelta(days=30)).isoformat()
        self.assertFalse(self.decision()["eligible"])

    def test_each_protection_is_preserved(self):
        for flag in ("frozen", "in_flight", "needed_for_retained_evidence", "needed_for_rollback"):
            for value in (True, None, "false", 0):
                record = self.record | {flag: value}
                with self.subTest(flag=flag, value=value):
                    self.assertFalse(
                        budget.metadata_cleanup_decision(record, policy=self.policy, now=NOW)[
                            "eligible"
                        ]
                    )

    def test_missing_protection_cannot_default_to_false(self):
        del self.record["frozen"]
        self.assertFalse(self.decision()["eligible"])

    def test_captures_retained_or_unknown_prevent_metadata_deletion(self):
        for value in (False, None, "true", 1):
            self.record["captures_pruned"] = value
            with self.subTest(value=value):
                self.assertFalse(self.decision()["eligible"])

    def test_unfinished_record_is_retained(self):
        self.record["completed"] = False
        self.assertFalse(self.decision()["eligible"])

    def test_future_or_missing_completion_is_retained(self):
        for stamp in (
            None,
            "bad-time",
            NOW.isoformat(),
            (NOW + dt.timedelta(days=1)).isoformat(),
            NOW.replace(tzinfo=None).isoformat(),
        ):
            self.record["completed_at"] = stamp
            with self.subTest(stamp=stamp):
                self.assertFalse(self.decision()["eligible"])

    def test_timezone_offsets_are_compared_as_instants(self):
        stamp = NOW - dt.timedelta(days=30, seconds=1)
        self.record["completed_at"] = stamp.astimezone(
            dt.timezone(dt.timedelta(hours=8))
        ).isoformat()
        self.assertTrue(self.decision()["eligible"])

    def test_old_format_is_not_blanket_deletion_permission(self):
        self.record = {"legacy": True, "completed_at": "2020-01-01T00:00:00Z"}
        self.assertFalse(self.decision()["eligible"])

    def test_pure_predicate_preserves_record_and_performs_no_deletion(self):
        self.record["private_payload"] = "sensitive-canary"
        before = copy.deepcopy(self.record)
        with patch.object(Path, "unlink", side_effect=AssertionError("no deletion")):
            result = self.decision()
        self.assertEqual(before, self.record)
        self.assertNotIn("sensitive-canary", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
