import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta


def next_run(now: datetime, hour: int) -> datetime:
    today = now.astimezone(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return today if today > now else today + timedelta(days=1)


def run_daily(
    source_ids: list[str],
    ingest: Callable[[str], bool],
    deliver: Callable[[], bool],
) -> int:
    failed = False
    for source_id in source_ids:
        try:
            failed |= not ingest(source_id)
        except Exception:
            print(f"Источник {source_id} упал", file=sys.stderr)
            traceback.print_exc()
            failed = True
    try:
        failed |= not deliver()
    except Exception:
        print("Рассылка упала", file=sys.stderr)
        traceback.print_exc()
        failed = True
    return 1 if failed else 0


def run_forever(
    hour: int,
    run: Callable[[], int],
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> None:
    while True:
        current = now()
        target = next_run(current, hour)
        print(f"следующий запуск: {target:%Y-%m-%d %H:%M} UTC", flush=True)
        sleep((target - current).total_seconds())
        run()
