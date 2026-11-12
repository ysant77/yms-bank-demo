from datetime import UTC, datetime

import pytest

from bank import ledger


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
