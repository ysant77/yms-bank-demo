import importlib.util
import os
import threading
from datetime import UTC, datetime

import pytest

from bank import ledger

# Locally the fraud tests skip without the optional extra; in CI a skip would hide them.
if os.environ.get("CI") and importlib.util.find_spec("sklearn") is None:
    raise RuntimeError("CI must install the fraud extra (uv sync --extra fraud)")


@pytest.fixture
def conn(tmp_path):
    connection = ledger.open_db(tmp_path / "bank.db")
    yield connection
    connection.close()


@pytest.fixture
def at():
    """Build a UTC timestamp for a posting date."""

    def make(day: str, time: str = "12:00") -> datetime:
        return datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=UTC)

    return make


@pytest.fixture
def run_together():
    """Run ``work(start)`` in ``n`` threads; each calls ``start()`` to wait for the others.

    A thread that fails breaks the barrier, so the other threads stop instead of waiting
    forever, and a thread that does not finish in time is reported. Returns the errors.
    """

    def run(n: int, work, timeout: float = 60.0) -> list[str]:
        barrier = threading.Barrier(n, timeout=timeout)
        errors: list[str] = []
        lock = threading.Lock()

        def worker():
            try:
                work(barrier.wait)
            except BaseException as exc:  # noqa: BLE001 - returned to the test
                barrier.abort()
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout)
        errors.extend("thread did not finish" for thread in threads if thread.is_alive())
        return errors

    return run


@pytest.fixture(scope="session")
def fraud_run():
    """The fraud score trained once per test session (synthetic data, fixed seed)."""
    from fraud import model

    return model.run()
