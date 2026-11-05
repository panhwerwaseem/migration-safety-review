-- Statements that look dangerous only to a parser that ignores quoting.
-- DROP TABLE users; this line is a comment
/* TRUNCATE orders; a block comment /* with a nested one; DROP TABLE a; */ still inside */

SET lock_timeout = '2s';

INSERT INTO audit_log (message) VALUES ('ran: DROP TABLE users; DELETE FROM orders');
INSERT INTO audit_log (message) VALUES ('it''s done; VACUUM FULL orders');
INSERT INTO audit_log (message) VALUES (E'it\'s done; TRUNCATE orders');

CREATE FUNCTION purge_order_cache() RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
    -- these statements run when the function is called, not when it is created
    DELETE FROM order_cache;
    UPDATE counters SET value = 0;
END;
$fn$;

CREATE FUNCTION price_label(cents integer) RETURNS text LANGUAGE sql AS $$
    SELECT '$' || (cents / 100)::text || '; DROP TABLE prices';
$$;

COMMENT ON COLUMN orders."type" IS 'legacy; do not DROP COLUMN yet';

UPDATE orders SET note = 'no WHERE here; UPDATE x SET y = 1' WHERE id = 42;
DELETE FROM sessions WHERE expires_at < now() - interval '30 days';
ALTER TABLE orders ALTER COLUMN type SET DEFAULT 'standard';
ALTER TABLE orders ADD COLUMN "drop" text, ADD COLUMN source text NOT NULL DEFAULT 'web';
