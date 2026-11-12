"""Startup seeding: one worker seeds; a failed seed is retried on the next start."""

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


def test_two_workers_starting_together_seed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(api.seed_data, "build", small_build)
    path = tmp_path / "demo.db"
    ledger.open_db(path).close()
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            barrier.wait()
            start(path)
        except Exception as exc:  # noqa: BLE001 - reported by the assertion
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert customer_count(path) == 4


def test_a_failed_seed_is_retried_on_the_next_start(tmp_path, monkeypatch):
    path = tmp_path / "demo.db"
    ledger.open_db(path).close()

    def broken(conn, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.seed_data, "build", broken)
    with pytest.raises(RuntimeError):
        start(path)
    assert customer_count(path) == 0

    monkeypatch.setattr(api.seed_data, "build", small_build)
    start(path)
    assert customer_count(path) == 4
