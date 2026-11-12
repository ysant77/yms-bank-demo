"""Double-entry ledger for YMS Bank on SQLite.

Every posting is a journal entry whose lines sum to zero per currency. A positive line adds to
an account balance and a negative line takes from it. Each account also stores
``balance_minor``, which always equals the sum of its journal lines. Cash that enters or leaves
the bank goes through one clearing account per currency, the only accounts that may go below
zero.

Writes run in one ``BEGIN IMMEDIATE`` transaction, and a debit is a conditional ``UPDATE`` that
only succeeds when the funds are there, so concurrent withdrawals cannot overdraw an account.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bank.db import connect, write_txn
from bank.migrate import migrate_up
from bank.money import Money, exponent


class LedgerError(Exception):
    """A request the ledger refuses, such as a bad amount or currency."""


class NotFound(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


@dataclass(frozen=True)
class Account:
    id: int
    number: str
    customer_id: int | None
    kind: str
    currency: str
    balance_minor: int

    @property
    def balance(self) -> Money:
        return Money(self.balance_minor, self.currency)


@dataclass(frozen=True)
class StatementLine:
    entry_id: int
    posted_at: str
    kind: str
    description: str
    reference: str | None
    amount_minor: int
    balance_minor: int


@dataclass(frozen=True)
class Statement:
    account: Account
    start: date
    end: date
    opening_minor: int
    closing_minor: int
    lines: list[StatementLine]


def open_db(path: str | Path) -> sqlite3.Connection:
    """Connect and bring the schema up to date."""
    conn = connect(path)
    migrate_up(conn)
    return conn


def timestamp(at: datetime | None = None) -> str:
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def account_number(sequence: int) -> str:
    """Obviously fake account numbers: YMS-0000-0001, YMS-0000-0002, ..."""
    return f"YMS-{sequence // 10_000:04d}-{sequence % 10_000:04d}"


def _row_to_account(row: sqlite3.Row | None) -> Account:
    if row is None:
        raise NotFound("account not found")
    return Account(
        row["id"],
        row["number"],
        row["customer_id"],
        row["kind"],
        row["currency"],
        row["balance_minor"],
    )


ACCOUNT_COLUMNS = "id, number, customer_id, kind, currency, balance_minor"


def create_customer(conn: sqlite3.Connection, name: str, at: datetime | None = None) -> int:
    if not name.strip():
        raise LedgerError("customer name is empty")
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO customers (name, created_at) VALUES (?, ?)", (name.strip(), timestamp(at))
        )
        return int(cur.lastrowid)


def get_customer(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise NotFound("customer not found")
    return row


def list_customers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM customers ORDER BY id").fetchall()


def open_account(
    conn: sqlite3.Connection, customer_id: int, currency: str, at: datetime | None = None
) -> Account:
    exponent(currency)
    with write_txn(conn):
        get_customer(conn, customer_id)
        (count,) = conn.execute("SELECT COUNT(*) FROM accounts WHERE kind = 'customer'").fetchone()
        cur = conn.execute(
            "INSERT INTO accounts (number, customer_id, kind, currency, created_at)"
            " VALUES (?, ?, 'customer', ?, ?)",
            (account_number(count + 1), customer_id, currency, timestamp(at)),
        )
        return get_account(conn, int(cur.lastrowid))


def get_account(conn: sqlite3.Connection, account_id: int) -> Account:
    row = conn.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,))
    return _row_to_account(row.fetchone())


def get_account_by_number(conn: sqlite3.Connection, number: str) -> Account:
    row = conn.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE number = ?", (number,))
    return _row_to_account(row.fetchone())


def list_accounts(conn: sqlite3.Connection, customer_id: int) -> list[Account]:
    rows = conn.execute(
        f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE customer_id = ? ORDER BY id", (customer_id,)
    )
    return [_row_to_account(row) for row in rows]


def _clearing_account(conn: sqlite3.Connection, currency: str) -> int:
    """The per-currency account for cash entering or leaving the bank. Call inside a write."""
    number = f"YMS-CLR-{currency}"
    row = conn.execute("SELECT id FROM accounts WHERE number = ?", (number,)).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO accounts (number, customer_id, kind, currency, created_at)"
        " VALUES (?, NULL, 'system', ?, ?)",
        (number, currency, timestamp()),
    )
    return int(cur.lastrowid)


def _require(conn: sqlite3.Connection, account_id: int, amount: Money) -> Account:
    account = get_account(conn, account_id)
    if account.kind != "customer":
        raise NotFound("account not found")
    if amount.currency != account.currency:
        raise LedgerError(
            f"account {account.number} holds {account.currency}, not {amount.currency}"
        )
    if amount.minor <= 0:
        raise LedgerError("amount must be positive")
    return account


def _debit(conn: sqlite3.Connection, account: Account, minor: int) -> None:
    """Take funds only if they are there; the check and the update are one statement."""
    cur = conn.execute(
        "UPDATE accounts SET balance_minor = balance_minor - ?"
        " WHERE id = ? AND (kind = 'system' OR balance_minor >= ?)",
        (minor, account.id, minor),
    )
    if cur.rowcount != 1:
        raise InsufficientFunds(f"insufficient funds in {account.number}")


def _credit(conn: sqlite3.Connection, account_id: int, minor: int) -> None:
    conn.execute(
        "UPDATE accounts SET balance_minor = balance_minor + ? WHERE id = ?", (minor, account_id)
    )


def _post(
    conn: sqlite3.Connection,
    kind: str,
    description: str,
    currency: str,
    lines: list[tuple[int, int]],
    at: datetime | None,
    reference: str | None = None,
) -> int:
    """Write a balanced journal entry. ``lines`` holds (account_id, signed amount_minor)."""
    if sum(amount for _, amount in lines) != 0:
        raise LedgerError("journal entry does not balance")
    cur = conn.execute(
        "INSERT INTO journal_entries (kind, description, posted_at, reference) VALUES (?, ?, ?, ?)",
        (kind, description, timestamp(at), reference),
    )
    entry_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO journal_lines (entry_id, account_id, currency, amount_minor)"
        " VALUES (?, ?, ?, ?)",
        [(entry_id, account_id, currency, amount) for account_id, amount in lines],
    )
    return entry_id


def deposit(
    conn: sqlite3.Connection,
    account_id: int,
    amount: Money,
    description: str = "Cash deposit",
    at: datetime | None = None,
) -> int:
    with write_txn(conn):
        account = _require(conn, account_id, amount)
        clearing = _clearing_account(conn, amount.currency)
        _credit(conn, account.id, amount.minor)
        _credit(conn, clearing, -amount.minor)
        lines = [(account.id, amount.minor), (clearing, -amount.minor)]
        return _post(conn, "deposit", description, amount.currency, lines, at)


def withdraw(
    conn: sqlite3.Connection,
    account_id: int,
    amount: Money,
    description: str = "Cash withdrawal",
    at: datetime | None = None,
) -> int:
    with write_txn(conn):
        account = _require(conn, account_id, amount)
        clearing = _clearing_account(conn, amount.currency)
        _debit(conn, account, amount.minor)
        _credit(conn, clearing, amount.minor)
        lines = [(account.id, -amount.minor), (clearing, amount.minor)]
        return _post(conn, "withdrawal", description, amount.currency, lines, at)


def _transfer_in_txn(
    conn: sqlite3.Connection,
    from_account_id: int,
    to_account_id: int,
    amount: Money,
    reference: str | None,
    at: datetime | None,
) -> int:
    source = _require(conn, from_account_id, amount)
    target = _require(conn, to_account_id, amount)
    _debit(conn, source, amount.minor)
    _credit(conn, target.id, amount.minor)
    lines = [(source.id, -amount.minor), (target.id, amount.minor)]
    description = f"Transfer {source.number} to {target.number}"
    return _post(conn, "transfer", description, amount.currency, lines, at, reference)


def transfer(
    conn: sqlite3.Connection,
    from_account_id: int,
    to_account_id: int,
    amount: Money,
    reference: str | None = None,
    at: datetime | None = None,
) -> int:
    if from_account_id == to_account_id:
        raise LedgerError("cannot transfer to the same account")
    with write_txn(conn):
        return _transfer_in_txn(conn, from_account_id, to_account_id, amount, reference, at)


class IdempotencyConflict(LedgerError):
    """The idempotency key was already used for a different request."""


IDEMPOTENCY_WINDOW_SECONDS = 24 * 60 * 60


def request_fingerprint(
    from_account_id: int, to_account_id: int, amount: Money, reference: str | None
) -> str:
    """Hash of the transfer's meaning, so "10.0" and "10.00" are the same request."""
    canonical = json.dumps(
        [from_account_id, to_account_id, amount.minor, amount.currency, reference or ""],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def transfer_once(
    conn: sqlite3.Connection,
    customer_id: int,
    key: str,
    from_account_id: int,
    to_account_id: int,
    amount: Money,
    reference: str | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], bool]:
    """Post a transfer at most once per (customer, key) within 24 hours.

    The key is checked and stored in the same write transaction as the postings, so a retry
    after a timeout returns the first result instead of moving money twice. The stored result
    is the one the first request saw, so a replay does not show later balances. A failed
    attempt (for example insufficient funds) stores nothing, so the client may retry it.
    Returns ``(result, replayed)``.
    """
    if from_account_id == to_account_id:
        raise LedgerError("cannot transfer to the same account")
    now = now or datetime.now(UTC)
    fingerprint = request_fingerprint(from_account_id, to_account_id, amount, reference)
    oldest = timestamp(now - timedelta(seconds=IDEMPOTENCY_WINDOW_SECONDS))
    with write_txn(conn):
        conn.execute(
            "DELETE FROM idempotency_keys WHERE customer_id = ? AND key = ? AND created_at < ?",
            (customer_id, key, oldest),
        )
        row = conn.execute(
            "SELECT request_hash, response_json FROM idempotency_keys"
            " WHERE customer_id = ? AND key = ?",
            (customer_id, key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != fingerprint:
                raise IdempotencyConflict("idempotency key already used for a different request")
            return json.loads(row["response_json"]), True
        entry_id = _transfer_in_txn(conn, from_account_id, to_account_id, amount, reference, now)
        result = {
            "entry_id": entry_id,
            "to_account_number": get_account(conn, to_account_id).number,
            "amount_minor": amount.minor,
            "currency": amount.currency,
            "from_balance_minor": get_account(conn, from_account_id).balance_minor,
        }
        conn.execute(
            "INSERT INTO idempotency_keys"
            " (customer_id, key, request_hash, entry_id, response_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, key, fingerprint, entry_id, json.dumps(result), timestamp(now)),
        )
        return result, False


def run_once(
    conn: sqlite3.Connection, name: str, job: Callable[[sqlite3.Connection], None]
) -> bool:
    """Run ``job(conn)`` once per database, as one write transaction with its completion flag.

    Either the job's writes and the ``done`` flag commit together, or neither does, so a job that
    fails part-way leaves nothing behind and runs again on the next call. A second caller waits
    for the write lock, then sees the flag and returns False. Returns True when the job ran.
    """

    def done() -> bool:
        row = conn.execute("SELECT state FROM app_flags WHERE name = ?", (name,)).fetchone()
        return row is not None and row["state"] == "done"

    if done():
        return False
    with write_txn(conn):
        if done():
            return False
        job(conn)
        conn.execute(
            "INSERT INTO app_flags (name, state, set_at) VALUES (?, 'done', ?)"
            " ON CONFLICT (name) DO UPDATE SET state = 'done', set_at = excluded.set_at",
            (name, timestamp()),
        )
    return True


def statement(conn: sqlite3.Connection, account_id: int, start: date, end: date) -> Statement:
    """Lines posted from ``start`` to ``end``, both dates inclusive, with a running balance."""
    if start > end:
        raise LedgerError("statement start is after its end")
    account = get_account(conn, account_id)
    try:
        upper = (end + timedelta(days=1)).isoformat()
    except OverflowError:
        raise LedgerError("statement end is out of range") from None
    lower = start.isoformat()
    (opening,) = conn.execute(
        "SELECT COALESCE(SUM(l.amount_minor), 0) FROM journal_lines l"
        " JOIN journal_entries e ON e.id = l.entry_id"
        " WHERE l.account_id = ? AND e.posted_at < ?",
        (account_id, lower),
    ).fetchone()
    rows = conn.execute(
        "SELECT e.id, e.posted_at, e.kind, e.description, e.reference, l.amount_minor"
        " FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id"
        " WHERE l.account_id = ? AND e.posted_at >= ? AND e.posted_at < ?"
        " ORDER BY e.posted_at, e.id, l.id",
        (account_id, lower, upper),
    ).fetchall()
    running = opening
    lines = []
    for row in rows:
        running += row["amount_minor"]
        lines.append(StatementLine(*row, balance_minor=running))
    return Statement(account, start, end, opening, running, lines)


def check_invariants(conn: sqlite3.Connection) -> list[str]:
    """Return every broken ledger rule; an empty list means the books are consistent."""
    problems = []
    for row in conn.execute(
        "SELECT entry_id, currency, SUM(amount_minor) FROM journal_lines"
        " GROUP BY entry_id, currency HAVING SUM(amount_minor) <> 0"
    ):
        problems.append(f"entry {row[0]} is off by {row[2]} {row[1]}")
    for row in conn.execute(
        "SELECT l.id, a.number FROM journal_lines l JOIN accounts a ON a.id = l.account_id"
        " WHERE l.currency <> a.currency"
    ):
        problems.append(f"line {row[0]} has the wrong currency for {row[1]}")
    for row in conn.execute(
        "SELECT a.number, a.balance_minor, COALESCE(SUM(l.amount_minor), 0) AS total"
        " FROM accounts a LEFT JOIN journal_lines l ON l.account_id = a.id"
        " GROUP BY a.id HAVING a.balance_minor <> total"
    ):
        problems.append(f"{row[0]} stores {row[1]} but its lines sum to {row[2]}")
    for row in conn.execute(
        "SELECT number, balance_minor FROM accounts WHERE kind = 'customer' AND balance_minor < 0"
    ):
        problems.append(f"{row[0]} is negative: {row[1]}")
    # A statement shows lines in posting-time order, so the running balance must never dip.
    for row in conn.execute(
        "SELECT number, MIN(running), posted_at FROM ("
        " SELECT a.number, e.posted_at,"
        "  SUM(l.amount_minor) OVER (PARTITION BY l.account_id ORDER BY e.posted_at, e.id"
        "  ROWS UNBOUNDED PRECEDING) AS running"
        " FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id"
        " JOIN accounts a ON a.id = l.account_id WHERE a.kind = 'customer')"
        " WHERE running < 0 GROUP BY number"
    ):
        problems.append(f"{row[0]} has a negative running balance {row[1]} at {row[2]}")
    for row in conn.execute(
        "SELECT a.number, e.posted_at FROM journal_lines l"
        " JOIN journal_entries e ON e.id = l.entry_id JOIN accounts a ON a.id = l.account_id"
        " WHERE a.kind = 'customer' AND e.posted_at < a.created_at GROUP BY a.number"
    ):
        problems.append(f"{row[0]} has a posting at {row[1]}, before it was opened")
    return problems
