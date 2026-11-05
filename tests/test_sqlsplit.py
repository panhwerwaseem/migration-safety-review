import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "migration-safety-review"
sys.path.insert(0, str(SKILL / "scripts"))

import sqlsplit  # noqa: E402

FILES = SKILL / "evals" / "files"


def texts(sql, dialect="postgres"):
    return [stmt.text for stmt in sqlsplit.split_statements(sql, dialect)]


def lines(sql, dialect="postgres"):
    return [stmt.line for stmt in sqlsplit.split_statements(sql, dialect)]


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = sqlsplit.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class SplitTests(unittest.TestCase):
    def test_statements_are_split_on_semicolons_with_start_lines(self):
        sql = "SELECT 1;\n\nSELECT\n  2;\nSELECT 3"
        self.assertEqual(texts(sql), ["SELECT 1", "SELECT 2", "SELECT 3"])
        self.assertEqual(lines(sql), [1, 3, 5])

    def test_start_line_is_the_first_token_not_a_leading_comment(self):
        sql = "-- header\n/* more\n   header */\nDROP TABLE a;"
        self.assertEqual(lines(sql), [4])

    def test_line_comment_hides_a_semicolon(self):
        self.assertEqual(texts("SELECT 1 -- not; a split\n+ 2;"), ["SELECT 1 + 2"])

    def test_block_comments_nest_in_postgres(self):
        sql = "/* a /* b; */ DROP TABLE x; */ SELECT 1;"
        self.assertEqual(texts(sql), ["SELECT 1"])

    def test_block_comments_do_not_nest_in_mysql(self):
        sql = "/* a /* b */ SELECT 1; /* c */"
        self.assertEqual(texts(sql, "mysql"), ["SELECT 1"])

    def test_string_literal_keeps_semicolons_and_doubled_quotes(self):
        sql = "INSERT INTO t VALUES ('a; b', 'it''s; fine');SELECT 2;"
        self.assertEqual(texts(sql), ["INSERT INTO t VALUES ('a; b', 'it''s; fine')", "SELECT 2"])

    def test_backslash_is_literal_in_a_plain_postgres_string(self):
        # With standard_conforming_strings (the default) 'a\' is a complete string.
        self.assertEqual(texts(r"SELECT 'a\'; SELECT 2;"), [r"SELECT 'a\'", "SELECT 2"])

    def test_backslash_escapes_apply_in_an_e_string(self):
        self.assertEqual(texts(r"SELECT E'it\'s; here'; SELECT 2;"),
                         [r"SELECT E'it\'s; here'", "SELECT 2"])

    def test_backslash_escapes_apply_in_mysql_strings(self):
        self.assertEqual(texts(r"SELECT 'it\'s; here'; SELECT 2;", "mysql"),
                         [r"SELECT 'it\'s; here'", "SELECT 2"])

    def test_dollar_quoted_body_is_one_token(self):
        sql = "CREATE FUNCTION f() RETURNS void AS $$\nBEGIN\n  DELETE FROM t;\nEND;\n$$ LANGUAGE plpgsql;\nSELECT 1;"
        statements = sqlsplit.split_statements(sql)
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[1].line, 6)
        bodies = [tok.value for tok in statements[0].tokens if tok.kind == "string"]
        self.assertEqual(len(bodies), 1)
        self.assertIn("DELETE FROM t;", bodies[0])

    def test_tagged_dollar_quote_may_contain_another_dollar_quote(self):
        sql = "DO $outer$ BEGIN EXECUTE $$DROP TABLE x;$$; END $outer$;\nSELECT 1;"
        self.assertEqual(len(sqlsplit.split_statements(sql)), 2)

    def test_positional_parameter_is_not_a_dollar_quote(self):
        self.assertEqual(texts("SELECT $1; SELECT $2;"), ["SELECT $1", "SELECT $2"])

    def test_quoted_identifier_is_not_a_keyword(self):
        statement = sqlsplit.split_statements('ALTER TABLE "Order;s" ADD "drop" int;')[0]
        idents = [tok for tok in statement.tokens if tok.kind == "ident"]
        self.assertEqual([tok.name for tok in idents], ["Order;s", "drop"])
        self.assertNotIn("DROP", sqlsplit.kw(statement.tokens))

    def test_mysql_backticks_hash_comments_and_double_dash_rule(self):
        sql = "# DROP TABLE a;\nUPDATE `my;table` SET a = 5--1 WHERE id = 1;"
        statements = sqlsplit.split_statements(sql, "mysql")
        self.assertEqual(len(statements), 1)
        self.assertIn("WHERE", sqlsplit.kw(statements[0].tokens))
        # In PostgreSQL the same "--1" starts a comment.
        self.assertNotIn("WHERE", sqlsplit.kw(sqlsplit.split_statements("UPDATE t SET a = 5--1 WHERE id = 1;")[0].tokens))

    def test_mysql_delimiter_keeps_a_procedure_body_together(self):
        sql = "DELIMITER //\nCREATE PROCEDURE p()\nBEGIN\n  DELETE FROM t;\nEND//\nDELIMITER ;\nSELECT 1;"
        self.assertEqual(lines(sql, "mysql"), [2, 7])

    def test_psql_meta_command_lines_are_skipped(self):
        self.assertEqual(texts("\\set ON_ERROR_STOP on\nSELECT 1;\n\\echo done; DROP TABLE x\n"), ["SELECT 1"])

    def test_parenthesis_depth_is_recorded(self):
        statement = sqlsplit.split_statements("ALTER TABLE t ADD c numeric(10, 2), ADD d int;")[0]
        parts = sqlsplit.split_top(statement.tokens[3:])
        self.assertEqual(len(parts), 2)
        self.assertEqual(sqlsplit.kw(parts[1])[:2], ["ADD", "D"])

    def test_read_name_takes_the_last_part_of_a_qualified_name(self):
        tokens = sqlsplit.split_statements('DROP TABLE public."Orders";')[0].tokens
        self.assertEqual(sqlsplit.read_name(tokens, 2)[0], "Orders")
        tokens = sqlsplit.split_statements("DROP TABLE Public.Orders;")[0].tokens
        self.assertEqual(sqlsplit.read_name(tokens, 2)[0], "orders")

    def test_has_seq_ignores_keywords_inside_parentheses(self):
        tokens = sqlsplit.split_statements("ALTER TABLE t ADD c int CHECK (c IS NOT NULL);")[0].tokens
        self.assertFalse(sqlsplit.has_seq(tokens, "NOT", "NULL"))
        tokens = sqlsplit.split_statements("ALTER TABLE t ADD c int NOT NULL;")[0].tokens
        self.assertTrue(sqlsplit.has_seq(tokens, "NOT", "NULL"))

    def test_unterminated_input_raises_with_the_line(self):
        for sql, fragment in (
            ("SELECT 1;\nSELECT 'oops;", "line 2: unterminated ' quote"),
            ("SELECT 1;\n\n/* never closed", "line 3: unterminated /* comment"),
            ("DO $body$ BEGIN END;", "line 1: unterminated dollar-quoted string $body$"),
            ('SELECT "a', 'line 1: unterminated " quote'),
        ):
            with self.assertRaises(sqlsplit.SqlError) as caught:
                sqlsplit.split_statements(sql)
            self.assertEqual(str(caught.exception), fragment)

    def test_same_input_gives_same_output(self):
        sql = (FILES / "safe" / "quoting_traps.sql").read_text(encoding="utf-8")
        self.assertEqual(sqlsplit.split_statements(sql), sqlsplit.split_statements(sql))


