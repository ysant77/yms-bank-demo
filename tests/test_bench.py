import time

import pytest

from bank import ledger
from bank.money import Money

TRANSFERS = 1000


@pytest.mark.bench
def test_transfer_throughput(tmp_path, record_metric):
    conn = ledger.open_db(tmp_path / "bench.db")
    payer = ledger.open_account(conn, ledger.create_customer(conn, "Bench Payer"), "EUR")
    payee = ledger.open_account(conn, ledger.create_customer(conn, "Bench Payee"), "EUR")
    ledger.deposit(conn, payer.id, Money.of("100000.00", "EUR"))
    one = Money.of("1.00", "EUR")

    start = time.perf_counter()
    for _ in range(TRANSFERS):
        ledger.transfer(conn, payer.id, payee.id, one)
    elapsed = time.perf_counter() - start

    record_metric("transfers_per_s", round(TRANSFERS / elapsed, 1))
    assert ledger.get_account(conn, payee.id).balance_minor == TRANSFERS * 100
    assert ledger.check_invariants(conn) == []
