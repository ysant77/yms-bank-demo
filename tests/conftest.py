import importlib.util
import os
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


@pytest.fixture(scope="session")
def fraud_run():
    """The fraud score trained once per test session (synthetic data, fixed seed)."""
    from fraud import model

    return model.run()
