# Rendering SQL and planning rollback

How to turn a migration written for an ORM or migration tool into the SQL the
database will run, how to tell whether it runs in a transaction and whether it
has already been applied, and how to plan the way back.

Commands and flags change between versions. If one is rejected, check the
tool's `--help`.

## Contents

- [Why review the rendered SQL](#why-review-the-rendered-sql)
- [Render commands](#render-commands)
- [What rendering does not show](#what-rendering-does-not-show)
- [Does it run in a transaction](#does-it-run-in-a-transaction)
- [Has it been applied anywhere](#has-it-been-applied-anywhere)
- [Rollback planning](#rollback-planning)

## Why review the rendered SQL

The ORM file says what the developer asked for. The database runs what the
tool generated. One line such as "add index" or "alter field" can become a
plain `CREATE INDEX`, a table rewrite, or several statements in one
transaction, and the result differs between database backends and tool
versions. Lock and data-loss risk is a property of the SQL.

## Render commands

Render against the same database engine and major version as production.

| Tool | Forward SQL | Notes |
|---|---|---|
| Django | `python manage.py sqlmigrate <app_label> <migration_name>` | Add `--backwards` for the reverse SQL. Needs a configured database connection to resolve constraint names |
| Alembic | `alembic upgrade <from_rev>:<to_rev> --sql` | Offline mode. `alembic upgrade head --sql` renders everything from the start |
| Rails | No dry-run command | Run `bin/rails db:migrate` against a disposable database and read the statements in `log/development.log` |
| Prisma Migrate | The migration is already SQL: `prisma/migrations/<name>/migration.sql` | `npx prisma migrate dev --create-only` writes the file without applying it |
| EF Core | `dotnet ef migrations script <From> <To>` | |
| Laravel | `php artisan migrate --pretend` | |
| Liquibase | `liquibase update-sql` | |
| Flyway, goose, dbmate, golang-migrate, sqitch, plain SQL | The migration is already SQL | Lint the file directly |

Save the output to a file and lint that file:

```bash
python manage.py sqlmigrate shop 0007 > /tmp/shop_0007.sql
python3 scripts/lint_migration.py /tmp/shop_0007.sql
```

**Files that hold both directions.** goose (`-- +goose Up` and `-- +goose
Down`) and dbmate (`-- migrate:up` and `-- migrate:down`) keep the forward and
reverse SQL in one file. Split the file at the marker and lint each part
separately, or the reverse statements are reported as if they ran on deploy.

**Any other tool.** Apply the migration to a disposable database of the same
engine and version with statement logging switched on (PostgreSQL
`log_statement = 'all'`; the MySQL general query log), then collect the
statements from the log.

## What rendering does not show

- **Data migrations written in code.** Django `RunPython`, Ruby code in a
  Rails migration, and Python loops in an Alembic revision do not appear as
  SQL (Django prints a comment in their place). Read that code separately and
  look for full-table updates and per-row queries.
- **Alembic offline limits.** A revision that reads from the database cannot
  be rendered offline.
- **Conditional and dynamic SQL.** `DO` blocks and `EXECUTE` run statements
  the renderer never prints.
- **Table size and traffic.** See the size and activity checks in
  `references/postgres-locks.md`.

If the SQL cannot be rendered, say so and ask the user to run the render
command and share the output. Do not give a verdict from the ORM file.

## Does it run in a transaction

It matters twice: `CONCURRENTLY` statements are rejected inside a transaction,
and every lock taken in a transaction is held until it ends.

| Tool | Default on PostgreSQL | How to switch it off for one migration |
|---|---|---|
| Django | One transaction per migration | `atomic = False` on the `Migration` class |
| Rails | One transaction per migration | `disable_ddl_transaction!` |
| Alembic | The default `env.py` runs all pending migrations of one command in a single transaction | `transaction_per_migration=True` in `env.py` separates them; `autocommit_block()` takes one operation out |
| Flyway | One transaction per migration | See the Flyway documentation for the per-script setting |
| goose | One transaction per migration | `-- +goose NO TRANSACTION` |
| dbmate | One transaction per migration | `-- migrate:up transaction:false` |

- If the rendered SQL contains `BEGIN` and `COMMIT`, the linter follows them.
- If the tool wraps the migration but the SQL file does not show it, pass
  `--in-transaction` to `scripts/lint_migration.py`.
- If you cannot tell, lint both ways and report both results.
- MySQL: DDL statements commit implicitly whatever the tool does.

## Has it been applied anywhere

An applied migration must not be edited. Tools record a migration as applied
by its name, version or checksum. An edited file is never re-run where it was
already applied, so environments drift apart without any error, or the tool
refuses to continue because the checksum changed (Flyway and Prisma Migrate
check this).

| Tool | Command that shows which migrations are applied |
|---|---|
| Django | `python manage.py showmigrations <app_label>` |
| Alembic | `alembic current`, `alembic history` |
| Rails | `bin/rails db:migrate:status` |
| Prisma Migrate | `npx prisma migrate status` |
| Flyway | `flyway info` |
| Liquibase | `liquibase history` |

Each command reports on the database it is connected to. For staging and
production, ask the user. A migration that is merged to a branch that deploys
automatically should be treated as applied.

If the migration has been applied anywhere, or you cannot find out:

- Fix forward with a new migration, or
- Roll it back in every environment where it was applied (with the user's
  agreement), and only then replace it.

If an unsafe migration has reached a lower environment but not production,
the user has to choose between those two. Do not edit the file in place and
leave the environments to diverge.

## Rollback planning

Classify every statement before the migration ships.

| Kind of step | Examples | What rollback needs |
|---|---|---|
| Reversible by schema alone | Add a nullable column, create an index, add a constraint | The reverse statement. Data written to a new column since the deploy is lost |
| Reversible only with a copy of the data | Drop a column or table, truncate, delete rows, overwrite values in a backfill, narrow a type | A copy or backup taken before the step, and a tested way to load it back |
| Not reversible in place | Anything above with no copy | A stated decision by the data's owner that the loss is acceptable |

Points to settle and write down:

- **Reverse SQL.** Render it (`sqlmigrate --backwards`; `alembic downgrade
  <from_rev>:<to_rev> --sql`; for Rails run `bin/rails db:rollback STEP=1`
  against the disposable database) and lint it too. A reverse migration can
  take the same locks as the forward one.
- **Schema is not data.** A reverse migration that re-creates a dropped
  column or table brings it back empty.
- **Point of no return.** Rollback is cheap only until new code has written
  data that the old schema cannot hold. Name that moment.
- **Partial failure.** On PostgreSQL a transactional migration that fails
  rolls back as a whole. A non-transactional migration, and any MySQL
  migration with more than one DDL statement, can stop half way. Say what
  state that leaves and how to continue.
- **Deploy order.** Additive changes go out before the code that needs them.
  Destructive changes go out after the code that used the old shape is gone.
  A change that is both needs the expand and contract steps in
  `references/safe-rewrites.md`.
