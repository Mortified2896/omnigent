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


def test_isolated_runner_can_probe_an_explicit_account_home(monkeypatch, tmp_path):
    import asyncio
    import json
    from pathlib import Path

    from omnigent.harnesses.codex_native import account_limits

    runner_home = tmp_path / "runner"
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text(json.dumps({"private": "test-credential"}))
    monkeypatch.setenv("CODEX_HOME", str(runner_home))
    monkeypatch.setenv("OMNIGENT_CODEX_ACCOUNT_HOME", str(account_home))
    monkeypatch.setattr(account_limits, "_cache", None)
    monkeypatch.setattr(account_limits, "_lock", asyncio.Lock())
    monkeypatch.setattr(account_limits, "_find_codex_cli", lambda: "/test/codex")

    async def start(**kwargs):
        probe_home = Path(kwargs["env"]["CODEX_HOME"])
        assert probe_home != account_home and probe_home != runner_home
        assert json.loads((probe_home / "auth.json").read_text()) == {"private": "test-credential"}
        assert (probe_home / "auth.json").stat().st_mode & 0o777 == 0o600
        return object()

    async def noop(*_args):
        pass

    class Client:
        def __init__(self, **_kwargs):
            pass

        connect = close = noop

        async def request(self, method, params):
            assert method == "account/rateLimits/read"
            return {"result": {"rateLimits": {"primary": {"usedPercent": 4}}}}

    monkeypatch.setattr(account_limits, "_start_codex_model_discovery_process", start)
    monkeypatch.setattr(account_limits, "_wait_for_discovery_listener", noop)
    monkeypatch.setattr(account_limits, "_stop_codex_model_discovery_process", noop)
    monkeypatch.setattr(account_limits, "CodexAppServerClient", Client)
    assert asyncio.run(account_limits.read_account_limits())["remaining_percent"] == 96
    assert not runner_home.exists()
    assert json.loads((account_home / "auth.json").read_text()) == {"private": "test-credential"}
