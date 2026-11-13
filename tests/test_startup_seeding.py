"""Startup seeding: one worker seeds, atomically; a failed seed leaves nothing and is retried."""

import threading

import pytest
from fastapi.testclient import TestClient

from bank import api, ledger, seed_data

REAL_BUILD = seed_data.build


def small_build(conn, **kwargs):
    return REAL_BUILD(conn, customers=4, accounts=8, days=2)


def customer_count(path):
    conn = ledger.open_db(path)
    try:
        return len(ledger.list_customers(conn))
    finally:
        conn.close()


def start(path):
    with TestClient(api.create_app(path)):
        pass


def test_two_workers_starting_together_seed_once(tmp_path, monkeypatch, run_together):
    """The first seed blocks until the second worker has entered the seeding step."""
    path = tmp_path / "demo.db"
    ledger.open_db(path).close()
    entered = []
    second_entered = threading.Event()
    lock = threading.Lock()
    real_run_once = ledger.run_once

    def counting_run_once(conn, name, job):
        with lock:
            entered.append(name)
            if len(entered) == 2:
                second_entered.set()
        return real_run_once(conn, name, job)

    builds = []

    def blocking_build(conn, **kwargs):
        builds.append(threading.get_ident())
        # The other worker is now inside run_once: it waits for the lock, then must skip.
        assert second_entered.wait(30)
        return small_build(conn)

    monkeypatch.setattr(api.ledger, "run_once", counting_run_once)
    monkeypatch.setattr(api.seed_data, "build", blocking_build)

    def worker(begin):
        begin()
        start(path)

    assert run_together(2, worker) == []
    assert entered == [api.SEED_FLAG, api.SEED_FLAG]
    assert len(builds) == 1
    assert customer_count(path) == 4


def test_a_seed_that_fails_part_way_leaves_nothing_and_is_retried(tmp_path, monkeypatch):
    path = tmp_path / "demo.db"
    ledger.open_db(path).close()

    def half_build(conn, **kwargs):
        ledger.create_customer(conn, "Partial Customer")
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.seed_data, "build", half_build)
    with pytest.raises(RuntimeError, match="disk full"):
        start(path)
    assert customer_count(path) == 0

    monkeypatch.setattr(api.seed_data, "build", small_build)
    start(path)
    assert customer_count(path) == 4
    start(path)  # seeded once: a later start changes nothing
    assert customer_count(path) == 4


def test_a_database_with_customers_is_not_seeded(tmp_path, monkeypatch):
    path = tmp_path / "demo.db"
    conn = ledger.open_db(path)
    ledger.create_customer(conn, "Existing Customer")
    conn.close()
    monkeypatch.setattr(api.seed_data, "build", small_build)
    start(path)
    assert customer_count(path) == 1
