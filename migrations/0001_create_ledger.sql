-- migrate:up
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    customer_id INTEGER REFERENCES customers (id),
    kind TEXT NOT NULL CHECK (kind IN ('customer', 'system')),
    currency TEXT NOT NULL,
    balance_minor INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    CHECK (kind = 'system' OR balance_minor >= 0),
    CHECK ((kind = 'customer') = (customer_id IS NOT NULL))
);

CREATE INDEX accounts_customer_idx ON accounts (customer_id);

CREATE TABLE journal_entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal', 'transfer')),
    description TEXT NOT NULL,
    posted_at TEXT NOT NULL
);

CREATE TABLE journal_lines (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES journal_entries (id),
    account_id INTEGER NOT NULL REFERENCES accounts (id),
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor <> 0)
);

CREATE INDEX journal_lines_account_idx ON journal_lines (account_id, entry_id);

-- migrate:down
DROP INDEX journal_lines_account_idx;
DROP TABLE journal_lines;
DROP TABLE journal_entries;
DROP INDEX accounts_customer_idx;
DROP TABLE accounts;
DROP TABLE customers;
