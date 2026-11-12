"""Numbered SQL migrations with an up and a down section each.

A migration file is ``migrations/NNNN_name.sql``. The line ``-- migrate:up`` starts the up
section and ``-- migrate:down`` starts the down section. Applied versions are kept in the
``schema_migrations`` table.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
UP_MARK = "-- migrate:up"
DOWN_MARK = "-- migrate:down"


@dataclass(frozen=True)
class Migration:
    number: str
    name: str
    path: Path
    up: str
    down: str

    @property
    def version(self) -> int:
        return int(self.number)


def default_dir() -> Path:
    """Migrations shipped in the wheel, else the ``migrations/`` folder of the checkout."""
    if env := os.environ.get("BANK_MIGRATIONS_DIR"):
        return Path(env)
    packaged = Path(__file__).resolve().parent / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


def parse(path: Path) -> Migration:
    match = FILE_RE.match(path.name)
    if not match:
        raise ValueError(f"bad migration file name: {path.name}")
    text = path.read_text(encoding="utf-8")
    if text.count(UP_MARK) != 1 or text.count(DOWN_MARK) != 1:
        raise ValueError(f"{path.name}: needs one '{UP_MARK}' and one '{DOWN_MARK}' line")
    head, _, rest = text.partition(UP_MARK)
    up, _, down = rest.partition(DOWN_MARK)
    if head.strip() or not up.strip():
        raise ValueError(f"{path.name}: the up section must come first and not be empty")
    return Migration(match.group(1), match.group(2), path, up.strip(), down.strip())


def load(directory: Path | None = None) -> list[Migration]:
    directory = directory or default_dir()
    migrations = [parse(p) for p in sorted(directory.glob("*.sql"))]
    numbers = [m.number for m in migrations]
    if len(set(numbers)) != len(numbers):
        raise ValueError("duplicate migration numbers")
    return migrations


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    _ensure_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    return [row[0] for row in rows]


def statements(script: str) -> list[str]:
    """Split a SQL script into complete statements, so each runs inside one transaction."""
    parts, current = [], ""
    for line in script.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                parts.append(current.strip())
            current = ""
    if current.strip():
        raise ValueError("migration ends with an incomplete statement")
    return parts


def _run(
    conn: sqlite3.Connection,
    version: int,
    script: str,
    bookkeeping: str,
    params: tuple,
    *,
    expect_applied: bool,
) -> bool:
    """Run one migration and its bookkeeping row in one write transaction.

    The applied state is read again after the write lock is taken, so two workers that start on
    the same database do not both run a migration. Returns False when another worker got there
    first.
    """
    _ensure_table(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        found = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if bool(found) != expect_applied:
            conn.execute("ROLLBACK")
            return False
        for statement in statements(script):
            conn.execute(statement)
        conn.execute(bookkeeping, params)
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate_up(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply every pending migration in order. Returns the versions applied."""
    done = set(applied_versions(conn))
    applied = []
    for m in load(directory):
        if m.version in done:
            continue
        now = datetime.now(UTC).isoformat(timespec="seconds")
        insert = "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)"
        if _run(conn, m.version, m.up, insert, (m.version, m.name, now), expect_applied=False):
            applied.append(m.version)
    return applied


def migrate_down(conn: sqlite3.Connection, target: int, directory: Path | None = None) -> list[int]:
    """Revert applied migrations above ``target``, newest first. Returns the versions reverted."""
    by_version = {m.version: m for m in load(directory)}
    reverted = []
    for version in sorted(applied_versions(conn), reverse=True):
        if version <= target:
            break
        if version not in by_version:
            raise ValueError(f"migration {version:04d} is applied but its file is missing")
        delete = "DELETE FROM schema_migrations WHERE version = ?"
        if _run(conn, version, by_version[version].down, delete, (version,), expect_applied=True):
            reverted.append(version)
    return reverted
