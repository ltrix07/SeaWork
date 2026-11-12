from datetime import UTC, datetime

import pytest

from seawork.config import Settings
from seawork.schedule import next_run, run_daily, run_forever


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def test_next_run_before_the_hour_is_today() -> None:
    assert next_run(at(12, 5, 30), 7) == at(12, 7)


def test_next_run_exactly_at_the_hour_is_tomorrow() -> None:
    assert next_run(at(12, 7), 7) == at(13, 7)


def test_next_run_after_the_hour_is_tomorrow() -> None:
    assert next_run(at(12, 9), 7) == at(13, 7)


def test_next_run_crosses_month_boundary() -> None:
    assert next_run(at(31, 23, 59), 0) == datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def test_run_daily_ingests_every_source_before_delivery() -> None:
    calls: list[str] = []

    def ingest(source_id: str) -> bool:
        calls.append(source_id)
        return True

    def deliver() -> bool:
        calls.append("digest")
        return True

    assert run_daily(["a", "b"], ingest, deliver) == 0
    assert calls == ["a", "b", "digest"]


def test_failed_source_does_not_stop_the_rest_or_delivery() -> None:
    calls: list[str] = []

    def ingest(source_id: str) -> bool:
        calls.append(source_id)
        if source_id == "a":
            raise RuntimeError("boom")
        return True

    def deliver() -> bool:
        calls.append("digest")
        return True

    assert run_daily(["a", "b"], ingest, deliver) == 1
    assert calls == ["a", "b", "digest"]


def test_refused_closing_fails_the_run_but_not_the_rest() -> None:
    calls: list[str] = []

    def ingest(source_id: str) -> bool:
        calls.append(source_id)
        return source_id != "a"

    def deliver() -> bool:
        calls.append("digest")
        return True

    assert run_daily(["a", "b"], ingest, deliver) == 1
    assert calls == ["a", "b", "digest"]


def test_delivery_failures_fail_the_run() -> None:
    assert run_daily(["a"], lambda _: True, lambda: False) == 1


def test_crashed_delivery_fails_the_run() -> None:
    def deliver() -> bool:
        raise RuntimeError("boom")

    assert run_daily(["a"], lambda _: True, deliver) == 1


class Stop(Exception):
    pass


def test_run_forever_sleeps_until_the_hour_then_runs_again_next_day() -> None:
    clock = [at(12, 5)]
    slept: list[float] = []
    runs: list[int] = []

    def sleep(seconds: float) -> None:
        if len(slept) == 2:
            raise Stop
        slept.append(seconds)
        clock[0] = clock[0].fromtimestamp(clock[0].timestamp() + seconds, UTC)

    def run() -> int:
        runs.append(1)
        return 1

    with pytest.raises(Stop):
        run_forever(7, run, lambda: clock[0], sleep)
    assert slept == [2 * 3600, 24 * 3600]
    assert len(runs) == 2


def test_digest_hour_is_validated() -> None:
    assert Settings(digest_hour_utc=23).digest_hour_utc == 23
    with pytest.raises(ValueError):
        Settings(digest_hour_utc=24)


def test_run_forever_announces_the_next_run(capsys: pytest.CaptureFixture[str]) -> None:
    def sleep(seconds: float) -> None:
        raise Stop

    with pytest.raises(Stop):
        run_forever(7, lambda: 0, lambda: at(12, 5), sleep)
    assert "следующий запуск: 2026-10-12 07:00 UTC" in capsys.readouterr().out


def test_run_forever_does_not_swallow_a_crashing_run() -> None:
    def run() -> int:
        raise RuntimeError("broken registry")

    with pytest.raises(RuntimeError):
        run_forever(7, run, lambda: at(12, 5), lambda _: None)


def test_run_daily_prints_the_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    def ingest(source_id: str) -> bool:
        raise RuntimeError("boom")

    run_daily(["a"], ingest, lambda: True)
    assert "Traceback" in capsys.readouterr().err
