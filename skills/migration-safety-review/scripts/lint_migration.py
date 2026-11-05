#!/usr/bin/env python3
"""Lint rendered SQL migrations for lock, data-loss and rollback hazards.

Splits each file into statements (sqlsplit.py), then checks every statement
against the rules in rule_catalog.py. Each finding names the file, the line,
the statement, the lock it takes, the risk, and the name of the safe rewrite
in references/safe-rewrites.md.

PostgreSQL (12 or later) gets the full rule set. MySQL gets only the
data-loss, rename and unbounded UPDATE/DELETE rules; MySQL lock behaviour is
not assessed. rule_catalog.py prints every rule and its dialects.

The input must be SQL. Render ORM migrations first (Django sqlmigrate,
Alembic --sql and so on; see references/render-and-rollback.md).

Examples:
  lint_migration.py migrations/0042_orders.sql
  lint_migration.py --in-transaction db/migrations/*.sql   # runner wraps each file in a transaction
  lint_migration.py --dialect mysql --json V9__rename.sql
  python manage.py sqlmigrate shop 0007 | lint_migration.py -
  lint_migration.py --fail-on warning rendered.sql

Not analysed: the bodies of DO blocks and functions, SQL built at run time,
an UPDATE or DELETE nested inside a WITH clause (reported as a warning, not
checked), table sizes, and whether running application code matches the new
schema. A table created earlier in the same file (without IF NOT EXISTS) is
treated as new and empty, so findings on it are skipped. UPDATE and DELETE on
a migration tool's own version table (alembic_version and similar) are
skipped. Tables are matched by name without the schema. A finding's line is
the line its statement starts on. A clean result is not proof that a
migration is safe.

Exit codes: 0 = no finding at or above --fail-on, 1 = at least one such
finding, 2 = usage or input error.
Standard library only. Reads the files you name. Writes only to stdout and
stderr. Makes no network calls.
"""
import argparse
import json
import re
import sys

from rule_catalog import BOOKKEEPING_TABLES, MYSQL_LOCK, RULES, SERIAL_TYPES, VOLATILE_FUNCTIONS
from sqlsplit import SqlError, has_seq, kw, load_statements, read_name, skip, split_statements, split_top, top

TABLE_PREFIXES = ("GLOBAL", "LOCAL", "TEMP", "TEMPORARY", "UNLOGGED")
MYSQL_DROP_NOT_COLUMN = ("CONSTRAINT", "INDEX", "KEY", "PRIMARY", "FOREIGN", "CHECK", "PARTITION")
MAINTENANCE_WORDS = ("FULL", "FREEZE", "VERBOSE", "ANALYZE", "INDEX", "TABLE", "SCHEMA", "DATABASE",
                     "SYSTEM", "CONCURRENTLY")


def option_on(tokens, name):
    """True if a maintenance option is present and not switched off, as in (FULL false)."""
    for k, tok in enumerate(tokens):
        if tok.kind == "word" and tok.value == name:
            return tokens[k + 1].value.upper() not in ("FALSE", "OFF", "0") if k + 1 < len(tokens) else True
    return False


