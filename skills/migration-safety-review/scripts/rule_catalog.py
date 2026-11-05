#!/usr/bin/env python3
"""The rules that lint_migration.py applies, and a command to print them.

Each rule has a severity, the dialects it applies to, the lock the statement
takes on PostgreSQL, the risk, and the name of the section in
references/safe-rewrites.md that gives the safe rewrite.

Lock levels are for PostgreSQL 12 or later. MySQL gets only the rules whose
risk does not depend on lock behaviour: data loss, renames, and UPDATE or
DELETE without WHERE.

Examples:
  rule_catalog.py                       # one line per rule
  rule_catalog.py --dialect mysql       # only the rules applied to MySQL
  rule_catalog.py --rule set-not-null   # lock, risk and fix for one rule
  rule_catalog.py --json

Exit codes: 0 = printed, 2 = usage error (for example an unknown rule id).
Exit code 1 is not used: this script reports no findings.
Standard library only. Reads no files. Writes only to stdout and stderr.
Makes no network calls.
"""
import argparse
import json
import sys
from collections import namedtuple

Rule = namedtuple("Rule", "severity dialects lock risk fix")

PG, BOTH = ("postgres",), ("postgres", "mysql")
MYSQL_LOCK = "not assessed for MySQL (depends on version, engine and the ALGORITHM and LOCK clauses)"
DATA_LOSS = " After commit the data comes back only from a backup or a copy made beforehand."
OLD_NAME = ("Application instances still running the previous release use the old name and fail "
            "until they are replaced.")

RULES = {
    "create-index-not-concurrent": Rule(
        "error", PG, "SHARE on the table for the whole index build",
        "Blocks INSERT, UPDATE and DELETE on the table until the build finishes.",
        "create-index-concurrently"),
    "concurrently-in-transaction": Rule(
        "error", PG, "none: the statement is rejected",
        "PostgreSQL refuses to run a CONCURRENTLY statement inside a transaction block, so the "
        "migration fails.",
        "run-outside-transaction"),
    "add-column-not-null-no-default": Rule(
        "error", PG, "ACCESS EXCLUSIVE",
        "Fails on any table that already has rows, because the existing rows would hold NULL.",
        "add-column-in-steps"),
    "add-column-table-rewrite": Rule(
        "error", PG, "ACCESS EXCLUSIVE for a full table rewrite",
        "A serial, identity, stored generated or volatile-default column rewrites every row and "
        "rebuilds every index while reads and writes on the table wait.",
        "add-column-in-steps"),
    "alter-column-type": Rule(
        "error", PG,
        "ACCESS EXCLUSIVE; full table rewrite unless the old type is binary coercible to the new one",
        "Reads and writes wait for the rewrite and the index rebuilds. Code still running may also "
        "break on the new type.",
        "expand-contract-column"),
    "set-not-null": Rule(
        "error", PG, "ACCESS EXCLUSIVE while the whole table is scanned",
        "Reads and writes wait for the scan. The scan is skipped only when a validated "
        "CHECK (column IS NOT NULL) constraint already exists.",
        "not-null-via-check"),
    "add-foreign-key-validates": Rule(
        "error", PG, "SHARE ROW EXCLUSIVE on both tables while the referencing table is scanned",
        "Writes to both tables wait until every existing row has been checked.",
        "not-valid-then-validate"),
    "add-check-validates": Rule(
        "error", PG, "ACCESS EXCLUSIVE while the whole table is scanned",
        "Reads and writes wait until every existing row has been checked.",
        "not-valid-then-validate"),
    "add-unique-builds-index": Rule(
        "error", PG, "ACCESS EXCLUSIVE while the unique index is built",
        "Reads and writes wait for the index build.",
        "unique-using-index"),
    "drop-table": Rule(
        "error", BOTH, "ACCESS EXCLUSIVE",
        "Deletes the table and its rows. Code that still uses the table fails." + DATA_LOSS,
        "contract-with-rollback"),
    "drop-column": Rule(
        "error", BOTH, "ACCESS EXCLUSIVE, brief once granted",
        "Deletes the column's data. Code that still reads or writes the column fails." + DATA_LOSS,
        "contract-with-rollback"),
    "drop-schema": Rule(
        "error", BOTH, "not the main concern: this removes whole objects",
        "Deletes a schema or database and everything in it." + DATA_LOSS,
        "contract-with-rollback"),
    "truncate": Rule(
        "error", BOTH, "ACCESS EXCLUSIVE",
        "Deletes every row in the table." + DATA_LOSS,
        "contract-with-rollback"),
    "delete-without-where": Rule(
        "error", BOTH, "ROW EXCLUSIVE on the table and a row lock on every row until commit",
        "No WHERE clause limits the rows, so the statement can delete every row." + DATA_LOSS,
        "contract-with-rollback"),
    "update-without-where": Rule(
        "error", BOTH, "ROW EXCLUSIVE on the table and a row lock on every row until commit",
        "No WHERE clause limits the rows, so the statement runs over the whole table in one "
        "transaction: row locks held until commit and a burst of changes for replicas to apply.",
        "batched-backfill"),
    "rename-table": Rule(
        "error", BOTH, "ACCESS EXCLUSIVE, brief once granted", OLD_NAME, "expand-contract-rename"),
    "rename-column": Rule(
        "error", BOTH, "ACCESS EXCLUSIVE, brief once granted", OLD_NAME, "expand-contract-rename"),
    "vacuum-full": Rule(
        "error", PG, "ACCESS EXCLUSIVE for a full table rewrite",
        "Reads and writes wait for the whole rewrite.",
        "online-rebuild"),
    "cluster": Rule(
        "error", PG, "ACCESS EXCLUSIVE for a full table rewrite",
        "Reads and writes wait for the whole rewrite.",
        "online-rebuild"),
    "reindex-not-concurrent": Rule(
        "error", PG, "blocks writes to the table and takes ACCESS EXCLUSIVE on each index it rebuilds",
        "Writes wait for the rebuild, and so do reads that would use the index.",
        "reindex-concurrently"),
    "slow-step-under-ddl-lock": Rule(
        "warning", PG, "a table lock taken earlier in this transaction is held until COMMIT",
        "Everything blocked by that lock keeps waiting for as long as this statement runs.",
        "split-schema-and-data"),
    "missing-lock-timeout": Rule(
        "warning", PG, "waits for its table lock with no time limit",
        "If a long-running transaction holds a conflicting lock, this statement waits, and new "
        "queries on the table queue behind it. Reported once per file.",
        "set-lock-timeout"),
    "drop-index-not-concurrent": Rule(
        "warning", PG, "ACCESS EXCLUSIVE on the index's table, brief once granted",
        "Waits behind any open transaction that uses the table, and reads and writes queue behind "
        "it meanwhile.",
        "drop-index-concurrently"),
    "do-block-not-analysed": Rule(
        "warning", PG, "unknown",
        "This linter does not parse PL/pgSQL. DDL or DML inside the block has not been checked.",
        "review-by-hand"),
    "write-in-with-not-analysed": Rule(
        "warning", PG, "unknown",
        "An UPDATE or DELETE inside a WITH clause has not been checked for a missing WHERE or for "
        "running under a DDL lock.",
        "review-by-hand"),
}

