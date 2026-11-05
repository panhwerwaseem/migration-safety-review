-- SQL for shop/migrations/0007_order_region.py on PostgreSQL, in the shape
-- that `python manage.py sqlmigrate shop 0007` prints. Written by hand for
-- this fixture; render the real thing before reviewing a real migration.
BEGIN;
--
-- Add field region to order
--
ALTER TABLE "shop_order" ADD COLUMN "region" varchar(20) NULL;
--
-- Create index order_region_idx on field(s) region of model order
--
CREATE INDEX "order_region_idx" ON "shop_order" ("region");
COMMIT;
