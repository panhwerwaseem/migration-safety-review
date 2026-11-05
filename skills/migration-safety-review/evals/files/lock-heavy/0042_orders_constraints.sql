-- 0042: tie orders to customers and tighten the orders table.
-- Applied by a runner that sends this file as one transaction.

BEGIN;

CREATE INDEX orders_customer_id_idx ON orders (customer_id);

ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_fkey
    FOREIGN KEY (customer_id) REFERENCES customers (id);

ALTER TABLE orders
    ADD CONSTRAINT orders_total_cents_check CHECK (total_cents >= 0);

ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;

ALTER TABLE orders ADD CONSTRAINT orders_reference_key UNIQUE (reference);

COMMIT;