class Linter(object):
    """Applies the rules to the statements of one file, in order."""

    def __init__(self, dialect, in_transaction):
        self.dialect, self.pg = dialect, dialect == "postgres"
        self.findings = []
        self.new_tables = set()         # created earlier in this file
        self.in_txn = in_transaction
        self.txn_lock_line = None       # first lock-taking DDL in the open transaction
        self.session_timeout, self.txn_timeout = False, None
        self.timeout_reported = False
        self.null_checks = {}           # (table, constraint) -> column of CHECK (column IS NOT NULL)
        self.proven_not_null = set()    # (table, column) covered by a validated constraint

    def report(self, rule, stmt, table=None, lock=None, always=False):
        """Record a finding, unless the rule is for another dialect or the table is new."""
        entry = RULES[rule]
        if self.dialect not in entry.dialects or (table in self.new_tables and not always):
            return
        text = stmt.text if len(stmt.text) <= 160 else stmt.text[:157] + "..."
        self.findings.append({
            "rule": rule, "severity": entry.severity, "line": stmt.line, "table": table,
            "statement": text, "lock": lock or (entry.lock if self.pg else MYSQL_LOCK),
            "risk": entry.risk, "fix": entry.fix})

    def takes_lock(self, stmt, table):
        """Note DDL whose table lock conflicts with normal traffic on an existing table."""
        if not self.pg or table in self.new_tables:
            return
        timeout = self.session_timeout if self.txn_timeout is None else self.txn_timeout
        if not timeout and not self.timeout_reported:
            self.timeout_reported = True
            self.report("missing-lock-timeout", stmt, table)
        if self.in_txn and self.txn_lock_line is None:
            self.txn_lock_line = stmt.line

    def slow_step(self, stmt, table):
        """A scan or data change that runs while this transaction already holds a DDL lock."""
        if self.pg and self.in_txn and self.txn_lock_line is not None:
            self.report("slow-step-under-ddl-lock", stmt, table,
                        lock="the table lock taken at line %d is held until COMMIT" % self.txn_lock_line)

    def set_txn(self, is_open):
        self.in_txn, self.txn_lock_line, self.txn_timeout = is_open, None, None

    def lint(self, stmt):
        t = stmt.tokens
        w = kw(t)
        head, verb, start = w[0], w[0], 0
        if head == "WITH":                          # find the statement that the CTE feeds
            for k, tok in enumerate(t):
                if tok.depth == 0 and tok.kind == "word" and tok.value in (
                        "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"):
                    verb, start = tok.value, k
                    break
            if any(tok.depth > 0 and tok.kind == "word" and tok.value in ("UPDATE", "DELETE")
                   and t[k - 1].value != "FOR" for k, tok in enumerate(t[:start] or t)):
                self.report("write-in-with-not-analysed", stmt)
        if head == "BEGIN" or w[:2] == ["START", "TRANSACTION"]:
            self.set_txn(True)
        elif head in ("COMMIT", "END", "ABORT") or (head == "ROLLBACK" and "TO" not in w):
            self.set_txn("CHAIN" in w and "NO" not in w)    # AND CHAIN opens the next transaction
        elif head == "SET":
            self.set_statement(t, w)
        elif w[:2] == ["ALTER", "TABLE"]:
            self.alter_table(stmt, t)
        elif head == "CREATE":
            self.create(stmt, t, w)
        elif head == "DROP":
            self.drop(stmt, t, w)
        elif head == "TRUNCATE":
            for part in split_top(t[skip(t, 1, "TABLE"):]):
                table = read_name(part, skip(part, 0, "ONLY"))[0]
                self.report("truncate", stmt, table)
                self.takes_lock(stmt, table)
        elif w[:2] == ["RENAME", "TABLE"]:          # MySQL: RENAME TABLE a TO b, c TO d
            for part in split_top(t[2:]):
                self.report("rename-table", stmt, read_name(part, 0)[0])
        elif head in ("VACUUM", "CLUSTER", "REINDEX"):
            self.maintenance(stmt, t, w)
        elif head == "DO":
            self.report("do-block-not-analysed", stmt)
        elif verb in ("INSERT", "UPDATE", "DELETE"):
            self.dml(stmt, t[start:], verb)

    def set_statement(self, t, w):
        """Track SET [SESSION | LOCAL] lock_timeout. A value of 0 switches it off."""
        if not self.pg or "LOCK_TIMEOUT" not in w:
            return
        is_on = not re.match(r"^(0[a-z]*|default)$", t[-1].value.strip().lower())
        if "LOCAL" not in w[:2]:
            self.session_timeout, self.txn_timeout = is_on, None
        elif self.in_txn:                           # SET LOCAL does nothing outside a transaction
            self.txn_timeout = is_on

    def alter_table(self, stmt, t):
        table, k = read_name(t, skip(t, 2, "IF", "EXISTS", "ONLY"))
        if table is None:
            return
        if k < len(t) and t[k].value == "*":
            k += 1
        actions = split_top(t[k:])
        verbs = [kw(action)[0] for action in actions]
        if "VALIDATE" in verbs and any(verb != "VALIDATE" for verb in verbs):
            self.report("slow-step-under-ddl-lock", stmt, table,
                        lock="one ALTER TABLE holds the strongest lock any of its actions needs, "
                             "so the validation scan runs under it")
        elif "VALIDATE" in verbs:
            self.slow_step(stmt, table)
        for action in actions:
            self.alter_action(stmt, table, action, kw(action))
        if any(verb != "VALIDATE" for verb in verbs):   # VALIDATE alone does not block reads or writes
            self.takes_lock(stmt, table)

    def alter_action(self, stmt, table, act, w):
        verb = w[0]
        if verb == "RENAME":
            if w[1:2] == ["CONSTRAINT"] or (not self.pg and w[1:2] in (["INDEX"], ["KEY"])):
                return
            is_column = w[1:2] == ["COLUMN"] or (self.pg and w[1:2] != ["TO"])
            self.report("rename-column" if is_column else "rename-table", stmt, table)
        elif verb == "CHANGE" and not self.pg:      # MySQL: CHANGE [COLUMN] old new definition
            k = skip(act, 1, "COLUMN")
            if len(act) > k + 1 and act[k].name != act[k + 1].name:
                self.report("rename-column", stmt, table)
        elif verb == "DROP":
            not_column = ("CONSTRAINT",) if self.pg else MYSQL_DROP_NOT_COLUMN
            if len(act) > 1 and w[1] not in not_column:
                self.report("drop-column", stmt, table)
        elif not self.pg:
            return
        elif verb == "VALIDATE" and len(act) > 2:
            column = self.null_checks.get((table, act[2].name))
            if column:
                self.proven_not_null.add((table, column))
        elif verb == "ALTER" and w[1:2] != ["CONSTRAINT"]:
            k = skip(act, 1, "COLUMN")              # act[k] is the column, which may be named "type"
            column = act[k].name if k < len(act) else None
            tail = w[k + 1:]
            if tail[:1] == ["TYPE"] or tail[:3] == ["SET", "DATA", "TYPE"]:
                self.report("alter-column-type", stmt, table)
            elif tail[:3] == ["SET", "NOT", "NULL"] and (table, column) not in self.proven_not_null:
                self.report("set-not-null", stmt, table)
        elif verb == "ADD":
            self.add_action(stmt, table, act, w)

    def add_action(self, stmt, table, act, w):
        k, constraint = 1, None
        if w[1:2] == ["CONSTRAINT"] and len(act) > 3:
            constraint, k = act[2].name, 3
        kind = w[k] if k < len(w) else ""
        if kind in ("CHECK", "FOREIGN", "UNIQUE", "PRIMARY", "EXCLUDE"):
            self.add_constraint(stmt, table, act, k, kind, constraint)
        elif constraint is None:
            self.add_column(stmt, table, act)

    def add_constraint(self, stmt, table, act, k, kind, constraint):
        not_valid = has_seq(act, "NOT", "VALID")
        if kind == "CHECK":
            inner = [x for x in act[k + 1:] if x.depth > 0 and x.kind != "punct"]
            if constraint and len(inner) == 4 and kw(inner)[1:] == ["IS", "NOT", "NULL"]:
                self.null_checks[(table, constraint)] = inner[0].name
                if not not_valid:
                    self.proven_not_null.add((table, inner[0].name))
            if not not_valid:
                self.report("add-check-validates", stmt, table)
        elif kind == "FOREIGN" and not not_valid:
            self.report("add-foreign-key-validates", stmt, table)
        elif kind in ("UNIQUE", "PRIMARY") and not has_seq(act, "USING", "INDEX"):
            self.report("add-unique-builds-index", stmt, table)

    def add_column(self, stmt, table, act):
        tail = act[skip(act, 1, "COLUMN", "IF", "NOT", "EXISTS") + 1:]   # after the column name
        tw = kw(top(tail))
        has_default = volatile = False
        for tok in tail:
            if tok.kind == "word" and tok.depth == 0 and tok.value == "DEFAULT":
                has_default = True
            elif tok.kind == "word" and has_default and tok.value in VOLATILE_FUNCTIONS:
                volatile = True
        serial = bool(tw) and tw[0] in SERIAL_TYPES
        stored = "GENERATED" in tw and ("IDENTITY" in tw or "STORED" in tw)
        if serial or volatile or stored:
            self.report("add-column-table-rewrite", stmt, table)
        elif has_seq(tail, "NOT", "NULL") and not has_default and "GENERATED" not in tw:
            self.report("add-column-not-null-no-default", stmt, table)
        if "REFERENCES" in tw:                      # a column constraint cannot be NOT VALID
            self.report("add-foreign-key-validates", stmt, table,
                        lock="ACCESS EXCLUSIVE on this table (ADD COLUMN) and SHARE ROW EXCLUSIVE "
                             "on the referenced table")
        if "UNIQUE" in tw or has_seq(tail, "PRIMARY", "KEY"):
            self.report("add-unique-builds-index", stmt, table)

    def create(self, stmt, t, w):
        if "INDEX" in w[1:3] and self.pg:           # CREATE [UNIQUE] INDEX [CONCURRENTLY] ... ON table
            concurrently = w[w.index("INDEX") + 1:][:1] == ["CONCURRENTLY"]
            on = [k for k, tok in enumerate(t) if tok.depth == 0 and tok.kind == "word" and tok.value == "ON"]
            table = read_name(t, skip(t, on[0] + 1, "ONLY"))[0] if on else None
            if not concurrently:
                self.report("create-index-not-concurrent", stmt, table)
                self.takes_lock(stmt, table)
            elif self.in_txn:
                self.report("concurrently-in-transaction", stmt, table, always=True)
        elif "TABLE" in w[1:4]:
            k = w.index("TABLE")
            name = read_name(t, k + 1)[0]           # IF NOT EXISTS: the table may already hold data
            if all(x in TABLE_PREFIXES for x in w[1:k]) and name and w[k + 1:k + 2] != ["IF"]:
                self.new_tables.add(name)

    def drop(self, stmt, t, w):
        if w[1:2] == ["TABLE"]:
            for part in split_top(t[skip(t, 2, "IF", "EXISTS"):]):
                table = read_name(part, 0)[0]
                self.report("drop-table", stmt, table)
                self.takes_lock(stmt, table)
                self.new_tables.discard(table)
        elif w[1:2] in (["SCHEMA"], ["DATABASE"]):
            self.report("drop-schema", stmt)
        elif w[1:2] == ["INDEX"] and self.pg:
            if w[2:3] != ["CONCURRENTLY"]:
                self.report("drop-index-not-concurrent", stmt)
                self.takes_lock(stmt, None)
            elif self.in_txn:
                self.report("concurrently-in-transaction", stmt, always=True)

    def maintenance(self, stmt, t, w):
        k = 1
        if len(t) > 1 and t[1].kind == "punct" and t[1].value == "(":   # e.g. VACUUM (FULL, ANALYZE) t
            k = 2
            while k < len(t) and t[k].depth > 0:
                k += 1
            k += 1                                  # the closing parenthesis
        table = read_name(t, skip(t, k, *MAINTENANCE_WORDS))[0]
        if w[0] == "REINDEX":
            table = table if "TABLE" in w else None     # otherwise the name is an index or schema
            if not option_on(t, "CONCURRENTLY"):
                self.report("reindex-not-concurrent", stmt, table)
                self.takes_lock(stmt, table)
            elif self.in_txn:
                self.report("concurrently-in-transaction", stmt, table, always=True)
        elif w[0] == "CLUSTER" or option_on(t, "FULL"):
            if w[0] == "CLUSTER" and "ON" in w:         # old form: CLUSTER index ON table
                table = read_name(t, w.index("ON") + 1)[0]
            self.report("cluster" if w[0] == "CLUSTER" else "vacuum-full", stmt, table)
            self.takes_lock(stmt, table)

    def dml(self, stmt, t, verb):
        top_words = kw(top(t))
        if verb == "INSERT":
            if "SELECT" in top_words:
                self.slow_step(stmt, None)
            return
        if verb == "UPDATE":
            k = skip(t, 1, "ONLY", "LOW_PRIORITY", "IGNORE")
        else:
            k = next((j + 1 for j, tok in enumerate(t)
                      if tok.depth == 0 and tok.kind == "word" and tok.value == "FROM"), len(t))
            k = skip(t, k, "ONLY")
        table = read_name(t, k)[0]
        if table in BOOKKEEPING_TABLES:             # the migration tool's own version row
            return
        if "WHERE" not in top_words and "LIMIT" not in top_words:   # MySQL allows LIMIT here
            self.report("update-without-where" if verb == "UPDATE" else "delete-without-where", stmt, table)
        if table not in self.new_tables:
            self.slow_step(stmt, table)


