from omnigent.server.audio_generation_estimate import estimate_generation


def test_model_and_length_drive_cold_estimates_and_queue_has_no_elapsed_time():
    short = estimate_generation(
        model="eleven_v4", characters=4000, started_at=None, progress={}, samples=[], now=100
    )
    long = estimate_generation(
        model="eleven_v4", characters=8000, started_at=None, progress={}, samples=[], now=100
    )
    local = estimate_generation(
        model="kokoro", characters=4000, started_at=None, progress={}, samples=[], now=100
    )
    assert (
        long["remaining_seconds_high"]
        > short["remaining_seconds_high"]
        > local["remaining_seconds_high"]
    )
    assert short["elapsed_seconds"] == 0
    assert short["estimate_basis"] == "initial estimate"


def test_completed_chunks_override_historical_speed_and_account_for_current_wait():
    result = estimate_generation(
        model="eleven_v4",
        characters=8000,
        started_at=100,
        progress={
            "completed_characters": 2000,
            "completed_at": 150,
            "completed_chunks": 1,
            "total_chunks": 4,
        },
        samples=[0.1],
        now=180,
    )
    assert result["estimate_basis"] == "this generation"
    assert result["elapsed_seconds"] == 80
    assert result["completed_chunks"] == 1
    assert result["remaining_seconds_high"] == 249


def test_overdue_estimate_never_promises_zero_seconds():
    result = estimate_generation(
        model="eleven_v4", characters=4000, started_at=100, progress={}, samples=[0.025], now=400
    )
    assert result["taking_longer"]
    assert result["remaining_seconds_low"] is None
    assert result["remaining_seconds_high"] is None
    assert result["estimate_basis"] == "recent generations"
