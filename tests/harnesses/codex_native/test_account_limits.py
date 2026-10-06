from omnigent.harnesses.codex_native.account_limits import subscription_windows


def test_subscription_usage_uses_the_tightest_window_and_omits_private_fields():
    result = subscription_windows(
        {
            "email": "private@example.test",
            "accessToken": "private-token",
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 28, "windowDurationMins": 300, "resetsAt": 100},
                    "secondary": {"usedPercent": 65, "windowDurationMins": 10080, "resetsAt": 200},
                }
            },
        }
    )
    assert result == {
        "remaining_percent": 35,
        "windows": [
            {"name": "primary", "remaining_percent": 72, "window_minutes": 300, "resets_at": 100},
            {
                "name": "secondary",
                "remaining_percent": 35,
                "window_minutes": 10080,
                "resets_at": 200,
            },
        ],
    }


def test_missing_or_invalid_subscription_usage_is_unknown_not_a_demo_percentage():
    assert subscription_windows({}) == {"remaining_percent": None, "windows": []}
    assert subscription_windows(
        {"rateLimits": {"primary": {"usedPercent": True}, "secondary": {"usedPercent": 101}}}
    ) == {
        "remaining_percent": None,
        "windows": [],
    }
