import sqlite3

import pytest

from bank import migrate
from bank.db import connect


def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows} - {"schema_migrations"}


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


ALL = [m.version for m in migrate.load()]


def test_up_applies_all_in_order_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "m.db")
    assert migrate.migrate_up(conn) == ALL
    assert migrate.migrate_up(conn) == []
    assert migrate.applied_versions(conn) == ALL
    assert {"customers", "accounts", "journal_entries", "journal_lines"} <= tables(conn)
    assert "reference" in columns(conn, "journal_entries")


def test_down_to_target_then_up_again(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate.migrate_up(conn)
    assert migrate.migrate_down(conn, 1) == sorted(ALL[1:], reverse=True)
    assert "reference" not in columns(conn, "journal_entries")
    assert migrate.migrate_down(conn, 0) == [1]
    assert tables(conn) == set()
    assert migrate.applied_versions(conn) == []
    assert migrate.migrate_up(conn) == ALL


def test_two_workers_on_a_fresh_database_apply_each_migration_once(tmp_path, run_together):
    path = tmp_path / "race.db"
    results = []

    def worker(start):
        start()  # both first connections race on the new file, including the WAL switch
        conn = connect(path)
        try:
            results.append(migrate.migrate_up(conn))
        finally:
            conn.close()

    assert run_together(2, worker) == []
    assert sorted(results[0] + results[1]) == ALL
    check = connect(path)
    try:
        assert migrate.applied_versions(check) == ALL
    finally:
        check.close()


def test_statements_split_on_complete_sql():
    script = "CREATE TABLE a (x TEXT DEFAULT ';');\nINSERT INTO a VALUES ('1;2');\n"
    assert migrate.statements(script) == [
        "CREATE TABLE a (x TEXT DEFAULT ';');",
        "INSERT INTO a VALUES ('1;2');",
    ]


def test_a_trailing_comment_is_not_an_incomplete_statement():
    assert migrate.statements("CREATE TABLE a (x);\n-- end of migration\n") == [
        "CREATE TABLE a (x);"
    ]
    with pytest.raises(ValueError):
        migrate.statements("CREATE TABLE a (x)\n")


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


def test_a_truly_locked_database_is_reported_after_one_busy_wait(tmp_path):
    """The WAL retry is only for quick failures: a database held by another writer fails after
    about one busy timeout, not after many."""
    import sqlite3
    import time

    path = tmp_path / "held.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            connect(path, timeout_ms=300)
        assert time.monotonic() - started < 1.5
    finally:
        holder.execute("ROLLBACK")
        holder.close()
