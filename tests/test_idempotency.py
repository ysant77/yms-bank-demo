"""transfer_once: one transfer per (customer, key), stored in the transfer's transaction."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from bank import ledger
from bank.money import Money


@pytest.fixture
def accounts(tmp_path):
    path = tmp_path / "idem.db"
    conn = ledger.open_db(path)
    owner = ledger.create_customer(conn, "Owner")
    source = ledger.open_account(conn, owner, "EUR")
    target = ledger.open_account(conn, owner, "EUR")
    ledger.deposit(conn, source.id, Money.of("100.00", "EUR"))
    return path, conn, owner, source, target


def entries(conn):
    return conn.execute("SELECT COUNT(*) FROM journal_entries WHERE kind = 'transfer'").fetchone()[
        0
    ]


def test_concurrent_requests_with_one_key_move_money_once(accounts):
    path, conn, owner, source, target = accounts
    barrier = threading.Barrier(8)
    outcomes = []
    lock = threading.Lock()

    def worker():
        c = ledger.open_db(path)
        try:
            barrier.wait()
            result, replayed = ledger.transfer_once(
                c, owner, "same-key", source.id, target.id, Money.of("10.00", "EUR")
            )
            with lock:
                outcomes.append((result["entry_id"], replayed))
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 8
    assert len({entry for entry, _ in outcomes}) == 1
    assert [replayed for _, replayed in outcomes].count(False) == 1
    assert entries(conn) == 1
    assert ledger.get_account(conn, source.id).balance_minor == 9000


def test_the_key_is_stored_in_the_transfer_transaction(accounts):
    _, conn, owner, source, target = accounts
    # If storing the key fails, the transfer must roll back with it.
    conn.execute(
        "CREATE TRIGGER refuse_keys BEFORE INSERT ON idempotency_keys"
        " BEGIN SELECT RAISE(ABORT, 'key store down'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.transfer_once(conn, owner, "k", source.id, target.id, Money.of("10.00", "EUR"))
    assert entries(conn) == 0
    assert ledger.get_account(conn, source.id).balance_minor == 10000
    assert ledger.check_invariants(conn) == []


def test_a_failed_attempt_stores_no_key(accounts):
    _, conn, owner, source, target = accounts
    with pytest.raises(ledger.InsufficientFunds):
        ledger.transfer_once(conn, owner, "k", source.id, target.id, Money.of("500.00", "EUR"))
    result, replayed = ledger.transfer_once(
        conn, owner, "k", source.id, target.id, Money.of("10.00", "EUR")
    )
    assert replayed is False and entries(conn) == 1


def test_keys_expire_after_24_hours(accounts):
    _, conn, owner, source, target = accounts
    start = datetime.now(UTC)
    ledger.transfer_once(conn, owner, "k", source.id, target.id, Money.of("1.00", "EUR"), now=start)
    later = start + timedelta(hours=25)
    _, replayed = ledger.transfer_once(
        conn, owner, "k", source.id, target.id, Money.of("2.00", "EUR"), now=later
    )
    assert replayed is False and entries(conn) == 2
