"""Properties of the ledger, checked on many generated amounts (Hypothesis).

- A transfer keeps the total of the two accounts. When it goes through, the source loses the
  amount and the target gains it. When it is refused, no balance changes.
- A refused withdrawal changes no balance and writes no journal entry.

The example tests in test_ledger.py check named amounts. These tests check the rule for any
amount, so a change of a test's numbers cannot hide a broken rule. The runs are derandomized and
keep no example database, so CI gives the same result each time.
"""

from contextlib import closing

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bank import ledger
from bank.money import Money

PROPERTY = settings(max_examples=150, deadline=None, derandomize=True, database=None)
MINOR = st.integers(min_value=1, max_value=10**12)


def balances(conn):
    return dict(conn.execute("SELECT id, balance_minor FROM accounts").fetchall())


def entries(conn):
    return conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]


def two_accounts(conn, start_a, start_b, currency="EUR"):
    a = ledger.open_account(conn, ledger.create_customer(conn, "Test Customer One"), currency)
    b = ledger.open_account(conn, ledger.create_customer(conn, "Test Customer Two"), currency)
    for account, start in ((a, start_a), (b, start_b)):
        if start:
            ledger.deposit(conn, account.id, Money(start, currency))
    return a, b


@PROPERTY
@given(start_a=st.integers(0, 10**12), start_b=st.integers(0, 10**12), amount=MINOR)
def test_a_transfer_keeps_the_total_of_the_two_accounts(start_a, start_b, amount):
    with closing(ledger.open_db(":memory:")) as conn:
        a, b = two_accounts(conn, start_a, start_b)
        before = balances(conn)
        try:
            ledger.transfer(conn, a.id, b.id, Money(amount, "EUR"))
            moved = amount
        except ledger.InsufficientFunds:
            assert amount > start_a
            moved = 0
        after = balances(conn)
        assert after[a.id] + after[b.id] == before[a.id] + before[b.id]
        assert after[a.id] == before[a.id] - moved
        assert after[b.id] == before[b.id] + moved
        assert after[a.id] >= 0
        assert ledger.check_invariants(conn) == []


@PROPERTY
@given(start=st.integers(0, 10**12), extra=MINOR)
def test_a_refused_withdrawal_changes_no_balance(start, extra):
    with closing(ledger.open_db(":memory:")) as conn:
        a, _ = two_accounts(conn, start, 0)
        before, written = balances(conn), entries(conn)
        with pytest.raises(ledger.InsufficientFunds):
            ledger.withdraw(conn, a.id, Money(start + extra, "EUR"))
        assert balances(conn) == before
        assert entries(conn) == written
        assert ledger.check_invariants(conn) == []


@PROPERTY
@given(start=st.integers(1, 10**12), data=st.data())
def test_a_withdrawal_within_the_balance_goes_through(start, data):
    """The control of the test above: an amount the account holds is not refused."""
    amount = data.draw(st.integers(1, start))
    with closing(ledger.open_db(":memory:")) as conn:
        a, _ = two_accounts(conn, start, 0)
        ledger.withdraw(conn, a.id, Money(amount, "EUR"))
        assert balances(conn)[a.id] == start - amount
        assert ledger.check_invariants(conn) == []