# Column types and default expressions that force a table rewrite in ADD COLUMN.
SERIAL_TYPES = ("SERIAL", "BIGSERIAL", "SMALLSERIAL", "SERIAL2", "SERIAL4", "SERIAL8")
VOLATILE_FUNCTIONS = ("RANDOM", "GEN_RANDOM_UUID", "UUID_GENERATE_V1", "UUID_GENERATE_V1MC",
                      "UUID_GENERATE_V4", "CLOCK_TIMESTAMP", "TIMEOFDAY", "NEXTVAL")
# Version tables that migration tools maintain themselves. Rendered SQL often includes a
# one-row INSERT, UPDATE or DELETE on them; those statements are not findings.
BOOKKEEPING_TABLES = ("alembic_version", "schema_migrations", "django_migrations",
                      "flyway_schema_history", "_prisma_migrations", "goose_db_version",
                      "databasechangelog", "databasechangeloglock")


def as_dict(rule_id):
    rule = RULES[rule_id]
    return {"rule": rule_id, "severity": rule.severity, "dialects": list(rule.dialects),
            "lock": rule.lock, "risk": rule.risk, "fix": rule.fix}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dialect", choices=("postgres", "mysql"),
                        help="only the rules applied to this dialect")
    parser.add_argument("--rule", metavar="ID", help="print the full entry for one rule")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    if args.rule is not None and args.rule not in RULES:
        parser.error("unknown rule %r; run without --rule to list the rule ids" % args.rule)
    chosen = [args.rule] if args.rule else sorted(RULES)
    rules = [as_dict(rule_id) for rule_id in chosen
             if not args.dialect or args.dialect in RULES[rule_id].dialects]
    if args.json:
        print(json.dumps({"rules": rules}, indent=2))
        return 0
    for rule in rules:
        if args.rule:
            print("rule:      %s" % rule["rule"])
            print("severity:  %s" % rule["severity"])
            print("dialects:  %s" % ", ".join(rule["dialects"]))
            print("lock:      %s" % rule["lock"])
            print("risk:      %s" % rule["risk"])
            print("fix:       %s (references/safe-rewrites.md)" % rule["fix"])
        else:
            print("%-32s %-8s %-16s %s" % (
                rule["rule"], rule["severity"], ",".join(rule["dialects"]), rule["fix"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
