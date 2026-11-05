-- 0060: add users.status, backfill it, make it required, and index it.

BEGIN;

ALTER TABLE users ADD COLUMN status text;

UPDATE users SET status = 'active';

ALTER TABLE users ALTER COLUMN status SET NOT NULL;

CREATE INDEX CONCURRENTLY users_status_idx ON users (status);

COMMIT;
