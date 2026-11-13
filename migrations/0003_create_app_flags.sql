-- migrate:up
CREATE TABLE app_flags (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('claimed', 'done')),
    set_at TEXT NOT NULL
);

-- migrate:down
DROP TABLE app_flags;
