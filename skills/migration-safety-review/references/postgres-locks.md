# PostgreSQL locks taken by migration statements

Use this file to state, for each statement in a migration, which lock it takes,
what that lock blocks, and how long it is held. Everything here is for
PostgreSQL 12 or later unless a version is named. For behaviour that matters to
your decision, confirm it in the documentation for your exact version
("Explicit Locking" and the Notes section of "ALTER TABLE").

## Contents

- [What each lock mode blocks](#what-each-lock-mode-blocks)
- [Lock and work per statement](#lock-and-work-per-statement)
- [Why a brief lock can still stall a table](#why-a-brief-lock-can-still-stall-a-table)
- [Locks last until the transaction ends](#locks-last-until-the-transaction-ends)
- [Size and activity checks](#size-and-activity-checks)
- [Version notes](#version-notes)
- [MySQL](#mysql)

## What each lock mode blocks

Ordinary traffic takes weak table locks: `SELECT` takes ACCESS SHARE, and
`INSERT`, `UPDATE` and `DELETE` take ROW EXCLUSIVE. A migration statement
blocks traffic when its lock conflicts with those.

| Lock mode | Blocks `SELECT` | Blocks `INSERT`, `UPDATE`, `DELETE` | Blocks other schema changes and `VACUUM` |
|---|---|---|---|
| SHARE UPDATE EXCLUSIVE | No | No | Yes |
| SHARE | No | Yes | Yes |
| SHARE ROW EXCLUSIVE | No | Yes | Yes |
| ACCESS EXCLUSIVE | Yes | Yes | Yes |

ACCESS EXCLUSIVE is the default for `ALTER TABLE`: every form takes it unless
the documentation says otherwise.

## Lock and work per statement

"Catalog only" means the statement finishes as soon as it has the lock. The
other entries hold the lock for a time that grows with the table.

| Statement | Lock on the table | Work done while holding it |
|---|---|---|
| `CREATE INDEX` | SHARE | Builds the index |
| `CREATE INDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | Builds the index in several passes; waits for older transactions to finish |
| `DROP INDEX` | ACCESS EXCLUSIVE | Catalog only |
| `DROP INDEX CONCURRENTLY` | Does not block reads or writes | Waits for conflicting transactions to finish |
| `REINDEX` | Blocks writes to the table; ACCESS EXCLUSIVE on the index being rebuilt | Rebuilds the index |
| `REINDEX ... CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | Rebuilds the index in several passes |
| `ALTER TABLE ... ADD COLUMN`, with no default or a non-volatile default (a constant, or `now()`) | ACCESS EXCLUSIVE | Catalog only |
| `ALTER TABLE ... ADD COLUMN` with a volatile default, a serial or identity column, or a stored generated column | ACCESS EXCLUSIVE | Rewrites the table and its indexes |
| `ALTER TABLE ... DROP COLUMN` | ACCESS EXCLUSIVE | Catalog only; disk space is not returned at once |
| `ALTER TABLE ... ALTER COLUMN ... TYPE` | ACCESS EXCLUSIVE | Rewrites the table and its indexes, unless the old type is binary coercible to the new one |
| `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL` | ACCESS EXCLUSIVE | Scans the table, unless a valid `CHECK (col IS NOT NULL)` constraint exists |
| `ALTER TABLE ... ALTER COLUMN ... DROP NOT NULL`, `SET DEFAULT`, `DROP DEFAULT` | ACCESS EXCLUSIVE | Catalog only |
| `ALTER TABLE ... ADD CONSTRAINT ... CHECK` | ACCESS EXCLUSIVE | Scans the table; catalog only with `NOT VALID` |
| `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY` | SHARE ROW EXCLUSIVE on both tables | Scans the referencing table; catalog only with `NOT VALID` |
| `ALTER TABLE ... VALIDATE CONSTRAINT` | SHARE UPDATE EXCLUSIVE, plus ROW SHARE on the referenced table for a foreign key | Scans the table |
| `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE` or `PRIMARY KEY` | ACCESS EXCLUSIVE | Builds a unique index; catalog only with `USING INDEX` (a primary key also scans if its columns are not already `NOT NULL`) |
| `ALTER TABLE ... DROP CONSTRAINT` | ACCESS EXCLUSIVE | Catalog only |
| `ALTER TABLE ... RENAME` (table or column) | ACCESS EXCLUSIVE | Catalog only |
| `DROP TABLE`, `TRUNCATE` | ACCESS EXCLUSIVE | Removes the data |
| `VACUUM FULL`, `CLUSTER` | ACCESS EXCLUSIVE | Rewrites the table and its indexes |
| `VACUUM` (without `FULL`), `ANALYZE` | SHARE UPDATE EXCLUSIVE | Does not block reads or writes |
| `UPDATE`, `DELETE` | ROW EXCLUSIVE, plus a lock on each row touched, held until commit | Other writers to the same rows wait |

Type changes that do not rewrite include raising or removing the length limit
of a `varchar` column and changing `varchar` to `text`. Changing `integer` to
`bigint` does rewrite. When unsure, time the statement on a copy of the table.

## Why a brief lock can still stall a table

Lock requests queue. An `ALTER TABLE` that needs ACCESS EXCLUSIVE waits behind
any transaction that already holds a lock on the table, including one that ran
a single `SELECT` and is now idle in a transaction. While the `ALTER TABLE`
waits, every new query on the table queues behind it. A catalog-only change can
therefore block the table for as long as the oldest open transaction that
touched it.

Limit the wait:

```sql
SET lock_timeout = '5s';
```

`lock_timeout` applies to each lock acquisition. When it expires the statement
fails and can be retried. The default is 0, which means wait without limit.
`scripts/lint_migration.py` reports `missing-lock-timeout` when a file takes a
conflicting table lock without setting it in the file. If the migration role or
the runner sets it elsewhere, say so in the review.

## Locks last until the transaction ends

A lock taken by a statement is held until the transaction commits or rolls
back, not until the statement finishes. Consequences:

- A backfill `UPDATE` after an `ALTER TABLE` in the same transaction runs for
  its whole duration under the ACCESS EXCLUSIVE lock.
- `VALIDATE CONSTRAINT` in the same transaction as `ADD CONSTRAINT ... NOT
  VALID` scans the table under the stronger lock that `ADD CONSTRAINT` took.
- Several `ALTER TABLE` statements on different tables in one transaction hold
  all their locks together until the end.

`scripts/lint_migration.py` reports the first two as
`slow-step-under-ddl-lock`. Most migration tools wrap each migration in a
transaction; `references/render-and-rollback.md` lists how to tell.

## Size and activity checks

A verdict depends on how large and how busy the table is. These read-only
queries give the facts. Ask the user to run them against the target database,
or to give you the numbers; do not connect to a production database yourself.

```sql
-- Estimated row count and total size, including indexes
SELECT reltuples::bigint AS estimated_rows,
       pg_size_pretty(pg_total_relation_size('public.orders')) AS total_size
FROM pg_class
WHERE oid = 'public.orders'::regclass;

-- Oldest open transactions: these are what a lock request would wait behind
SELECT pid, now() - xact_start AS open_for, state, left(query, 80) AS query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY xact_start
LIMIT 5;
```

State the numbers you used in the report. "Small table" is not a finding;
"about 20,000 rows, 6 MB, so a plain `CREATE INDEX` holds its lock for well
under a second" is.

## Version notes

- **PostgreSQL 11** made `ADD COLUMN` with a non-volatile default a
  catalog-only change. On version 10 and earlier, `ADD COLUMN` with any
  `DEFAULT` rewrites the table.
- **PostgreSQL 12** added `REINDEX ... CONCURRENTLY`, and lets `SET NOT NULL`
  skip its scan when a valid `CHECK (col IS NOT NULL)` constraint exists.
- The linter assumes version 12 or later. For an older server, treat every
  `ADD COLUMN ... DEFAULT` as a table rewrite and keep the `CHECK` constraint
  instead of converting it to `SET NOT NULL`.
- `CREATE INDEX CONCURRENTLY` on a partitioned parent table has long been
  unsupported. The documented route is an index on the parent created with
  `ON ONLY`, a concurrent index on each partition, then `ALTER INDEX ... ATTACH
  PARTITION` for each. Check the partitioning chapter for your version.

## MySQL

This skill does not assess MySQL or MariaDB lock behaviour. What an `ALTER
TABLE` blocks there depends on the server version, the storage engine, and the
operation, and is listed under "Online DDL Operations" in the MySQL reference
manual. Two facts hold generally and matter for review:

- DDL statements commit implicitly. A migration that fails half way stays half
  applied; it cannot be rolled back as a transaction.
- You can state the requirement in the statement, for example
  `ALTER TABLE ... , ALGORITHM=INPLACE, LOCK=NONE`. The server then refuses the
  statement if it cannot meet the requirement, instead of silently copying the
  table under a stronger lock.

For large tables many teams use an online schema change tool such as gh-ost or
pt-online-schema-change. Whether to use one is a decision for the user.
