"""Concurrent withdrawals from separate processes on one SQLite file.

tests/test_concurrency.py runs its workers as threads of one process. A fix that only takes a lock
inside the process (a mutex) passes that test, but two processes do not share the lock. Here each
withdrawal runs in its own Python process, all started at the same moment, on the same database
file. The ledger must still let exactly one 80.00 withdrawal from 100.00 succeed.

Properties checked: exactly one success; the balance never goes below zero; the balance equals
100.00 minus the successful withdrawals; the journal invariants hold.
"""

import json
import os
import subprocess
import sys
import time

from bank import ledger
from bank.money import Money

PROCESSES = 6

# One withdrawal in its own process: wait for the common start time, withdraw, print the outcome.
CHILD = """
import json, sys, time
from bank import ledger
from bank.money import Money
path, account_id, start = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
conn = ledger.open_db(path)
try:
    time.sleep(max(0.0, start - time.time()))
    try:
        ledger.withdraw(conn, account_id, Money.of("80.00", "EUR"))
        outcome = "ok"
    except ledger.InsufficientFunds:
        outcome = "refused"
    except Exception as exc:
        outcome = "error: " + repr(exc)
finally:
    conn.close()
print(json.dumps(outcome))
"""


def test_concurrent_withdrawals_from_separate_processes_never_overdraw(tmp_path):
    path = tmp_path / "race.db"
    setup = ledger.open_db(path)
    customer = ledger.create_customer(setup, "Process Race Customer")
    account = ledger.open_account(setup, customer, "EUR")
    ledger.deposit(setup, account.id, Money.of("100.00", "EUR"))

    # Every process waits until the same wall-clock moment, after all of them have started.
    start = time.time() + 2.0
    procs = [
        subprocess.Popen(  # noqa: S603 the running Python with a fixed argument list, no shell
            [sys.executable, "-c", CHILD, str(path), str(account.id), repr(start)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        for _ in range(PROCESSES)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))

    successes = results.count("ok")
    final = ledger.get_account(setup, account.id).balance_minor
    assert results.count("refused") + successes == PROCESSES, results
    # 100.00 covers one 80.00 withdrawal: exactly one must succeed, not zero and not more.
    assert successes == 1, results
    assert final >= 0
    assert final == 10000 - 8000 * successes
    assert ledger.check_invariants(setup) == []
