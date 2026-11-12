import sqlite3

import pytest

from bank import migrate
from bank.db import connect


def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows} - {"schema_migrations"}


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_up_applies_all_in_order_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "m.db")
    assert migrate.migrate_up(conn) == [1, 2]
    assert migrate.migrate_up(conn) == []
    assert migrate.applied_versions(conn) == [1, 2]
    assert {"customers", "accounts", "journal_entries", "journal_lines"} <= tables(conn)
    assert "reference" in columns(conn, "journal_entries")


def test_down_to_target_then_up_again(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate.migrate_up(conn)
    assert migrate.migrate_down(conn, 1) == [2]
    assert "reference" not in columns(conn, "journal_entries")
    assert migrate.migrate_down(conn, 0) == [1]
    assert tables(conn) == set()
    assert migrate.applied_versions(conn) == []
    assert migrate.migrate_up(conn) == [1, 2]


def test_failed_migration_rolls_back(tmp_path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / "0001_ok.sql").write_text(
        "-- migrate:up\nCREATE TABLE a (x);\n-- migrate:down\nDROP TABLE a;\n"
    )
    (folder / "0002_bad.sql").write_text(
        "-- migrate:up\nCREATE TABLE b (x);\nNOT SQL;\n-- migrate:down\nDROP TABLE b;\n"
    )
    conn = connect(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate.migrate_up(conn, folder)
    assert migrate.applied_versions(conn) == [1]
    assert tables(conn) == {"a"}


@pytest.mark.parametrize(
    "name, text",
    [
        ("1_short.sql", "-- migrate:up\nSELECT 1;\n-- migrate:down\nSELECT 1;\n"),
        ("0001_no_down.sql", "-- migrate:up\nSELECT 1;\n"),
        ("0001_down_first.sql", "-- migrate:down\nSELECT 1;\n-- migrate:up\nSELECT 1;\n"),
    ],
)
def test_malformed_files_are_rejected(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(ValueError):
        migrate.parse(path)
