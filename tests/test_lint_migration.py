import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "migration-safety-review"
sys.path.insert(0, str(SKILL / "scripts"))

import lint_migration  # noqa: E402
import sqlsplit  # noqa: E402

FILES = SKILL / "evals" / "files"


def rules(sql, dialect="postgres", in_transaction=False):
    """Rule ids reported for a SQL text, in line order."""
    return [f["rule"] for f in lint_migration.lint_sql(sql, dialect, in_transaction)]


def with_timeout(sql):
    """Prefix a lock_timeout so only the rule under test can fire."""
    return "SET lock_timeout = '5s';\n" + sql


def fixture_findings(relative, dialect="postgres"):
    text = (FILES / relative).read_text(encoding="utf-8")
    return [(f["line"], f["rule"]) for f in lint_migration.lint_sql(text, dialect)]


def run_cli(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = lint_migration.main(argv)
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


class IndexRuleTests(unittest.TestCase):
    def test_plain_create_index_is_reported(self):
        self.assertEqual(rules(with_timeout("CREATE INDEX i ON t (a);")), ["create-index-not-concurrent"])
        self.assertEqual(rules(with_timeout("CREATE UNIQUE INDEX IF NOT EXISTS i ON ONLY s.t (a);")),
                         ["create-index-not-concurrent"])
        self.assertEqual(rules(with_timeout("CREATE INDEX ON t (a);")), ["create-index-not-concurrent"])

    def test_concurrent_create_index_outside_a_transaction_is_clean(self):
        self.assertEqual(rules("CREATE INDEX CONCURRENTLY i ON t (a);"), [])
        self.assertEqual(rules("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a);"), [])

    def test_concurrently_inside_a_transaction_is_reported(self):
        self.assertEqual(rules("BEGIN; CREATE INDEX CONCURRENTLY i ON t (a); COMMIT;"),
                         ["concurrently-in-transaction"])
        self.assertEqual(rules("START TRANSACTION; DROP INDEX CONCURRENTLY i; COMMIT;"),
                         ["concurrently-in-transaction"])
        self.assertEqual(rules("BEGIN; REINDEX INDEX CONCURRENTLY i; COMMIT;"),
                         ["concurrently-in-transaction"])

    def test_in_transaction_flag_treats_the_file_as_one_transaction(self):
        sql = "CREATE INDEX CONCURRENTLY i ON t (a);"
        self.assertEqual(rules(sql, in_transaction=True), ["concurrently-in-transaction"])
        # An explicit COMMIT ends the runner's transaction, which is the usual workaround.
        self.assertEqual(rules("COMMIT; " + sql, in_transaction=True), [])

    def test_transaction_ends_at_commit_but_not_at_rollback_to_savepoint(self):
        self.assertEqual(rules("BEGIN; SELECT 1; COMMIT; CREATE INDEX CONCURRENTLY i ON t (a);"), [])
        self.assertEqual(
            rules("BEGIN; SAVEPOINT s; ROLLBACK TO SAVEPOINT s; CREATE INDEX CONCURRENTLY i ON t (a);"),
            ["concurrently-in-transaction"])

    def test_commit_and_chain_keeps_a_transaction_open(self):
        sql = "BEGIN; SELECT 1; COMMIT AND CHAIN; CREATE INDEX CONCURRENTLY i ON t (a); COMMIT;"
        self.assertEqual(rules(sql), ["concurrently-in-transaction"])
        sql = "BEGIN; SELECT 1; COMMIT AND NO CHAIN; CREATE INDEX CONCURRENTLY i ON t (a);"
        self.assertEqual(rules(sql), [])

    def test_drop_and_reindex_without_concurrently(self):
        self.assertEqual(rules(with_timeout("DROP INDEX i;")), ["drop-index-not-concurrent"])
        self.assertEqual(rules("DROP INDEX CONCURRENTLY IF EXISTS i;"), [])
        self.assertEqual(rules(with_timeout("REINDEX TABLE t;")), ["reindex-not-concurrent"])
        self.assertEqual(rules(with_timeout("REINDEX INDEX i;")), ["reindex-not-concurrent"])
        self.assertEqual(rules("REINDEX (VERBOSE) TABLE CONCURRENTLY t;"), [])
        self.assertEqual(rules("REINDEX (CONCURRENTLY) TABLE t;"), [])
        self.assertEqual(rules(with_timeout("REINDEX (CONCURRENTLY false) TABLE t;")), ["reindex-not-concurrent"])


class ColumnRuleTests(unittest.TestCase):
    def test_not_null_column_without_default_is_reported(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD COLUMN c integer NOT NULL;")),
                         ["add-column-not-null-no-default"])
        self.assertEqual(rules(with_timeout('ALTER TABLE t ADD "c" varchar(20) NOT NULL;')),
                         ["add-column-not-null-no-default"])

    def test_nullable_or_constant_default_column_is_clean(self):
        for sql in (
            "ALTER TABLE t ADD COLUMN c integer;",
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS c integer NULL;",
            "ALTER TABLE t ADD COLUMN c integer NOT NULL DEFAULT 0;",
            "ALTER TABLE t ADD COLUMN c timestamptz NOT NULL DEFAULT now();",
            "ALTER TABLE t ADD COLUMN c integer CHECK (c IS NOT NULL);",
            "ALTER TABLE t ADD COLUMN generated boolean DEFAULT false;",
        ):
            self.assertEqual(rules(with_timeout(sql)), [], sql)

    def test_columns_that_force_a_table_rewrite_are_reported(self):
        for sql in (
            "ALTER TABLE t ADD COLUMN c bigserial;",
            "ALTER TABLE t ADD COLUMN c uuid NOT NULL DEFAULT gen_random_uuid();",
            "ALTER TABLE t ADD COLUMN c timestamptz DEFAULT clock_timestamp();",
            "ALTER TABLE t ADD COLUMN c bigint GENERATED ALWAYS AS IDENTITY;",
            "ALTER TABLE t ADD COLUMN c bigint GENERATED BY DEFAULT AS IDENTITY;",
            "ALTER TABLE t ADD COLUMN c integer GENERATED ALWAYS AS (a + b) STORED;",
        ):
            self.assertEqual(rules(with_timeout(sql)), ["add-column-table-rewrite"], sql)

    def test_inline_foreign_key_and_unique_on_a_new_column_are_reported(self):
        findings = lint_migration.lint_sql(with_timeout("ALTER TABLE t ADD COLUMN u_id bigint REFERENCES u (id);"))
        self.assertEqual([f["rule"] for f in findings], ["add-foreign-key-validates"])
        self.assertIn("SHARE ROW EXCLUSIVE on the referenced table", findings[0]["lock"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD COLUMN code text UNIQUE;")),
                         ["add-unique-builds-index"])

    def test_alter_column_type_is_reported_even_for_a_column_named_type(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t ALTER COLUMN c TYPE bigint;")), ["alter-column-type"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ALTER c SET DATA TYPE text USING c::text;")),
                         ["alter-column-type"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ALTER COLUMN type TYPE text;")), ["alter-column-type"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ALTER COLUMN type SET DEFAULT 'x';")), [])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ALTER COLUMN type DROP NOT NULL;")), [])

    def test_set_not_null_is_reported(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE ONLY public.t ALTER COLUMN c SET NOT NULL;")),
                         ["set-not-null"])

    def test_set_not_null_is_clean_after_a_validated_not_null_check(self):
        proven = with_timeout(
            "ALTER TABLE t ADD CONSTRAINT t_c_nn CHECK (c IS NOT NULL) NOT VALID;"
            "ALTER TABLE t VALIDATE CONSTRAINT t_c_nn;"
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL;")
        self.assertEqual(rules(proven), [])

    def test_set_not_null_needs_the_check_to_be_validated_and_for_the_same_column(self):
        not_validated = with_timeout(
            "ALTER TABLE t ADD CONSTRAINT t_c_nn CHECK (c IS NOT NULL) NOT VALID;"
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL;")
        self.assertEqual(rules(not_validated), ["set-not-null"])
        other_column = with_timeout(
            "ALTER TABLE t ADD CONSTRAINT t_d_nn CHECK (d IS NOT NULL) NOT VALID;"
            "ALTER TABLE t VALIDATE CONSTRAINT t_d_nn;"
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL;")
        self.assertEqual(rules(other_column), ["set-not-null"])
        other_table = with_timeout(
            "ALTER TABLE u ADD CONSTRAINT c_nn CHECK (c IS NOT NULL) NOT VALID;"
            "ALTER TABLE u VALIDATE CONSTRAINT c_nn;"
            "ALTER TABLE t ALTER COLUMN c SET NOT NULL;")
        self.assertEqual(rules(other_table), ["set-not-null"])

    def test_several_actions_in_one_alter_table_are_each_checked(self):
        sql = with_timeout("ALTER TABLE t ADD COLUMN p numeric(10, 2) NOT NULL, DROP COLUMN q, "
                           "ALTER COLUMN r SET NOT NULL;")
        self.assertEqual(rules(sql), ["add-column-not-null-no-default", "drop-column", "set-not-null"])


class ConstraintRuleTests(unittest.TestCase):
    def test_foreign_key_and_check_without_not_valid_are_reported(self):
        self.assertEqual(
            rules(with_timeout("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (u_id) REFERENCES u (id);")),
            ["add-foreign-key-validates"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD FOREIGN KEY (u_id) REFERENCES u;")),
                         ["add-foreign-key-validates"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD CONSTRAINT ck CHECK (a > 0);")),
                         ["add-check-validates"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD CHECK (a > 0);")), ["add-check-validates"])

    def test_not_valid_then_validate_is_clean_outside_a_transaction(self):
        sql = with_timeout(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (u_id) REFERENCES u (id) NOT VALID;"
            "ALTER TABLE t VALIDATE CONSTRAINT fk;"
            "ALTER TABLE t ADD CONSTRAINT ck CHECK (a > 0) NOT VALID;"
            "ALTER TABLE t VALIDATE CONSTRAINT ck;")
        self.assertEqual(rules(sql), [])

    def test_validate_in_the_transaction_that_added_the_constraint_is_reported(self):
        sql = ("BEGIN; SET LOCAL lock_timeout = '5s';"
               "ALTER TABLE t ADD CONSTRAINT ck CHECK (a > 0) NOT VALID;"
               "ALTER TABLE t VALIDATE CONSTRAINT ck; COMMIT;")
        findings = lint_migration.lint_sql(sql)
        self.assertEqual([f["rule"] for f in findings], ["slow-step-under-ddl-lock"])
        self.assertEqual(findings[0]["severity"], "warning")

    def test_validate_in_the_same_alter_table_as_the_add_is_reported(self):
        sql = with_timeout("ALTER TABLE t ADD CONSTRAINT ck CHECK (a > 0) NOT VALID, VALIDATE CONSTRAINT ck;")
        findings = lint_migration.lint_sql(sql)
        self.assertEqual([f["rule"] for f in findings], ["slow-step-under-ddl-lock"])
        self.assertIn("strongest lock", findings[0]["lock"])

    def test_unique_and_primary_key_built_inline_are_reported(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD CONSTRAINT uq UNIQUE (a);")),
                         ["add-unique-builds-index"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD PRIMARY KEY (id);")), ["add-unique-builds-index"])

    def test_unique_and_primary_key_using_an_existing_index_are_clean(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD CONSTRAINT uq UNIQUE USING INDEX uq_idx;")), [])
        self.assertEqual(rules(with_timeout("ALTER TABLE t ADD CONSTRAINT pk PRIMARY KEY USING INDEX pk_idx;")), [])

    def test_dropping_or_renaming_a_constraint_is_clean(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t DROP CONSTRAINT ck;")), [])
        self.assertEqual(rules(with_timeout("ALTER TABLE t RENAME CONSTRAINT a TO b;")), [])


class DestructiveRuleTests(unittest.TestCase):
    def test_drop_table_reports_each_table(self):
        findings = lint_migration.lint_sql(with_timeout("DROP TABLE IF EXISTS a, s.b CASCADE;"))
        self.assertEqual([(f["rule"], f["table"]) for f in findings], [("drop-table", "a"), ("drop-table", "b")])

    def test_drop_column_truncate_and_drop_schema(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t DROP COLUMN IF EXISTS c;")), ["drop-column"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t DROP c CASCADE;")), ["drop-column"])
        self.assertEqual(rules(with_timeout("TRUNCATE TABLE a, b RESTART IDENTITY;")), ["truncate", "truncate"])
        self.assertEqual(rules("DROP SCHEMA legacy CASCADE;"), ["drop-schema"])

    def test_renames(self):
        self.assertEqual(rules(with_timeout("ALTER TABLE t RENAME TO t2;")), ["rename-table"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t RENAME COLUMN a TO b;")), ["rename-column"])
        self.assertEqual(rules(with_timeout("ALTER TABLE t RENAME a TO b;")), ["rename-column"])
        self.assertEqual(rules("ALTER INDEX i RENAME TO j;"), [])

    def test_update_and_delete_without_where(self):
        self.assertEqual(rules("UPDATE t SET a = 1;"), ["update-without-where"])
        self.assertEqual(rules("DELETE FROM ONLY t;"), ["delete-without-where"])
        self.assertEqual(rules("UPDATE t SET a = (SELECT b FROM u WHERE u.id = t.id);"), ["update-without-where"])
        self.assertEqual(rules("WITH x AS (SELECT 1 WHERE true) UPDATE t SET a = 1;"), ["update-without-where"])

    def test_update_and_delete_with_where_are_clean(self):
        for sql in (
            "UPDATE t SET a = 1 WHERE id < 1000;",
            "UPDATE t SET a = u.b FROM u WHERE u.id = t.id;",
            "DELETE FROM t USING u WHERE u.id = t.id;",
            "WITH old AS (SELECT id FROM t LIMIT 10) DELETE FROM t WHERE id IN (SELECT id FROM old);",
            "INSERT INTO t (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2;",
            "SELECT * FROM t FOR UPDATE;",
        ):
            self.assertEqual(rules(sql), [], sql)

    def test_maintenance_commands(self):
        self.assertEqual(rules(with_timeout("VACUUM FULL t;")), ["vacuum-full"])
        self.assertEqual(rules(with_timeout("VACUUM (FULL, ANALYZE) t;")), ["vacuum-full"])
        self.assertEqual(rules("VACUUM (ANALYZE) t; VACUUM t; ANALYZE t; VACUUM (FULL false) t;"), [])
        self.assertEqual(rules(with_timeout("VACUUM (VERBOSE, FULL true) t;")), ["vacuum-full"])
        old_form = lint_migration.lint_sql(with_timeout("CLUSTER t_pkey ON t;"))
        self.assertEqual([(f["rule"], f["table"]) for f in old_form], [("cluster", "t")])
        findings = lint_migration.lint_sql(with_timeout("CLUSTER t USING t_pkey;"))
        self.assertEqual([(f["rule"], f["table"]) for f in findings], [("cluster", "t")])


class ContextRuleTests(unittest.TestCase):
    def test_tables_created_in_the_same_file_are_exempt(self):
        sql = ("CREATE TABLE fresh (id bigint, a int);"
               "CREATE INDEX fresh_a ON fresh (a);"
               "ALTER TABLE fresh ADD CONSTRAINT ck CHECK (a > 0);"
               "UPDATE fresh SET a = 1;"
               "DROP TABLE fresh;")
        self.assertEqual(rules(sql), [])

    def test_create_table_if_not_exists_is_not_treated_as_new(self):
        sql = with_timeout("CREATE TABLE IF NOT EXISTS maybe (id bigint); CREATE INDEX maybe_id ON maybe (id);")
        self.assertEqual(rules(sql), ["create-index-not-concurrent"])

    def test_missing_lock_timeout_is_reported_once_at_the_first_ddl(self):
        findings = lint_migration.lint_sql("SELECT 1;\nALTER TABLE t ADD COLUMN a int;\nALTER TABLE t ADD COLUMN b int;")
        self.assertEqual([(f["line"], f["rule"], f["severity"]) for f in findings],
                         [(2, "missing-lock-timeout", "warning")])

    def test_lock_timeout_forms(self):
        ddl = "ALTER TABLE t ADD COLUMN a int;"
        self.assertEqual(rules("SET lock_timeout TO '2s';" + ddl), [])
        self.assertEqual(rules("SET SESSION lock_timeout = 3000;" + ddl), [])
        self.assertEqual(rules("BEGIN; SET LOCAL lock_timeout = '2s';" + ddl + "COMMIT;"), [])
        # Zero disables the timeout; SET LOCAL does nothing outside a transaction and ends at COMMIT.
        self.assertEqual(rules("SET lock_timeout = 0;" + ddl), ["missing-lock-timeout"])
        self.assertEqual(rules("SET LOCAL lock_timeout = '2s';" + ddl), ["missing-lock-timeout"])
        self.assertEqual(rules("BEGIN; SET LOCAL lock_timeout = '2s'; COMMIT;" + ddl), ["missing-lock-timeout"])
        self.assertEqual(rules("SET statement_timeout = '2s';" + ddl), ["missing-lock-timeout"])

    def test_validate_and_concurrent_index_do_not_need_a_lock_timeout(self):
        self.assertEqual(rules("ALTER TABLE t VALIDATE CONSTRAINT ck; CREATE INDEX CONCURRENTLY i ON t (a);"), [])

    def test_backfill_after_ddl_in_one_transaction_is_reported(self):
        sql = ("BEGIN;\nSET LOCAL lock_timeout = '5s';\nALTER TABLE t ADD COLUMN a int;\n"
               "UPDATE t SET a = 0 WHERE a IS NULL;\nINSERT INTO audit SELECT id FROM t;\nCOMMIT;")
        findings = lint_migration.lint_sql(sql)
        self.assertEqual([(f["line"], f["rule"]) for f in findings],
                         [(4, "slow-step-under-ddl-lock"), (5, "slow-step-under-ddl-lock")])
        self.assertEqual(findings[0]["lock"], "the table lock taken at line 3 is held until COMMIT")

    def test_backfill_outside_the_ddl_transaction_is_clean(self):
        sql = with_timeout("BEGIN; ALTER TABLE t ADD COLUMN a int; COMMIT;"
                           "UPDATE t SET a = 0 WHERE id BETWEEN 1 AND 1000;")
        self.assertEqual(rules(sql), [])
        before = with_timeout("BEGIN; UPDATE t SET a = 0 WHERE id < 10; ALTER TABLE t ADD COLUMN b int; COMMIT;")
        self.assertEqual(rules(before), [])

    def test_update_or_delete_inside_a_with_clause_is_reported_as_not_analysed(self):
        sql = "WITH gone AS (DELETE FROM t RETURNING *) INSERT INTO archive SELECT * FROM gone;"
        findings = lint_migration.lint_sql(sql)
        self.assertEqual([(f["rule"], f["severity"]) for f in findings],
                         [("write-in-with-not-analysed", "warning")])
        self.assertEqual(rules("WITH gone AS (UPDATE t SET a = 1 RETURNING id) SELECT count(*) FROM gone;"),
                         ["write-in-with-not-analysed"])
        locked = "WITH pick AS (SELECT id FROM t WHERE a = 1 FOR UPDATE) UPDATE t SET a = 2 WHERE id IN (SELECT id FROM pick);"
        self.assertEqual(rules(locked), [])

    def test_migration_tool_version_tables_are_not_findings(self):
        sql = ("BEGIN;\nSET LOCAL lock_timeout = '5s';\nALTER TABLE account ADD COLUMN note text;\n"
               "UPDATE alembic_version SET version_num='b2' WHERE alembic_version.version_num = 'a1';\n"
               "DELETE FROM schema_migrations WHERE version = '20261104';\nCOMMIT;")
        self.assertEqual(rules(sql), [])
        self.assertEqual(rules("DELETE FROM alembic_version;"), [])

    def test_do_block_is_reported_as_not_analysed(self):
        findings = lint_migration.lint_sql("DO $$ BEGIN ALTER TABLE t ADD COLUMN a int; END $$;")
        self.assertEqual([(f["rule"], f["severity"]) for f in findings], [("do-block-not-analysed", "warning")])

    def test_every_finding_carries_its_evidence(self):
        text = (FILES / "backfill" / "0060_users_status.sql").read_text(encoding="utf-8")
        for finding in lint_migration.lint_sql(text):
            for field in ("rule", "severity", "line", "statement", "lock", "risk", "fix"):
                self.assertTrue(finding[field], field)

    def test_long_statements_are_truncated_in_the_finding(self):
        sql = "UPDATE t SET a = '%s';" % ("x" * 400)
        self.assertEqual(len(lint_migration.lint_sql(sql)[0]["statement"]), 160)

    def test_truncated_statements_never_raise(self):
        for path in sorted(FILES.rglob("*.sql")):
            dialect = "mysql" if path.parent.name == "mysql" else "postgres"
            for stmt in sqlsplit.split_statements(path.read_text(encoding="utf-8"), dialect):
                for cut in range(1, len(stmt.tokens) + 1):
                    part = sqlsplit.Statement(stmt.line, stmt.text, stmt.tokens[:cut])
                    lint_migration.lint_statements([part], dialect)
                    lint_migration.lint_statements([part], dialect, in_transaction=True)


class MysqlTests(unittest.TestCase):
    def test_only_the_dialect_neutral_rules_apply(self):
        for sql in (
            "CREATE INDEX i ON t (a);",
            "ALTER TABLE t ADD COLUMN c INT NOT NULL;",
            "ALTER TABLE t MODIFY c BIGINT NOT NULL;",
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (u_id) REFERENCES u (id);",
            "ALTER TABLE t DROP PRIMARY KEY, DROP FOREIGN KEY fk, DROP INDEX i, DROP KEY k;",
            "ALTER TABLE t RENAME INDEX a TO b;",
            "ALTER TABLE t CHANGE c c BIGINT;",
            "DROP TEMPORARY TABLE scratch;",
        ):
            self.assertEqual(rules(sql, "mysql"), [], sql)

    def test_data_loss_and_rename_rules(self):
        self.assertEqual(rules("ALTER TABLE t DROP c;", "mysql"), ["drop-column"])
        self.assertEqual(rules("ALTER TABLE t CHANGE COLUMN old_name new_name INT;", "mysql"), ["rename-column"])
        self.assertEqual(rules("ALTER TABLE t RENAME COLUMN a TO b;", "mysql"), ["rename-column"])
        self.assertEqual(rules("ALTER TABLE t RENAME TO u;", "mysql"), ["rename-table"])
        self.assertEqual(rules("ALTER TABLE t RENAME AS u;", "mysql"), ["rename-table"])
        self.assertEqual(rules("RENAME TABLE a TO b, c TO d;", "mysql"), ["rename-table", "rename-table"])
        self.assertEqual(rules("DROP TABLE IF EXISTS a;", "mysql"), ["drop-table"])
        self.assertEqual(rules("TRUNCATE TABLE a;", "mysql"), ["truncate"])
        self.assertEqual(rules("DROP DATABASE shop;", "mysql"), ["drop-schema"])
        self.assertEqual(rules("DELETE FROM a;", "mysql"), ["delete-without-where"])
        self.assertEqual(rules("UPDATE LOW_PRIORITY a SET b = 1;", "mysql"), ["update-without-where"])
        self.assertEqual(rules("UPDATE a JOIN b ON a.id = b.id SET a.x = b.x;", "mysql"), ["update-without-where"])
        self.assertEqual(rules("DELETE a FROM a JOIN b ON a.id = b.id;", "mysql"), ["delete-without-where"])

    def test_statements_bounded_by_where_or_limit_are_clean(self):
        self.assertEqual(rules("DELETE FROM a ORDER BY id LIMIT 1000;", "mysql"), [])
        self.assertEqual(rules("UPDATE `a` SET b = 1 WHERE id = 5;", "mysql"), [])
        self.assertEqual(rules("UPDATE a SET b = 5--1 WHERE id = 1;", "mysql"), [])

    def test_lock_is_reported_as_not_assessed(self):
        finding = lint_migration.lint_sql("DROP TABLE a;", "mysql")[0]
        self.assertTrue(finding["lock"].startswith("not assessed for MySQL"))


class FixtureTests(unittest.TestCase):
    def test_lock_heavy_fixture_reports_every_planted_defect(self):
        self.assertEqual(fixture_findings("lock-heavy/0042_orders_constraints.sql"), [
            (6, "create-index-not-concurrent"),
            (6, "missing-lock-timeout"),
            (8, "add-foreign-key-validates"),
            (12, "add-check-validates"),
            (15, "set-not-null"),
            (17, "add-unique-builds-index"),
        ])

    def test_backfill_fixture_reports_every_planted_defect(self):
        self.assertEqual(fixture_findings("backfill/0060_users_status.sql"), [
            (5, "missing-lock-timeout"),
            (7, "update-without-where"),
            (7, "slow-step-under-ddl-lock"),
            (9, "set-not-null"),
            (11, "concurrently-in-transaction"),
        ])

    def test_destructive_fixture_reports_every_planted_defect(self):
        self.assertEqual(fixture_findings("destructive/0051_drop_legacy_billing.sql"), [
            (5, "drop-column"),
            (7, "rename-column"),
            (9, "alter-column-type"),
            (11, "drop-table"),
        ])

    def test_destructive_down_fixture(self):
        self.assertEqual(fixture_findings("destructive/0051_drop_legacy_billing.down.sql"), [
            (12, "alter-column-type"),
            (14, "rename-column"),
        ])

    def test_django_rendered_fixture_reports_the_plain_index(self):
        self.assertEqual(fixture_findings("django-add-index/0007_order_region.sql"), [
            (8, "missing-lock-timeout"),
            (12, "create-index-not-concurrent"),
        ])

    def test_mysql_fixture_reports_every_planted_defect(self):
        self.assertEqual(fixture_findings("mysql/0009_rename_and_purge.sql", "mysql"), [
            (4, "rename-column"),
            (6, "drop-column"),
            (8, "rename-table"),
            (10, "delete-without-where"),
        ])

    def test_safe_fixtures_report_nothing(self):
        self.assertEqual(fixture_findings("safe/0042_orders_constraints_safe.sql"), [])
        self.assertEqual(fixture_findings("safe/quoting_traps.sql"), [])

    def test_safe_migration_is_not_safe_inside_a_transaction(self):
        text = (FILES / "safe" / "0042_orders_constraints_safe.sql").read_text(encoding="utf-8")
        found = [f["rule"] for f in lint_migration.lint_sql(text, in_transaction=True)]
        self.assertEqual(found.count("concurrently-in-transaction"), 2)
        self.assertEqual(found.count("slow-step-under-ddl-lock"), 3)

    def test_every_sql_fixture_is_covered_by_a_test_above(self):
        covered = {
            "lock-heavy/0042_orders_constraints.sql", "backfill/0060_users_status.sql",
            "destructive/0051_drop_legacy_billing.sql", "destructive/0051_drop_legacy_billing.down.sql",
            "django-add-index/0007_order_region.sql", "mysql/0009_rename_and_purge.sql",
            "safe/0042_orders_constraints_safe.sql", "safe/quoting_traps.sql",
        }
        present = {str(path.relative_to(FILES)).replace("\\", "/") for path in FILES.rglob("*.sql")}
        self.assertEqual(present, covered)


class CliTests(unittest.TestCase):
    def test_exit_one_and_blocking_line_for_an_unsafe_file(self):
        code, out, _ = run_cli([str(FILES / "lock-heavy" / "0042_orders_constraints.sql")])
        self.assertEqual(code, 1)
        self.assertIn("line 6  ERROR  create-index-not-concurrent  table: orders", out)
        self.assertIn("BLOCKING: 5 error(s), 1 warning(s).", out)

    def test_exit_zero_for_the_safe_file(self):
        code, out, _ = run_cli([str(FILES / "safe" / "0042_orders_constraints_safe.sql")])
        self.assertEqual(code, 0)
        self.assertIn("NO FINDINGS", out)
        self.assertIn("not proof of safety", out)

    def test_fail_on_controls_the_exit_code(self):
        warning_only = "ALTER TABLE t ADD COLUMN a int;\n"
        self.assertEqual(run_cli(["-"], stdin=warning_only)[0], 0)
        self.assertEqual(run_cli(["--fail-on", "warning", "-"], stdin=warning_only)[0], 1)
        unsafe = str(FILES / "lock-heavy" / "0042_orders_constraints.sql")
        self.assertEqual(run_cli(["--fail-on", "never", unsafe])[0], 0)

    def test_json_output_is_complete_and_stable(self):
        argv = ["--json", str(FILES / "backfill" / "0060_users_status.sql")]
        code, out, _ = run_cli(argv)
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(data["summary"], {"errors": 3, "warnings": 2, "blocking": True})
        self.assertEqual(data["files"][0]["statements"], 6)
        self.assertEqual(sorted(data["files"][0]["findings"][0]),
                         ["fix", "line", "lock", "risk", "rule", "severity", "statement", "table"])
        self.assertEqual(run_cli(argv)[1], out)

    def test_several_files_are_reported_separately(self):
        code, out, _ = run_cli(["--json", str(FILES / "safe" / "quoting_traps.sql"),
                                str(FILES / "destructive" / "0051_drop_legacy_billing.sql")])
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([len(entry["findings"]) for entry in data["files"]], [0, 4])

    def test_stdin_and_in_transaction_flag(self):
        code, out, _ = run_cli(["--in-transaction", "-"], stdin="CREATE INDEX CONCURRENTLY i ON t (a);")
        self.assertEqual(code, 1)
        self.assertIn("<stdin>: postgres, 1 statement(s), 1 finding(s)", out)
        self.assertIn("concurrently-in-transaction", out)

    def test_mysql_output_states_the_reduced_rule_set(self):
        code, out, _ = run_cli(["--dialect", "mysql", str(FILES / "mysql" / "0009_rename_and_purge.sql")])
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("note: MySQL gets the data-loss, rename and unbounded UPDATE/DELETE rules only."))

    def test_input_errors_exit_two_without_a_traceback(self):
        code, out, err = run_cli([str(FILES / "django-add-index" / "0007_order_region.py")])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not a SQL file", err)
        code, _, err = run_cli(["/no/such/migration.sql"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        code, _, err = run_cli(["-"], stdin="SELECT 'unterminated")
        self.assertEqual(code, 2)
        self.assertIn("<stdin>: line 1: unterminated ' quote", err)
        self.assertEqual(run_cli(["-"], stdin="-- only a comment\n")[0], 2)
        self.assertEqual(run_cli([])[0], 2)
        self.assertEqual(run_cli(["--dialect", "oracle", "x.sql"])[0], 2)

    def test_help_exits_zero_and_documents_exit_codes(self):
        code, out, _ = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Exit codes", out)


class ValidateRepoTests(unittest.TestCase):
    def test_repository_passes_its_own_validator(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import validate
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate.main(["validate.py", str(ROOT)])
        self.assertEqual(code, 0, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
