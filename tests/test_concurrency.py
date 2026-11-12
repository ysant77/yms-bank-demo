import threading

from bank import ledger
from bank.money import Money

THREADS = 20


def test_concurrent_withdrawals_never_overdraw(tmp_path, run_together):
    """20 threads withdraw 80.00 from 100.00 at once: at most one may succeed."""
    path = tmp_path / "race.db"
    setup = ledger.open_db(path)
    customer = ledger.create_customer(setup, "Race Test Customer")
    account = ledger.open_account(setup, customer, "EUR")
    ledger.deposit(setup, account.id, Money.of("100.00", "EUR"))

    results: list[str] = []
    lock = threading.Lock()

    def worker(start):
        conn = ledger.open_db(path)
        try:
            start()
            try:
                ledger.withdraw(conn, account.id, Money.of("80.00", "EUR"))
                outcome = "ok"
            except ledger.InsufficientFunds:
                outcome = "refused"
            except Exception as exc:  # any other failure must surface in the assertion
                outcome = f"error: {exc!r}"
        finally:
            conn.close()
        with lock:
            results.append(outcome)

    assert run_together(THREADS, worker) == []
    successes = results.count("ok")
    final = ledger.get_account(setup, account.id).balance_minor
    assert len(results) == THREADS
    assert results.count("refused") + successes == THREADS, results
    assert successes <= 1
    assert final >= 0
    assert final == 10000 - 8000 * successes
    assert ledger.check_invariants(setup) == []
