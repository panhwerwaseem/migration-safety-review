# Safe rewrites

Each finding from `scripts/lint_migration.py` names a fix. This file has one
section per fix name. SQL is PostgreSQL 12 or later unless stated. The ORM
notes name the operation to look for; check its exact signature in the
documentation for your version before using it.

## Contents

- [Rules and their rewrites](#rules-and-their-rewrites)
- [set-lock-timeout](#set-lock-timeout)
- [create-index-concurrently](#create-index-concurrently)
- [drop-index-concurrently](#drop-index-concurrently)
- [reindex-concurrently](#reindex-concurrently)
- [run-outside-transaction](#run-outside-transaction)
- [not-valid-then-validate](#not-valid-then-validate)
- [not-null-via-check](#not-null-via-check)
- [unique-using-index](#unique-using-index)
- [add-column-in-steps](#add-column-in-steps)
- [batched-backfill](#batched-backfill)
- [split-schema-and-data](#split-schema-and-data)
- [expand-contract-column](#expand-contract-column)
- [expand-contract-rename](#expand-contract-rename)
- [contract-with-rollback](#contract-with-rollback)
- [online-rebuild](#online-rebuild)
- [review-by-hand](#review-by-hand)

## Rules and their rewrites

| Rule | Severity | Dialects | Rewrite |
|---|---|---|---|
| `add-check-validates` | error | postgres | [not-valid-then-validate](#not-valid-then-validate) |
| `add-column-not-null-no-default` | error | postgres | [add-column-in-steps](#add-column-in-steps) |
| `add-column-table-rewrite` | error | postgres | [add-column-in-steps](#add-column-in-steps) |
| `add-foreign-key-validates` | error | postgres | [not-valid-then-validate](#not-valid-then-validate) |
| `add-unique-builds-index` | error | postgres | [unique-using-index](#unique-using-index) |
| `alter-column-type` | error | postgres | [expand-contract-column](#expand-contract-column) |
| `cluster` | error | postgres | [online-rebuild](#online-rebuild) |
| `concurrently-in-transaction` | error | postgres | [run-outside-transaction](#run-outside-transaction) |
| `create-index-not-concurrent` | error | postgres | [create-index-concurrently](#create-index-concurrently) |
| `delete-without-where` | error | postgres, mysql | [contract-with-rollback](#contract-with-rollback) |
| `do-block-not-analysed` | warning | postgres | [review-by-hand](#review-by-hand) |
| `drop-column` | error | postgres, mysql | [contract-with-rollback](#contract-with-rollback) |
| `drop-index-not-concurrent` | warning | postgres | [drop-index-concurrently](#drop-index-concurrently) |
| `drop-schema` | error | postgres, mysql | [contract-with-rollback](#contract-with-rollback) |
| `drop-table` | error | postgres, mysql | [contract-with-rollback](#contract-with-rollback) |
| `missing-lock-timeout` | warning | postgres | [set-lock-timeout](#set-lock-timeout) |
| `reindex-not-concurrent` | error | postgres | [reindex-concurrently](#reindex-concurrently) |
| `rename-column` | error | postgres, mysql | [expand-contract-rename](#expand-contract-rename) |
| `rename-table` | error | postgres, mysql | [expand-contract-rename](#expand-contract-rename) |
| `set-not-null` | error | postgres | [not-null-via-check](#not-null-via-check) |
| `slow-step-under-ddl-lock` | warning | postgres | [split-schema-and-data](#split-schema-and-data) |
| `truncate` | error | postgres, mysql | [contract-with-rollback](#contract-with-rollback) |
| `update-without-where` | error | postgres, mysql | [batched-backfill](#batched-backfill) |
| `vacuum-full` | error | postgres | [online-rebuild](#online-rebuild) |
| `write-in-with-not-analysed` | warning | postgres | [review-by-hand](#review-by-hand) |

An error is a statement that holds a strong lock for a time that grows with
the table, loses data, breaks code that is still running, or fails outright. A
warning needs a decision from a person. An error can be accepted when a stated
fact removes the risk (for example the table has a few thousand rows); record
that fact in the report.

## set-lock-timeout

```sql
SET lock_timeout = '5s';          -- at the top of a file that runs in autocommit
SET LOCAL lock_timeout = '5s';    -- as the first statement inside a transaction
```

- When the timeout expires the statement fails with a lock timeout error. In a
  transactional migration the whole migration rolls back. Retry later; do not
  raise the value until it passes.
- `SET LOCAL` only has an effect inside a transaction block.
- A value of `0` turns the timeout off.
- If the timeout is set on the migration role (`ALTER ROLE ... SET
  lock_timeout`) or by the migration runner, the linter cannot see it. State
  where it is set and accept the warning.

## create-index-concurrently

```sql
CREATE INDEX CONCURRENTLY orders_customer_id_idx ON orders (customer_id);
```

- It cannot run inside a transaction block: see
  [run-outside-transaction](#run-outside-transaction).
- It takes longer than a plain build and waits for transactions that were
  already open to finish.
- If it fails or is cancelled it leaves an index marked invalid, which still
  costs every write. Find leftovers with
  `SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;`, remove
  each with `DROP INDEX CONCURRENTLY`, and run the build again.
- Avoid `IF NOT EXISTS` here. After a failed build it finds the invalid index
  by name and skips the build, so the migration reports success with no usable
  index.
- A unique index built this way fails if duplicate values exist. Check for
  duplicates first.
- Rollback: `DROP INDEX CONCURRENTLY orders_customer_id_idx;`

ORM operations to look for: Django `AddIndexConcurrently` from
`django.contrib.postgres.operations`, in a migration with `atomic = False`.
Rails `add_index ..., algorithm: :concurrently` with `disable_ddl_transaction!`.
Alembic `op.create_index(..., postgresql_concurrently=True)` inside
`with op.get_context().autocommit_block():`.

## drop-index-concurrently

```sql
DROP INDEX CONCURRENTLY orders_old_idx;
```

- It cannot run inside a transaction block, takes one index name per
  statement, and does not accept `CASCADE`. An index that backs a `UNIQUE` or
  `PRIMARY KEY` constraint cannot be dropped this way; drop the constraint
  instead.
- First confirm the index is unused, for example from `idx_scan` in
  `pg_stat_user_indexes` on the primary and on each replica that serves reads.
- Rollback: rebuilding the index is a full concurrent build. Plan the time.

## reindex-concurrently

```sql
REINDEX INDEX CONCURRENTLY orders_customer_id_idx;
```

- PostgreSQL 12 or later. It cannot run inside a transaction block.
- A failed run can leave an invalid index whose name ends in `_ccnew` or
  `_ccold`. Drop it before retrying.

## run-outside-transaction

`CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY` and
`REINDEX ... CONCURRENTLY` are rejected inside a transaction block. A file
sent to the server as one multi-statement string also counts as a
transaction. Give the statement its own migration and switch the transaction
off for that migration:

| Tool | Switch |
|---|---|
| Django | `atomic = False` on the `Migration` class |
| Rails | `disable_ddl_transaction!` in the migration class |
| Alembic | `with op.get_context().autocommit_block():` around the operation |
| goose | `-- +goose NO TRANSACTION` at the top of the file |
| dbmate | `-- migrate:up transaction:false` |
| Others | Look for a per-migration "no transaction" option in the tool's documentation |

A migration that runs without a transaction and fails half way stays half
applied. Keep each such migration to one statement, or make every statement
safe to run twice.

## not-valid-then-validate

For a foreign key or a `CHECK` constraint on a table that has rows:

```sql
-- Transaction 1: catalog only. New and changed rows are checked from now on.
ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_fkey
    FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;

-- Transaction 2: scans existing rows without blocking reads or writes.
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_fkey;
```

- Run the two statements in separate transactions. In one transaction the scan
  runs under the lock that `ADD CONSTRAINT` took.
- If validation fails, existing rows break the rule. Fix the rows, then run
  `VALIDATE CONSTRAINT` again. The constraint already protects new writes.
- A foreign key written as a column constraint (`ADD COLUMN ... REFERENCES
  ...`) cannot be marked `NOT VALID`. Add the column first, then the
  constraint as above.
- Rollback: `ALTER TABLE orders DROP CONSTRAINT orders_customer_id_fkey;`

ORM operations to look for: Rails `add_foreign_key ..., validate: false` then
`validate_foreign_key` in a later migration, and `add_check_constraint ...,
validate: false` then `validate_check_constraint`. Django
`AddConstraintNotValid` and `ValidateConstraint` from
`django.contrib.postgres.operations` for check constraints; for a foreign key
use `RunSQL` with `state_operations`. Alembic passes `postgresql_not_valid=True`
to `op.create_foreign_key` or `op.create_check_constraint`, then runs the
`VALIDATE CONSTRAINT` statement with `op.execute` in a later migration.

## not-null-via-check

```sql
ALTER TABLE orders
    ADD CONSTRAINT orders_customer_id_not_null
    CHECK (customer_id IS NOT NULL) NOT VALID;                        -- transaction 1
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_not_null;   -- transaction 2
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;             -- transaction 3, no scan
ALTER TABLE orders DROP CONSTRAINT orders_customer_id_not_null;
```

- PostgreSQL 12 or later. `SET NOT NULL` skips its table scan because the
  validated constraint already proves there are no NULLs.
- Backfill NULLs before validating: see [batched-backfill](#batched-backfill).
- The linter accepts `SET NOT NULL` only when the `CHECK (col IS NOT NULL)`
  constraint is added and validated earlier in the same file. If those steps
  were in an earlier migration, cite that migration and accept the finding.
- Rollback: `ALTER TABLE orders ALTER COLUMN customer_id DROP NOT NULL;`

## unique-using-index

```sql
CREATE UNIQUE INDEX CONCURRENTLY orders_reference_key ON orders (reference);
ALTER TABLE orders
    ADD CONSTRAINT orders_reference_key UNIQUE USING INDEX orders_reference_key;
```

- The second statement is catalog only. If the constraint name differs from
  the index name, the index is renamed to match.
- For `PRIMARY KEY USING INDEX`, make the columns `NOT NULL` first with
  [not-null-via-check](#not-null-via-check). Otherwise the statement scans the
  table to check for NULLs.
- Rollback: `ALTER TABLE orders DROP CONSTRAINT orders_reference_key;` which
  also drops the index.

## add-column-in-steps

**Required column with one constant value for existing rows.** One statement
is enough on PostgreSQL 11 or later, because a constant default is stored in
the catalog and no row is rewritten:

```sql
ALTER TABLE users ADD COLUMN status text NOT NULL DEFAULT 'active';
```

**Required column whose value differs per row.**

1. Add the column nullable, with no default.
2. Deploy code that writes the column for new and changed rows.
3. Fill existing rows with a [batched-backfill](#batched-backfill).
4. Make it required with [not-null-via-check](#not-null-via-check).

**Column with a volatile default (for example a generated UUID).** Add the
column with no default. Then set the default in a separate statement:
`ALTER TABLE t ALTER COLUMN c SET DEFAULT gen_random_uuid();` applies only to
rows written afterwards and rewrites nothing. Backfill existing rows in
batches.

**Serial column.** `serial` is shorthand for an integer column whose default
reads a sequence. Do it in steps: add a plain `integer` or `bigint` column,
`CREATE SEQUENCE`, set the default with
`ALTER COLUMN ... SET DEFAULT nextval('...')`, then backfill existing rows in
batches.

**Identity or stored generated column.** Adding either to a table that has
rows rewrites it. If the rewrite time is not acceptable, use a plain column
filled by a default or a trigger, or schedule a maintenance window.

- Rollback: `ALTER TABLE users DROP COLUMN status;` This loses whatever was
  written to the new column since it was added.

## batched-backfill

```sql
-- One batch. Repeat with the next id range, one transaction per batch.
UPDATE users
SET status = 'active'
WHERE id >= 1 AND id < 10001
  AND status IS NULL;
```

- Run it outside the transaction that changed the schema.
- Walk the primary key in ranges. Commit after each batch and pause between
  batches so replicas and vacuum keep up.
- Keep the `IS NULL` (or equivalent) condition so the backfill can be stopped
  and re-run.
- In an ORM migration this means a non-transactional migration with a loop
  (Django `atomic = False` with `RunPython`; Rails `disable_ddl_transaction!`
  with `in_batches`), or a separate script or job.
- Rollback: a backfill that overwrites existing values is not reversible
  unless the old values were copied first.

## split-schema-and-data

Put the schema change and the slow step in separate transactions, which for
most tools means separate migrations:

1. Migration A: the `ALTER TABLE`. Its lock is released when it commits.
2. Migration B: the backfill, in batches, or the `VALIDATE CONSTRAINT`.

Check how your tool groups migrations. Alembic's default `env.py` runs every
pending migration of one `upgrade` command in a single transaction unless
`transaction_per_migration=True` is set, so two migration files can still
share one transaction.

## expand-contract-column

For a type change that rewrites the table, and for any change where old and
new code need different shapes:

1. **Expand.** Add a new column with the new type, nullable.
2. **Write both.** Deploy code that writes the old and the new column, or add
   a trigger that copies the value.
3. **Backfill** the new column with a [batched-backfill](#batched-backfill).
4. **Switch reads** to the new column. Deploy.
5. **Contract.** Stop writing the old column, then drop it in a later release
   using [contract-with-rollback](#contract-with-rollback).

- A cast that narrows (`numeric` to `integer`, `text` to `varchar(n)`,
  `timestamptz` to `date`) can lose data or fail part way. Check the existing
  values before step 3.
- Rollback is cheap until step 5: reads move back to the old column, which is
  still being written.

## expand-contract-rename

During a deploy, old and new application versions run at the same time. A
rename breaks whichever version does not match the database.

**Often the best fix is not to rename.** Keep the database name and change
only the name in code (Django `db_column` or `Meta.db_table`; Rails
`alias_attribute` or `self.table_name`).

**Column rename.** Treat it as a new column: add it, write both, backfill,
switch reads, stop writing the old one, drop the old one later. The steps are
the same as [expand-contract-column](#expand-contract-column).

**Table rename.** Rename and leave a view under the old name, in one
transaction, so old code keeps working:

```sql
BEGIN;
ALTER TABLE order_items RENAME TO line_items;
CREATE VIEW order_items AS SELECT * FROM line_items;
COMMIT;
```

A simple single-table view like this one accepts `INSERT`, `UPDATE` and
`DELETE` in PostgreSQL. Its column list is fixed when it is created. Run the
old code's queries against the view before relying on it, and drop the view
once no running code uses the old name.

- Rollback: before old code is gone, drop the view and rename back.

## contract-with-rollback

For `DROP TABLE`, `DROP COLUMN`, `DROP SCHEMA`, `TRUNCATE` and `DELETE`
without `WHERE`. Before the statement ships, the review must be able to answer
three questions.

**1. Is anything still using it?** Deployed application code, background
jobs, reports, exports, and replicas feeding other systems. Remove the usage
and deploy that first. An ORM that names every column in its queries must
stop knowing about the column before the column is dropped (Rails
`ignored_columns`; in Django, take the field out of the model state first
with `SeparateDatabaseAndState` and drop the column in a later release).

**2. How does the data come back?** Name one:

- A copy kept in the database, made before the drop:
  `CREATE TABLE accounts_legacy_plan_code_copy AS SELECT id, legacy_plan_code FROM accounts;`
- A backup or point-in-time recovery that has been restored successfully
  before, with a known restore time.
- The data can be rebuilt from another system. Name it.
- The owner of the data has agreed that it is disposable.

**3. Until when is rollback possible?** For example "the copy table is kept
for 30 days".

A down migration that re-creates an empty column or table restores the
schema, not the data. Do not count it as a rollback for the data.

In PostgreSQL, `DROP TABLE` and `TRUNCATE` can be rolled back until the
transaction commits. In MySQL they commit implicitly and cannot be rolled
back.

A softer first step is often available: stop writing to the column or table,
wait a release, and only then drop it.

## online-rebuild

`VACUUM FULL` and `CLUSTER` rewrite the table under ACCESS EXCLUSIVE. They do
not belong in a migration that runs during normal traffic.

- To make dead space reusable, plain `VACUUM` is enough and does not block
  reads or writes. It does not shrink the file.
- To shrink or reorder a table while it stays in use, the `pg_repack`
  extension rebuilds it with only brief locks. Check that it is available on
  your platform and read its documentation first.
- Otherwise schedule a maintenance window and say how long the rewrite took on
  a copy of the table.

## review-by-hand

The linter does not parse PL/pgSQL, so a `DO` block is reported and not
checked. The same goes for an `UPDATE` or `DELETE` written inside a `WITH`
clause: check by eye that it has a `WHERE` and how many rows it touches.

- Read the block. Write out the statements it will run for the current state
  of the target database, and lint those as plain SQL.
- An `ALTER TABLE` wrapped in `IF NOT EXISTS ... THEN` takes the same lock as
  one written directly.
- SQL assembled at run time with `EXECUTE` cannot be linted. Say so under
  "Not verified" in the report.
