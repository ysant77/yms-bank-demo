"""Startup seeding: one worker seeds; a failed seed is retried on the next start."""

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
    monkeypatch.setattr(api.seed_data, "build", small_build)
    path = tmp_path / "demo.db"
    ledger.open_db(path).close()

    def worker(begin):
        begin()
        start(path)

    assert run_together(2, worker) == []
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
