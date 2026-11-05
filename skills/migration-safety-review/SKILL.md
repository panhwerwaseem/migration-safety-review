---
name: migration-safety-review
description: Reviews a database schema migration for lock, data-loss and rollback hazards before it ships, by linting the rendered SQL statement by statement and naming the lock each statement takes. Use when a migration is about to be merged or deployed, when the user asks whether a migration is safe, will lock a table, cause downtime or can be rolled back, when writing or reviewing ALTER TABLE, CREATE INDEX, DROP COLUMN, a rename, a backfill or a NOT NULL change on a table that holds production data, or when a Django, Alembic, Rails, Prisma, Flyway or raw SQL migration targets PostgreSQL or MySQL.
license: MIT
compatibility: Requires Python 3.9 or later to run the bundled scripts. No network access and no extra packages needed. Lock analysis covers PostgreSQL 12 or later; MySQL gets a smaller rule set.
metadata:
  author: panhwerwaseem
  version: "0.1.0"
---

# Migration safety review

A migration review is done when every statement that touches an existing table
has a stated lock, a stated table-size assumption and a decision; every
destructive step has a stated way to get the data back; and the verdict says
plainly what was and was not verified. Everything below serves that.

Paths in this file are relative to this skill's directory.

## Hard rules

1. **Review the rendered SQL, not the ORM file.** Do not judge a Django,
   Alembic, Rails, Prisma or other tool migration from its source. Render it to
   SQL first. If it cannot be rendered, say so, ask the user for the render
   output, and give no verdict.
2. **No "safe" without the lock and the size.** Do not call a statement or a
   migration safe unless you have stated the lock it takes and the table size
   and write traffic you assumed. If the size is unknown, ask, or make the
   verdict conditional on it.
3. **Every destructive step needs a stated rollback.** For each drop,
   truncate, delete, overwrite or narrowing type change, name how the data
   comes back and until when. A down migration that re-creates an empty column
   or table does not count.
4. **Never rewrite a migration that has been applied anywhere.** Before
   editing a migration file, establish whether it has run in any environment.
   If it has, or you cannot find out, write a new migration or have it rolled
   back everywhere first. Do not edit it in place.
5. **A clean lint is not a verdict.** Do not report "no issues" unless
   `scripts/lint_migration.py` ran on the rendered SQL and you have also
   covered what it cannot see: table size, deploy order, code-run data
   changes, `DO` blocks and rollback.
6. **Do not dismiss a finding without a fact.** An error finding is accepted
   only with a specific reason, such as a measured row count or the table
   being created in the same release. "It should be fine" is not a reason.
7. **Do not run the migration against a shared database.** Reviewing means
   rendering, linting and reading. Apply a migration only to a disposable
   local database, and only when the user agrees.

## Workflow

### 1. Establish the context

Get these from the repository or the user before judging anything:

- Database engine and major version, and the migration tool.
- Each table the migration touches, with its approximate row count, size and
  write rate. `references/postgres-locks.md` has read-only queries the user
  can run.
- Whether the tool wraps the migration in a transaction.
- Whether the migration has been applied in any environment.
- How the deploy works: migration before or after the new code, and whether
  old and new application versions run side by side.

### 2. Render the SQL

Use the command for the tool from `references/render-and-rollback.md`, for
example `python manage.py sqlmigrate <app> <name>` or
`alembic upgrade <from>:<to> --sql`. Render the reverse migration too when one
exists. Plain SQL migrations need no rendering.

### 3. Lint

```bash
python3 scripts/lint_migration.py <rendered.sql>
python3 scripts/lint_migration.py --in-transaction <file.sql>   # the runner wraps the file, the SQL has no BEGIN
python3 scripts/lint_migration.py --dialect mysql <file.sql>    # MySQL or MariaDB
python3 scripts/lint_migration.py --json <file.sql>             # machine-readable
```

