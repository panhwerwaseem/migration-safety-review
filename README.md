# migration-safety-review

**An agent skill that reviews a database migration for locks, data loss and rollback before it ships, by linting the SQL the database will actually run.**

Left alone, a coding agent asked "is this migration safe?" tends to read the ORM file, see nothing alarming, and say yes. It does not render the SQL, does not say which lock each statement takes or for how long, and treats a down migration as a rollback even when the data is gone. This skill replaces that with a protocol and a linter: review the rendered SQL, name the lock and the table size behind every verdict, state how the data comes back for every destructive step, and never rewrite a migration that has already been applied somewhere.

Works with Claude Code, and with any agent that reads the open [Agent Skills](https://agentskills.io) format (Codex, Cursor, GitHub Copilot, Gemini CLI and others).

## What it looks like

The commands below are run from `skills/migration-safety-review/`.

A migration that adds a column, backfills it, makes it required and indexes it, all in one transaction:

```console
$ cat evals/files/backfill/0060_users_status.sql
-- 0060: add users.status, backfill it, make it required, and index it.

BEGIN;

ALTER TABLE users ADD COLUMN status text;

UPDATE users SET status = 'active';

ALTER TABLE users ALTER COLUMN status SET NOT NULL;

CREATE INDEX CONCURRENTLY users_status_idx ON users (status);

COMMIT;
```

```console
$ python3 scripts/lint_migration.py evals/files/backfill/0060_users_status.sql
evals/files/backfill/0060_users_status.sql: postgres, 6 statement(s), 5 finding(s)
  line 5  WARNING  missing-lock-timeout  table: users
      statement: ALTER TABLE users ADD COLUMN status text
      lock:      waits for its table lock with no time limit
      risk:      If a long-running transaction holds a conflicting lock, this statement waits, and new queries on the table queue behind it. Reported once per file.
      fix:       set-lock-timeout (references/safe-rewrites.md)
  line 7  ERROR  update-without-where  table: users
      statement: UPDATE users SET status = 'active'
      lock:      ROW EXCLUSIVE on the table and a row lock on every row until commit
      risk:      No WHERE clause limits the rows, so the statement runs over the whole table in one transaction: row locks held until commit and a burst of changes for replicas to apply.
      fix:       batched-backfill (references/safe-rewrites.md)
  line 7  WARNING  slow-step-under-ddl-lock  table: users
      statement: UPDATE users SET status = 'active'
      lock:      the table lock taken at line 5 is held until COMMIT
      risk:      Everything blocked by that lock keeps waiting for as long as this statement runs.
      fix:       split-schema-and-data (references/safe-rewrites.md)
  line 9  ERROR  set-not-null  table: users
      statement: ALTER TABLE users ALTER COLUMN status SET NOT NULL
      lock:      ACCESS EXCLUSIVE while the whole table is scanned
      risk:      Reads and writes wait for the scan. The scan is skipped only when a validated CHECK (column IS NOT NULL) constraint already exists.
      fix:       not-null-via-check (references/safe-rewrites.md)
  line 11  ERROR  concurrently-in-transaction  table: users
      statement: CREATE INDEX CONCURRENTLY users_status_idx ON users (status)
      lock:      none: the statement is rejected
      risk:      PostgreSQL refuses to run a CONCURRENTLY statement inside a transaction block, so the migration fails.
      fix:       run-outside-transaction (references/safe-rewrites.md)
BLOCKING: 3 error(s), 2 warning(s). Do not run this migration as written.
```

A migration written in safe steps passes, and the linter says what a pass does not prove:

```console
$ python3 scripts/lint_migration.py evals/files/safe/0042_orders_constraints_safe.sql
evals/files/safe/0042_orders_constraints_safe.sql: postgres, 14 statement(s), 0 finding(s)
NO FINDINGS. This is not proof of safety: confirm table sizes, deploy order and the rollback plan.
```

An ORM file is refused, because the risk is in the SQL it renders to:

```console
$ python3 scripts/lint_migration.py evals/files/django-add-index/0007_order_region.py
lint_migration.py: error: evals/files/django-add-index/0007_order_region.py: not a SQL file. Render the migration to SQL first; references/render-and-rollback.md lists the command for each tool.
```

## Install

Pick one route. Installing through more than one gives you the skill twice.

**Claude Code (plugin)**

```
/plugin install migration-safety-review --marketplace panhwerwaseem/migration-safety-review
```

On Claude Code versions before 2.1.275, use two steps:

```
/plugin marketplace add panhwerwaseem/migration-safety-review
/plugin install migration-safety-review@panhwerwaseem-migration-safety-review
```

**Any agent (skills CLI)**

```bash
npx skills add panhwerwaseem/migration-safety-review
```

**Manual**

Copy the `skills/migration-safety-review` folder into `.claude/skills/` (Claude Code) or `.agents/skills/` (cross-agent) in your project, or into the same folder under your home directory.

To pin a release, add the tag: `/plugin marketplace add panhwerwaseem/migration-safety-review#v0.1.0` or `npx skills add panhwerwaseem/migration-safety-review@v0.1.0`. Third-party plugins do not update automatically; run `claude plugin update migration-safety-review@panhwerwaseem-migration-safety-review` to update.

## Use

Describe the migration and the skill loads on its own:

> Is db/migrations/0042_orders_constraints.sql safe to run on production? orders has about 60 million rows.

In Claude Code you can also call it directly with `/migration-safety-review`.

The scripts also run on their own, for example in a pre-merge check:

```bash
python3 skills/migration-safety-review/scripts/lint_migration.py db/migrations/0042_orders_constraints.sql
python manage.py sqlmigrate shop 0007 | python3 skills/migration-safety-review/scripts/lint_migration.py -
```

## What is in the skill

| File | Purpose |
|---|---|
| `SKILL.md` | The protocol: seven hard rules and an eight-step workflow from rendering the SQL to a report with a lock, a size assumption and a decision per statement |
| `scripts/lint_migration.py` | Lints rendered SQL statement by statement. Each finding gives the line, the statement, the lock taken, the risk and the name of the safe rewrite. Exit code 1 when an error-level finding is present |
| `scripts/rule_catalog.py` | The rule table the linter uses, and a command to print it |
| `scripts/sqlsplit.py` | The statement splitter the linter uses. Handles comments, string literals, dollar-quoted bodies, psql meta-commands and the MySQL `DELIMITER` command. Run it to see how a file was split |
| `references/postgres-locks.md` | The lock each PostgreSQL statement takes, what it blocks, why a brief lock can still stall a table, and read-only queries for table size and open transactions |
| `references/safe-rewrites.md` | One section per fix name: concurrent index builds, `NOT VALID` then `VALIDATE`, `NOT NULL` through a check constraint, batched backfills, expand and contract for renames and type changes, and what a destructive step needs before it ships |
| `references/render-and-rollback.md` | How to render SQL from Django, Alembic, Rails, Prisma, EF Core, Laravel and Liquibase migrations, how to tell whether a migration runs in a transaction or has been applied, and how to plan rollback |
| `evals/` | Unsafe migrations with known findings, two safe files that must pass, eval cases, and trigger queries |

### What the linter checks

PostgreSQL 12 or later gets every rule. `python3 scripts/rule_catalog.py` prints the full table.

| Group | Rules |
|---|---|
| Index builds | `CREATE INDEX`, `DROP INDEX` and `REINDEX` without `CONCURRENTLY`; `CONCURRENTLY` inside a transaction |
| Table scans and rewrites | `ADD COLUMN ... NOT NULL` with no default; `ADD COLUMN` with a serial, identity, stored generated or volatile-default column; `ALTER COLUMN ... TYPE`; `SET NOT NULL`; foreign key or `CHECK` added without `NOT VALID`; `UNIQUE` or `PRIMARY KEY` built inline; `VACUUM FULL`; `CLUSTER` |
| Data loss | `DROP TABLE`, `DROP COLUMN`, `DROP SCHEMA`, `DROP DATABASE`, `TRUNCATE`, `DELETE` without `WHERE` |
| Running code | `RENAME` of a table or column |
| Transactions | `UPDATE` without `WHERE`; a backfill or `VALIDATE CONSTRAINT` in a transaction that already holds a schema-change lock; no `lock_timeout` before a statement that takes a conflicting table lock |
| Not analysed | `DO` blocks, and `UPDATE` or `DELETE` inside a `WITH` clause, are reported as warnings to review by hand |

MySQL and MariaDB get only the data-loss, rename and `UPDATE` or `DELETE` without `WHERE` rules. The linter does not assess MySQL lock behaviour and says so in its output.

### What it does not check

- Table size and traffic. The linter flags the statement; whether the lock matters depends on facts you supply.
- Whether the application code running during the deploy matches the new schema.
- Data changes run from code (`RunPython`, Ruby blocks), function bodies, and SQL built at run time.
- Less common operations: partition attach, detach, drop and truncate, `SET TABLESPACE`, `SET LOGGED` and `SET UNLOGGED`, explicit `LOCK TABLE`, triggers, and exclusion constraints.
- Whether a down migration really restores data. The protocol in `SKILL.md` makes the agent check that.

A clean result means none of the rules matched. It is not proof that a migration is safe.

## Requirements

- Python 3.9 or later
- No packages to install and no network access
- Your migration tool, if the migration is not already SQL, to render it

## What it reads, writes and contacts

- **Reads:** the SQL files you name, or standard input.
- **Writes:** nothing. Results go to standard output, errors to standard error.
- **Runs:** nothing. The scripts start no processes and open no database connection.
- **Contacts:** nothing. The scripts contain no network code; CI enforces this.
- **Permissions:** the skill pre-approves no tools. Your agent asks before running commands, as usual.

See [SECURITY.md](SECURITY.md).

## Testing status

- The scripts have unit tests that run in CI on Python 3.9 and 3.12, together with a validator for the skill format and plugin manifests. The tests run the linter against every fixture and check each planted defect by line and rule, and check that the safe fixtures produce no findings.
- The tests check the linter's behaviour, not PostgreSQL's. This repository does not run statements against a live server, so confirm a lock level in the PostgreSQL documentation for your version before a decision rests on it.
- The eval cases in `skills/migration-safety-review/evals/` are in the [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) format. A with-skill versus without-skill score has not been published yet; results are welcome as an issue or pull request.

## Development

```bash
python3 tools/validate.py              # skill format, manifests, no-network check
python3 -m unittest discover -s tests  # script unit tests
```

## License

[MIT](LICENSE)
