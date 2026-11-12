"""Acceptance tests for BANK-4: concurrent withdrawals."""

import threading

from bank import ledger
from bank.money import Money

THREADS = 20


def _race(path):
    setup = ledger.open_db(path)
    customer = ledger.create_customer(setup, "Race Customer")
    account = ledger.open_account(setup, customer, "EUR")
    ledger.deposit(setup, account.id, Money.of("100.00", "EUR"))
    barrier = threading.Barrier(THREADS)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        conn = ledger.open_db(path)
        try:
            barrier.wait()
            try:
                ledger.withdraw(conn, account.id, Money.of("80.00", "EUR"))
                outcome = "ok"
            except ledger.InsufficientFunds:
                outcome = "refused"
            except Exception as exc:  # noqa: BLE001 - every other outcome is a failure
                outcome = f"error: {exc!r}"
        finally:
            conn.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return setup, account, outcomes


def test_BANK4_concurrent_withdrawals_20_threads(tmp_path):
    for run in range(5):
        _, _, outcomes = _race(tmp_path / f"race{run}.db")
        assert outcomes.count("ok") <= 1, outcomes
        assert all(o in ("ok", "refused") for o in outcomes), outcomes


def test_BANK4_balance_never_negative(tmp_path):
    for run in range(5):
        setup, account, _ = _race(tmp_path / f"neg{run}.db")
        assert ledger.get_account(setup, account.id).balance_minor >= 0
        assert ledger.check_invariants(setup) == []