Exit code 0 means no error-level finding, 1 means at least one, 2 means the
input could not be read (for example an ORM file was passed instead of SQL).
Each finding gives the line, the statement, the lock, the risk and a fix name.
`python3 scripts/rule_catalog.py` lists every rule.
`python3 scripts/sqlsplit.py <file.sql>` shows how the file was split if a
line number looks wrong.

### 4. Decide each finding

For every finding, and for every other statement on an existing table:

1. Confirm the lock in `references/postgres-locks.md`.
2. Combine it with the table's size and traffic from step 1. A SHARE lock for
   an index build on 20,000 rows is not the same finding as on 200 million.
3. Decide: **rewrite** (the default for errors on large or busy tables), or
   **accept** with the fact that makes it acceptable.

Warnings need the same decision. `missing-lock-timeout` is accepted only when
you can say where else the timeout is set.

### 5. Check what the linter cannot see

- **Code compatibility.** Will the application version that is running during
  the deploy work against the new schema, and the new version against the old
  schema? Renames, drops and new required columns fail here even when the lock
  is brief.
- **Data changes run from code.** `RunPython`, Ruby blocks and similar do not
  appear in rendered SQL. Read them for full-table updates and per-row loops.
- **`DO` blocks and dynamic SQL.** Read them by hand.
- **Migration grouping.** Several migrations applied in one transaction share
  their locks until the end.

### 6. Plan the rollback

Fill in the rollback part of the report using the table in
`references/render-and-rollback.md`: the reverse statement for each step, the
copy or backup for each destructive step, the point after which rollback
loses data, and what a failure half way leaves behind. Lint the reverse SQL
as well.

### 7. Rewrite, if needed

Look up each finding's fix name in `references/safe-rewrites.md` and apply it.
Write a new migration unless you have established under rule 4 that the file
has never been applied. Express the fix in the project's migration tool, not
as loose SQL, when the project uses one. Then render and lint again. Finish
when the linter exits 0, or when every remaining finding is accepted with its
reason.

### 8. Report

```
Migration:        <file or revision>, <tool>, <engine and version>
Rendered with:    <command>; runs in a transaction: <yes | no | unknown>
Applied anywhere: <no | yes, where | unknown>
Linter:           <errors> errors, <warnings> warnings, exit <code>
Verdict:          <safe to run as written | safe only if <conditions> | not safe as written>

| Line | Statement | Lock, and what it blocks | Table size assumed | Decision |
|------|-----------|--------------------------|--------------------|----------|
| ...  | ...       | ...                      | ...                | rewrite: <fix name> / accept: <fact> |

Rollback:         <per step: reverse statement, or where the data comes back from, and until when>
Deploy order:     <migration first or code first, and why>
Changes made:     <new migration written | file edited (never applied) | none>
Not verified:     <table sizes not confirmed, code-run data changes, DO blocks, anything assumed>
```

## Edge cases

- **Cannot render.** No database connection or the tool is not installed: ask
  the user to run the render command. Rule 1 still applies.
- **Small or new tables.** The linter already skips tables created earlier in
  the same file. For a small existing table, accept the finding and record the
  row count you relied on.
- **Unsafe migration already applied to staging but not production.** The
  user chooses: roll it back where it ran and replace it, or fix forward with
  a new migration. Do not edit the file in place.
- **Up and down in one file** (goose, dbmate). Split at the marker and lint
  each half, or the reverse statements are reported as if they ran on deploy.
- **MySQL and MariaDB.** Only the data-loss, rename and unbounded
  UPDATE/DELETE rules run, and no lock is stated. Say in the report that lock
  behaviour was not assessed, and point the user to the online DDL
  documentation for their version.
- **PostgreSQL older than 12.** The linter assumes 12 or later. See the
  version notes in `references/postgres-locks.md`.
- **The linter and your reading disagree.** Report both and say which you
  trust and why. Do not drop either silently.
