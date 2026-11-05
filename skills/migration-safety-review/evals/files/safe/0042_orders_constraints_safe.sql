-- 0042, safe form: the same end state as the lock-heavy version, in steps
-- that never hold a strong lock for longer than a catalog change.
-- Each statement runs in its own transaction (autocommit). Do not wrap this
-- file in BEGIN/COMMIT: CREATE INDEX CONCURRENTLY refuses to run in one.

SET lock_timeout = '5s';

CREATE INDEX CONCURRENTLY orders_customer_id_idx ON orders (customer_id);

ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_fkey
    FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_fkey;

ALTER TABLE orders
    ADD CONSTRAINT orders_total_cents_check CHECK (total_cents >= 0) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_cents_check;

-- NOT NULL without a full scan under ACCESS EXCLUSIVE: prove it with a
-- validated CHECK first, then drop the helper constraint.
ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_not_null CHECK (customer_id IS NOT NULL) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_not_null;
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;
ALTER TABLE orders DROP CONSTRAINT orders_customer_id_not_null;

CREATE UNIQUE INDEX CONCURRENTLY orders_reference_key ON orders (reference);
ALTER TABLE orders
    ADD CONSTRAINT orders_reference_key UNIQUE USING INDEX orders_reference_key;

-- A table created here is new and empty, so a plain CREATE INDEX on it is fine.
CREATE TABLE order_notes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id bigint NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX order_notes_order_id_idx ON order_notes (order_id);