def lint_statements(statements, dialect="postgres", in_transaction=False):
    """Return the findings for one file's statements, in line order."""
    linter = Linter(dialect, in_transaction)
    for stmt in statements:
        linter.lint(stmt)
    return sorted(linter.findings, key=lambda finding: finding["line"])


def lint_sql(text, dialect="postgres", in_transaction=False):
    """Split one SQL text and return its findings. Raises SqlError."""
    return lint_statements(split_statements(text, dialect), dialect, in_transaction)


def print_text(report):
    if report["dialect"] == "mysql":
        print("note: MySQL gets the data-loss, rename and unbounded UPDATE/DELETE rules only. "
              "Lock behaviour is not assessed.")
    for entry in report["files"]:
        print("%s: %s, %d statement(s), %d finding(s)" % (
            entry["file"], report["dialect"], entry["statements"], len(entry["findings"])))
        for finding in entry["findings"]:
            table = "  table: %s" % finding["table"] if finding["table"] else ""
            print("  line %d  %s  %s%s" % (
                finding["line"], finding["severity"].upper(), finding["rule"], table))
            print("      statement: " + finding["statement"])
            print("      lock:      " + finding["lock"])
            print("      risk:      " + finding["risk"])
            print("      fix:       %s (references/safe-rewrites.md)" % finding["fix"])
    summary = report["summary"]
    counts = "%d error(s), %d warning(s)" % (summary["errors"], summary["warnings"])
    if summary["blocking"]:
        print("BLOCKING: %s. Do not run this migration as written." % counts)
    elif summary["errors"] or summary["warnings"]:
        print("NOT BLOCKING at --fail-on %s: %s. Review each finding." % (report["fail_on"], counts))
    else:
        print("NO FINDINGS. This is not proof of safety: confirm table sizes, deploy order "
              "and the rollback plan.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", metavar="FILE",
                        help="rendered SQL file; use - to read standard input")
    parser.add_argument("--dialect", choices=("postgres", "mysql"), default="postgres",
                        help="SQL dialect (default postgres)")
    parser.add_argument("--in-transaction", action="store_true",
                        help="the migration runner wraps each file in a transaction, "
                             "even though the file has no BEGIN")
    parser.add_argument("--fail-on", choices=("error", "warning", "never"), default="error",
                        help="lowest severity that gives exit code 1 (default error)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    report = {"dialect": args.dialect, "fail_on": args.fail_on,
              "in_transaction": args.in_transaction, "files": []}
    for path in args.files:
        try:
            name, statements = load_statements(path, args.dialect)
        except SqlError as exc:
            print("lint_migration.py: error: %s" % exc, file=sys.stderr)
            return 2
        report["files"].append({
            "file": name, "statements": len(statements),
            "findings": lint_statements(statements, args.dialect, args.in_transaction)})

    found = [f["severity"] for entry in report["files"] for f in entry["findings"]]
    errors, warnings = found.count("error"), found.count("warning")
    blocking = bool(errors) if args.fail_on == "error" else bool(found) and args.fail_on == "warning"
    report["summary"] = {"errors": errors, "warnings": warnings, "blocking": blocking}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_text(report)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
