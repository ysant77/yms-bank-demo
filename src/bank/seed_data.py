"""Build a synthetic YMS Bank database from a fixed seed.

All people, account numbers and amounts are made up. The same seed and end date always give
the same data.

    uv run python -m bank.seed_data --db demo.db
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from bank import ledger
from bank.money import Money

SEED = 2026
CURRENCIES = ("EUR", "USD", "SGD", "JPY")
# Minor units per "unit of spending", so amounts look alike across currencies.
SCALE = {"EUR": 100, "USD": 100, "SGD": 130, "JPY": 160}

FIRST_NAMES = (
    "Ada", "Bram", "Cleo", "Dev", "Esme", "Finn", "Gita", "Hugo", "Ines", "Jun",
    "Kira", "Leon", "Mira", "Noor", "Omar", "Pia", "Quinn", "Rosa", "Sami", "Tova",
    "Uma", "Vik", "Wren", "Xena", "Yara", "Zane",
)  # fmt: skip
LAST_NAMES = (
    "Abel", "Brook", "Castle", "Dale", "Ember", "Field", "Grove", "Hale", "Isle", "Jett",
    "Knoll", "Lake", "Marsh", "North", "Oak", "Pike", "Reed", "Stone", "Thorne", "Vale",
)  # fmt: skip


@dataclass(frozen=True)
class Summary:
    customers: int
    accounts: int
    entries: int
    first_day: date
    last_day: date


def fake_names(rng: random.Random, count: int) -> list[str]:
    pool = [f"{first} {last}" for first in FIRST_NAMES for last in LAST_NAMES]
    return rng.sample(pool, count)


def _at(rng: random.Random, day: date) -> datetime:
    moment = time(rng.randrange(7, 22), rng.randrange(60), rng.randrange(60))
    return datetime.combine(day, moment, tzinfo=UTC)


def build(
    conn: sqlite3.Connection,
    *,
    seed: int = SEED,
    customers: int = 50,
    accounts: int = 100,
    days: int = 90,
    end: date | None = None,
) -> Summary:
    """Fill an empty, migrated database with customers, accounts and ``days`` of history."""
    if ledger.list_customers(conn):
        raise ValueError("database already has customers")
    rng = random.Random(seed)
    end = end or datetime.now(UTC).date()
    first_day = end - timedelta(days=days - 1)
    opened = _at(rng, first_day).replace(hour=8)

    customer_ids = [
        ledger.create_customer(conn, name, opened) for name in fake_names(rng, customers)
    ]
    owners = customer_ids + [rng.choice(customer_ids) for _ in range(accounts - customers)]
    by_currency: dict[str, list[int]] = {c: [] for c in CURRENCIES}
    for i, owner in enumerate(owners):
        currency = CURRENCIES[i % len(CURRENCIES)]
        account = ledger.open_account(conn, owner, currency, opened)
        by_currency[currency].append(account.id)
        amount = Money(rng.randrange(200, 5000) * SCALE[currency], currency)
        ledger.deposit(conn, account.id, amount, "Opening deposit", opened)

    all_accounts = [(acc, cur) for cur, ids in by_currency.items() for acc in ids]
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        moments = sorted(_at(rng, day) for _ in range(rng.randrange(20, 45)))
        for moment in moments:
            account_id, currency = rng.choice(all_accounts)
            amount = Money(rng.randrange(5, 400) * SCALE[currency], currency)
            roll = rng.random()
            try:
                if roll < 0.3:
                    ledger.deposit(conn, account_id, amount, "Salary credit", moment)
                elif roll < 0.6:
                    ledger.withdraw(conn, account_id, amount, "Card payment", moment)
                else:
                    target = rng.choice([a for a in by_currency[currency] if a != account_id])
                    reference = rng.choice(["Rent", "Dinner", "Gift", "Loan", None])
                    ledger.transfer(conn, account_id, target, amount, reference, moment)
            except ledger.InsufficientFunds:
                continue

    (entries,) = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()
    return Summary(customers, accounts, entries, first_day, end)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a synthetic YMS Bank demo database.")
    parser.add_argument("--db", type=Path, default=Path("demo.db"))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--end-date", type=date.fromisoformat, default=None)
    parser.add_argument("--force", action="store_true", help="replace an existing database")
    args = parser.parse_args(argv)

    if args.db.exists():
        if not args.force:
            parser.error(f"{args.db} exists; pass --force to replace it")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{args.db}{suffix}").unlink(missing_ok=True)
    conn = ledger.open_db(args.db)
    try:
        summary = build(conn, seed=args.seed, end=args.end_date)
        problems = ledger.check_invariants(conn)
    finally:
        conn.close()
    print(
        f"{args.db}: {summary.customers} customers, {summary.accounts} accounts, "
        f"{summary.entries} journal entries, {summary.first_day} to {summary.last_day}"
    )
    if problems:
        print("ledger check FAILED:", *problems, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
