"""SQLite connections and explicit write transactions."""

from __future__ import annotations

import itertools
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; callers start transactions explicitly."""
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        timeout=BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if str(path) != ":memory:":
        _enable_wal(conn)
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50) -> None:
    """Switch to WAL. Two first connections to a new file can race on this and get
    "database is locked" without the busy handler, so retry with a short back-off."""
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                conn.close()
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.2))


_SAVEPOINTS = itertools.count(1)
# The connection on which nested write_txn blocks may run as SAVEPOINTs, per thread.
_NESTING: ContextVar[sqlite3.Connection | None] = ContextVar("bank_nesting", default=None)


@contextmanager
def nesting_allowed(conn: sqlite3.Connection) -> Iterator[None]:
    """Let write_txn blocks on ``conn`` nest as SAVEPOINTs inside the open transaction.

    Opt-in, used only to run a job as one unit (``ledger.run_once``). Elsewhere a write_txn on
    a connection that is already in a transaction raises, as SQLite does for a nested BEGIN.
    """
    token = _NESTING.set(conn)
    try:
        yield
    finally:
        _NESTING.reset(token)


def _quietly(conn: sqlite3.Connection, *statements: str) -> None:
    """Undo steps after a failure: never let them hide the original exception."""
    for sql in statements:
        try:
            conn.execute(sql)
        except sqlite3.Error:
            pass


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the block in one write transaction that takes the write lock up front.

    Inside ``nesting_allowed(conn)`` and an open transaction, the block runs as a SAVEPOINT: a
    failure undoes only the block, and the outer transaction commits or rolls back everything.
    """
    if conn.in_transaction and _NESTING.get() is conn:
        name = f"sp_{next(_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            _quietly(conn, f"ROLLBACK TO {name}", f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _quietly(conn, "ROLLBACK")
        raise
    conn.execute("COMMIT")
