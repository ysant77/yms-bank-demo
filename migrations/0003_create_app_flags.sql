-- migrate:up
CREATE TABLE app_flags (
    name TEXT PRIMARY KEY,
    set_at TEXT NOT NULL
);

-- migrate:down
DROP TABLE app_flags;
