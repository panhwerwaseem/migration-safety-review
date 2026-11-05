-- 0051 down: put the schema back the way it was.

SET lock_timeout = '5s';

CREATE TABLE billing_events_archive (
    id bigint PRIMARY KEY,
    account_id bigint NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
);

ALTER TABLE invoices ALTER COLUMN amount TYPE numeric(12, 2);

ALTER TABLE accounts RENAME COLUMN plan_code TO plan;

ALTER TABLE accounts ADD COLUMN legacy_plan_code text;