class FixtureSplitTests(unittest.TestCase):
    def test_quoting_traps_fixture_splits_into_the_expected_statements(self):
        sql = (FILES / "safe" / "quoting_traps.sql").read_text(encoding="utf-8")
        statements = sqlsplit.split_statements(sql)
        self.assertEqual([stmt.line for stmt in statements], [5, 7, 8, 9, 11, 19, 23, 25, 26, 27, 28])
        heads = [sqlsplit.kw(stmt.tokens)[0] for stmt in statements]
        self.assertEqual(heads, ["SET", "INSERT", "INSERT", "INSERT", "CREATE", "CREATE", "COMMENT",
                                 "UPDATE", "DELETE", "ALTER", "ALTER"])

    def test_mysql_fixture_splits_into_the_expected_statements(self):
        sql = (FILES / "mysql" / "0009_rename_and_purge.sql").read_text(encoding="utf-8")
        statements = sqlsplit.split_statements(sql, "mysql")
        self.assertEqual([stmt.line for stmt in statements], [4, 6, 8, 10, 12, 14, 17, 23])
        self.assertTrue(statements[6].text.startswith("CREATE PROCEDURE purge_sessions()"))
        self.assertTrue(statements[6].text.endswith("END"))


class CliTests(unittest.TestCase):
    def test_lists_statements_with_lines(self):
        code, out, _ = run_cli([str(FILES / "backfill" / "0060_users_status.sql")])
        self.assertEqual(code, 0)
        self.assertIn("6 statement(s)", out)
        self.assertIn("  line 7: UPDATE users SET status = 'active'", out)

    def test_json_output(self):
        code, out, _ = run_cli(["--json", str(FILES / "backfill" / "0060_users_status.sql")])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["dialect"], "postgres")
        self.assertEqual([s["line"] for s in data["files"][0]["statements"]], [3, 5, 7, 9, 11, 13])

    def test_orm_file_is_refused_with_a_pointer_to_rendering(self):
        code, out, err = run_cli([str(FILES / "django-add-index" / "0007_order_region.py")])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("not a SQL file", err)
        self.assertIn("references/render-and-rollback.md", err)

    def test_missing_file_is_an_input_error_not_a_traceback(self):
        code, _, err = run_cli(["/no/such/dir/migration.sql"])
        self.assertEqual(code, 2)
        self.assertIn("/no/such/dir/migration.sql: cannot read", err)

    def test_file_without_statements_is_an_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.sql"
            path.write_text("-- nothing here\n", encoding="utf-8")
            code, _, err = run_cli([str(path)])
        self.assertEqual(code, 2)
        self.assertIn("no SQL statements found", err)

    def test_unterminated_quote_is_an_input_error_with_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sql"
            path.write_text("SELECT 1;\nSELECT 'oops;\n", encoding="utf-8")
            code, _, err = run_cli([str(path)])
        self.assertEqual(code, 2)
        self.assertIn("broken.sql: line 2: unterminated ' quote", err)

    def test_byte_order_mark_does_not_hide_the_first_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.sql"
            path.write_bytes(b"\xef\xbb\xbfDROP TABLE a;\r\nDROP TABLE b;\r\n")
            code, out, _ = run_cli(["--json", str(path)])
        self.assertEqual(code, 0)
        statements = json.loads(out)["files"][0]["statements"]
        self.assertEqual(statements, [{"line": 1, "text": "DROP TABLE a"}, {"line": 2, "text": "DROP TABLE b"}])

    def test_no_arguments_is_a_usage_error(self):
        code, _, _ = run_cli([])
        self.assertEqual(code, 2)

    def test_help_exits_zero(self):
        code, out, _ = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Exit codes", out)


if __name__ == "__main__":
    unittest.main()
