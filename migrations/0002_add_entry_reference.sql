-- migrate:up
ALTER TABLE journal_entries ADD COLUMN reference TEXT;

CREATE INDEX journal_entries_posted_idx ON journal_entries (posted_at);

-- migrate:down
DROP INDEX journal_entries_posted_idx;
ALTER TABLE journal_entries DROP COLUMN reference;
