-- migrate:up
CREATE TABLE idempotency_keys (
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES journal_entries (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (customer_id, key)
);

-- migrate:down
DROP TABLE idempotency_keys;
