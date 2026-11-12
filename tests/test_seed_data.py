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
    assert first.startswith("2026-04-02") and last.startswith("2026-06-30")


def test_cli_refuses_to_overwrite(tmp_path):
    path = tmp_path / "demo.db"
    path.write_bytes(b"")
    with pytest.raises(SystemExit) as exc:
        seed_data.main(["--db", str(path)])
    assert exc.value.code == 2
