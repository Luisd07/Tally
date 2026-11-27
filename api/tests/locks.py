import time

from sqlalchemy import Engine, text


def wait_until_blocked_on_lock(engine: Engine, pid: int) -> None:
    """Return once the Postgres backend `pid` is waiting on a lock held by another session."""
    deadline = time.monotonic() + 10
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            wait = conn.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
            if wait == "Lock":
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError(f"backend {pid} never blocked on a lock")
