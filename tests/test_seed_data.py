import re
from datetime import date

import pytest

from bank import ledger, seed_data


def dump(conn):
    customers = conn.execute("SELECT id, name FROM customers ORDER BY id").fetchall()
    lines = conn.execute(
        "SELECT e.posted_at, l.account_id, l.amount_minor FROM journal_lines l"
        " JOIN journal_entries e ON e.id = l.entry_id ORDER BY l.id"
    ).fetchall()
    return [tuple(r) for r in customers], [tuple(r) for r in lines]


def small(path):
    conn = ledger.open_db(path)
    seed_data.build(conn, customers=5, accounts=8, days=3, end=date(2026, 1, 31))
    return conn


def test_same_seed_gives_same_data(tmp_path):
    assert dump(small(tmp_path / "a.db")) == dump(small(tmp_path / "b.db"))


def test_cli_builds_full_demo_database(tmp_path, capsys):
    path = tmp_path / "demo.db"
    assert seed_data.main(["--db", str(path), "--end-date", "2026-06-30"]) == 0
    assert "50 customers, 100 accounts" in capsys.readouterr().out
    conn = ledger.open_db(path)
    assert ledger.check_invariants(conn) == []
    currencies = dict(conn.execute("SELECT currency, COUNT(*) FROM accounts GROUP BY currency"))
    assert set(currencies) == {"EUR", "USD", "SGD", "JPY"}
    numbers = [r[0] for r in conn.execute("SELECT number FROM accounts WHERE kind = 'customer'")]
    assert len(numbers) == 100
    assert all(re.fullmatch(r"YMS-\d{4}-\d{4}", n) for n in numbers)
    first, last = conn.execute(
        "SELECT MIN(posted_at), MAX(posted_at) FROM journal_entries"
    ).fetchone()
    # Accounts open (with their opening deposit) the day before activity starts on 2026-04-02.
    assert first.startswith("2026-04-01") and last.startswith("2026-06-30")
    (activity_start,) = conn.execute(
        "SELECT MIN(posted_at) FROM journal_entries WHERE description <> 'Opening deposit'"
    ).fetchone()
    assert activity_start.startswith("2026-04-02")


def test_cli_refuses_to_overwrite(tmp_path):
    path = tmp_path / "demo.db"
    path.write_bytes(b"")
    with pytest.raises(SystemExit) as exc:
        seed_data.main(["--db", str(path)])
    assert exc.value.code == 2


def test_full_seed_keeps_every_running_balance_non_negative(tmp_path):
    conn = ledger.open_db(tmp_path / "full.db")
    seed_data.build(conn, end=date(2026, 6, 30))
    assert ledger.check_invariants(conn) == []


def test_default_seed_ends_the_day_before_now(tmp_path):
    from datetime import UTC, datetime

    now = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)  # late in the day, not the wall clock
    conn = ledger.open_db(tmp_path / "now.db")
    summary = seed_data.build(conn, customers=5, accounts=8, days=3, now=now)
    (latest,) = conn.execute("SELECT MAX(posted_at) FROM journal_entries").fetchone()
    assert latest < "2026-03-10"
    assert summary.last_day == date(2026, 3, 9)


def test_entries_after_now_are_cut_and_the_summary_says_so(tmp_path):
    from datetime import UTC, datetime

    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
    conn = ledger.open_db(tmp_path / "cut.db")
    summary = seed_data.build(conn, customers=5, accounts=8, days=3, end=date(2026, 3, 10), now=now)
    (latest,) = conn.execute("SELECT MAX(posted_at) FROM journal_entries").fetchone()
    assert latest <= "2026-03-10T12:00:00Z"
    assert summary.last_day <= date(2026, 3, 10)
    assert ledger.check_invariants(conn) == []


def test_invariants_catch_a_posting_before_the_opening(conn, at):
    from bank.money import Money

    customer = ledger.create_customer(conn, "Early Bird", at("2026-03-02"))
    account = ledger.open_account(conn, customer, "EUR", at("2026-03-02", "09:00"))
    ledger.deposit(conn, account.id, Money.of("50.00", "EUR"), "Back-dated", at("2026-03-01"))
    problems = ledger.check_invariants(conn)
    assert any("before it was opened" in p for p in problems), problems


def test_invariants_catch_a_negative_running_balance(conn, at):
    from bank.money import Money

    customer = ledger.create_customer(conn, "Out Of Order", at("2026-03-01"))
    account = ledger.open_account(conn, customer, "EUR", at("2026-03-01", "07:00"))
    ledger.deposit(
        conn, account.id, Money.of("100.00", "EUR"), "Deposit", at("2026-03-01", "08:00")
    )
    ledger.withdraw(
        conn, account.id, Money.of("60.00", "EUR"), "Earlier", at("2026-03-01", "07:30")
    )
    problems = ledger.check_invariants(conn)
    assert any("negative running balance" in p for p in problems), problems
