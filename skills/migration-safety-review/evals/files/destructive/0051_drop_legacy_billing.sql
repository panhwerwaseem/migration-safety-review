-- 0051: remove the legacy billing fields now that invoices live in billing_v2.

SET lock_timeout = '5s';

ALTER TABLE accounts DROP COLUMN legacy_plan_code;

ALTER TABLE accounts RENAME COLUMN plan TO plan_code;

ALTER TABLE invoices ALTER COLUMN amount TYPE integer USING amount::integer;

DROP TABLE billing_events_archive;
