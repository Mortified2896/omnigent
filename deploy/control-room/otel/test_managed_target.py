import datetime as dt
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "managed_budget", Path(__file__).with_name("managed_budget.py")
)
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)
NOW = dt.datetime(2026, 9, 11, 8, tzinfo=dt.timezone.utc)


def snapshot(total=0, *, complete=True, reserved=0):
    names = sorted(budget.COMPONENTS)
    parts = {name: {"bytes": 0, "protected_bytes": 0} for name in names}
    parts[names[0]]["bytes"] = total
    return {
        "complete": complete,
        "observed_at": NOW.isoformat(),
        "reserved_bytes": reserved,
        "components": parts,
    }


class ManagedTargetTests(unittest.TestCase):
    def setUp(self):
        self.policy = budget.load_policy()

    def assess(self, total, growth=0, paused=False, **kw):
        return budget.assess_budget(
            snapshot(total, **kw),
            policy=self.policy,
            now=NOW,
            max_age_seconds=300,
            requested_growth_bytes=growth,
            optional_paused=paused,
        )

    def test_policy_thresholds(self):
        self.assertEqual(self.policy["total_max_bytes"], 50_000_000_000)
        self.assertEqual(self.policy["management"]["cleanup_start_bytes"], 45_000_000_000)
        self.assertEqual(self.policy["management"]["resume_optional_bytes"], 46_000_000_000)
        self.assertEqual(self.policy["management"]["pause_optional_bytes"], 48_000_000_000)

    def test_normal(self):
        result = self.assess(44_999_999_999)
        self.assertEqual(result["management_state"], "NORMAL")
        self.assertTrue(result["optional_telemetry_allowed"])
        self.assertFalse(result["cleanup_requested"])
        self.assertTrue(result["normal_codex_allowed"])

    def test_cleanup_due(self):
        result = self.assess(45_000_000_000)
        self.assertEqual(result["management_state"], "CLEANUP_DUE")
        self.assertTrue(result["cleanup_requested"])
        self.assertTrue(result["optional_telemetry_allowed"])

    def test_pause_optional(self):
        result = self.assess(48_000_000_000)
        self.assertEqual(result["management_state"], "OPTIONAL_PAUSED")
        self.assertFalse(result["optional_telemetry_allowed"])
        self.assertTrue(result["normal_codex_allowed"])

    def test_hysteresis(self):
        self.assertEqual(
            self.assess(46_000_000_001, paused=True)["management_state"],
            "OPTIONAL_PAUSED",
        )
        self.assertEqual(
            self.assess(46_000_000_000, paused=True)["management_state"],
            "CLEANUP_DUE",
        )

    def test_over_target_does_not_stop_codex(self):
        result = self.assess(50_000_000_001)
        self.assertEqual(result["management_state"], "OVER_TARGET")
        self.assertEqual(result["status"], "OVER_BUDGET")
        self.assertFalse(result["optional_telemetry_allowed"])
        self.assertTrue(result["normal_codex_allowed"])
        self.assertFalse(result["hard_ceiling_enforced"])

    def test_projected_optional_write_can_trigger_pause(self):
        result = self.assess(47_900_000_000, growth=100_000_000)
        self.assertEqual(result["projected_bytes"], 48_000_000_000)
        self.assertEqual(result["management_state"], "OPTIONAL_PAUSED")

    def test_incomplete_inventory_pauses_only_optional_telemetry(self):
        result = budget.assess_budget(
            snapshot(0, complete=False),
            policy=self.policy,
            now=NOW,
            max_age_seconds=300,
        )
        self.assertEqual(result["management_state"], "INCOMPLETE")
        self.assertFalse(result["optional_telemetry_allowed"])
        self.assertTrue(result["normal_codex_allowed"])

    def test_reservations_are_pressure(self):
        result = self.assess(47_500_000_000, reserved=500_000_000)
        self.assertEqual(result["management_state"], "OPTIONAL_PAUSED")

    def test_invalid_threshold_order(self):
        self.policy["management"]["pause_optional_bytes"] = self.policy["total_max_bytes"]
        with self.assertRaises(ValueError):
            budget.validate_policy(self.policy)

    def test_metadata_policy_unchanged(self):
        record = {
            "completed": True,
            "completed_at": (NOW - dt.timedelta(days=31)).isoformat(),
            "frozen": False,
            "in_flight": False,
            "needed_for_retained_evidence": False,
            "needed_for_rollback": False,
            "captures_pruned": True,
        }
        self.assertTrue(
            budget.metadata_cleanup_decision(record, policy=self.policy, now=NOW)["eligible"]
        )


if __name__ == "__main__":
    unittest.main()
