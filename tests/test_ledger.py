from datetime import date

import pytest

from bank import ledger
from bank.money import Money


def eur(amount):
    return Money.of(amount, "EUR")


@pytest.fixture
def two_accounts(conn):
    alice = ledger.create_customer(conn, "Test Customer One")
    bob = ledger.create_customer(conn, "Test Customer Two")
    a = ledger.open_account(conn, alice, "EUR")
    b = ledger.open_account(conn, bob, "EUR")
    ledger.deposit(conn, a.id, eur("100.00"))
    return a, b


def balance(conn, account):
    return ledger.get_account(conn, account.id).balance_minor


def test_account_numbers_are_obviously_fake(conn):
    customer = ledger.create_customer(conn, "Test Customer")
    first = ledger.open_account(conn, customer, "EUR")
    second = ledger.open_account(conn, customer, "JPY")
    assert (first.number, second.number) == ("YMS-0000-0001", "YMS-0000-0002")
    assert ledger.account_number(12345) == "YMS-0001-2345"


def test_deposit_and_withdraw_keep_books_balanced(conn, two_accounts):
    a, _ = two_accounts
    ledger.withdraw(conn, a.id, eur("30.25"))
    assert balance(conn, a) == 6975
    assert ledger.check_invariants(conn) == []
    clearing = ledger.get_account_by_number(conn, "YMS-CLR-EUR")
    assert clearing.balance_minor == -6975


def test_transfer_moves_money(conn, two_accounts):
    a, b = two_accounts
    ledger.transfer(conn, a.id, b.id, eur("40.00"), reference="Rent")
    assert (balance(conn, a), balance(conn, b)) == (6000, 4000)
    assert ledger.check_invariants(conn) == []


def test_overdraw_is_refused_and_nothing_is_written(conn, two_accounts):
    a, b = two_accounts
    entries_before = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    with pytest.raises(ledger.InsufficientFunds):
        ledger.transfer(conn, a.id, b.id, eur("100.01"))
    with pytest.raises(ledger.InsufficientFunds):
        ledger.withdraw(conn, a.id, eur("100.01"))
    assert (balance(conn, a), balance(conn, b)) == (10000, 0)
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == entries_before
    assert not conn.in_transaction


def test_withdraw_whole_balance_is_allowed(conn, two_accounts):
    a, _ = two_accounts
    ledger.withdraw(conn, a.id, eur("100.00"))
    assert balance(conn, a) == 0


def test_currency_mismatch_and_bad_amounts(conn, two_accounts):
    a, b = two_accounts
    other = ledger.open_account(conn, ledger.create_customer(conn, "Test Yen"), "JPY")
    with pytest.raises(ledger.LedgerError):
        ledger.transfer(conn, a.id, other.id, eur("1.00"))
    with pytest.raises(ledger.LedgerError):
        ledger.deposit(conn, a.id, Money.of("100", "JPY"))
    with pytest.raises(ledger.LedgerError):
        ledger.deposit(conn, a.id, eur("0"))
    with pytest.raises(ledger.LedgerError):
        ledger.withdraw(conn, a.id, eur("-1.00"))
    with pytest.raises(ledger.LedgerError):
        ledger.transfer(conn, a.id, a.id, eur("1.00"))
    with pytest.raises(ledger.NotFound):
        ledger.deposit(conn, 9999, eur("1.00"))


def test_clearing_account_cannot_be_used_directly(conn, two_accounts):
    a, _ = two_accounts
    clearing = ledger.get_account_by_number(conn, "YMS-CLR-EUR")
    with pytest.raises(ledger.NotFound):
        ledger.transfer(conn, clearing.id, a.id, eur("1.00"))


def test_database_refuses_negative_customer_balance(conn, two_accounts):
    a, _ = two_accounts
    with pytest.raises(Exception, match="CHECK constraint failed"):
        conn.execute("UPDATE accounts SET balance_minor = -1 WHERE id = ?", (a.id,))


def test_invariant_check_finds_tampering(conn, two_accounts):
    a, _ = two_accounts
    conn.execute("UPDATE accounts SET balance_minor = balance_minor + 1 WHERE id = ?", (a.id,))
    conn.execute("UPDATE journal_lines SET amount_minor = 5 WHERE id = 1")
    problems = ledger.check_invariants(conn)
    assert any("stores" in p for p in problems)
    assert any("is off by" in p for p in problems)


def test_statement_running_balance(conn, two_accounts, at):
    a, b = two_accounts
    ledger.deposit(conn, b.id, eur("10.00"), at=at("2026-03-01"))
    ledger.transfer(conn, a.id, b.id, eur("5.00"), at=at("2026-03-02"))
    ledger.withdraw(conn, b.id, eur("2.50"), at=at("2026-03-03"))
    ledger.deposit(conn, b.id, eur("1.00"), at=at("2026-03-05"))
    st = ledger.statement(conn, b.id, date(2026, 3, 2), date(2026, 3, 4))
    assert st.opening_minor == 1000
    assert [line.amount_minor for line in st.lines] == [500, -250]
    assert [line.balance_minor for line in st.lines] == [1500, 1250]
    assert st.closing_minor == 1250


def test_statement_end_date_is_inclusive(conn, two_accounts, at):
    _, b = two_accounts
    ledger.deposit(conn, b.id, eur("1.00"), at=at("2026-04-30", "00:00:00"))
    ledger.deposit(conn, b.id, eur("2.00"), at=at("2026-04-30", "23:59:59"))
    ledger.deposit(conn, b.id, eur("4.00"), at=at("2026-05-01", "00:00:00"))
    st = ledger.statement(conn, b.id, date(2026, 4, 30), date(2026, 4, 30))
    assert [line.amount_minor for line in st.lines] == [100, 200]
    assert st.closing_minor == 300


def test_statement_rejects_reversed_range(conn, two_accounts):
    a, _ = two_accounts
    with pytest.raises(ledger.LedgerError):
        ledger.statement(conn, a.id, date(2026, 5, 2), date(2026, 5, 1))


def test_run_once_runs_a_job_once(conn):
    runs = []
    assert ledger.run_once(conn, "job", lambda c: runs.append(1)) is True
    assert ledger.run_once(conn, "job", lambda c: runs.append(1)) is False
    assert runs == [1]


def test_a_failed_job_leaves_nothing_and_runs_again(conn):
    def half_done(c):
        ledger.create_customer(c, "Written Before The Failure")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        ledger.run_once(conn, "job", half_done)
    assert ledger.list_customers(conn) == []
    assert not conn.in_transaction
    assert ledger.run_once(conn, "job", lambda c: ledger.create_customer(c, "Second Try")) is True
    assert [c["name"] for c in ledger.list_customers(conn)] == ["Second Try"]


def test_a_nested_write_failure_undoes_only_its_block(conn):
    from bank.db import write_txn

    with write_txn(conn):
        ledger.create_customer(conn, "Kept")
        with pytest.raises(ledger.LedgerError):
            with write_txn(conn):
                ledger.create_customer(conn, "Undone")
                raise ledger.LedgerError("inner failure")
    assert [c["name"] for c in ledger.list_customers(conn)] == ["Kept"]
