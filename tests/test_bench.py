import time

import pytest

from bank import ledger
from bank.money import Money

TRANSFERS = 200
RUNS = 20


@pytest.mark.bench
def test_transfer_throughput(tmp_path, record_samples):
    conn = ledger.open_db(tmp_path / "bench.db")
    payer = ledger.open_account(conn, ledger.create_customer(conn, "Bench Payer"), "EUR")
    payee = ledger.open_account(conn, ledger.create_customer(conn, "Bench Payee"), "EUR")
    ledger.deposit(conn, payer.id, Money.of("100000.00", "EUR"))
    one = Money.of("1.00", "EUR")

    # 20 short rounds: the report carries their median, quartiles, minimum and maximum. This is
    # the jitter inside one process; it says nothing about the difference between two CI jobs.
    # One warm-up round, not measured: the first round pays for caches and page allocation.
    for _ in range(TRANSFERS):
        ledger.transfer(conn, payer.id, payee.id, one)
    samples = []
    for _ in range(RUNS):
        start = time.perf_counter()
        for _ in range(TRANSFERS):
            ledger.transfer(conn, payer.id, payee.id, one)
        samples.append(round(TRANSFERS / (time.perf_counter() - start), 1))

    record_samples("transfers_per_s", samples)
    assert ledger.get_account(conn, payee.id).balance_minor == (RUNS + 1) * TRANSFERS * 100
    assert ledger.check_invariants(conn) == []
