import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "migration-safety-review"
sys.path.insert(0, str(SKILL / "scripts"))

import rule_catalog  # noqa: E402

REWRITES = (SKILL / "references" / "safe-rewrites.md").read_text(encoding="utf-8")


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = rule_catalog.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CatalogTests(unittest.TestCase):
    def test_every_rule_is_well_formed(self):
        for rule_id, rule in rule_catalog.RULES.items():
            self.assertRegex(rule_id, r"^[a-z]+(-[a-z]+)*$")
            self.assertIn(rule.severity, ("error", "warning"), rule_id)
            self.assertIn("postgres", rule.dialects, rule_id)
            self.assertTrue(set(rule.dialects) <= {"postgres", "mysql"}, rule_id)
            for text in (rule.lock, rule.risk, rule.fix):
                self.assertTrue(text.strip(), rule_id)

    def test_mysql_gets_only_data_loss_rename_and_unbounded_dml_rules(self):
        mysql = sorted(r for r, rule in rule_catalog.RULES.items() if "mysql" in rule.dialects)
        self.assertEqual(mysql, [
            "delete-without-where", "drop-column", "drop-schema", "drop-table",
            "rename-column", "rename-table", "truncate", "update-without-where"])

    def test_every_fix_has_a_section_in_safe_rewrites(self):
        headings = set(re.findall(r"^## ([a-z-]+)$", REWRITES, re.M))
        for rule_id, rule in rule_catalog.RULES.items():
            self.assertIn(rule.fix, headings, "%s points at a missing section" % rule_id)

    def test_rule_table_in_safe_rewrites_matches_the_catalog(self):
        rows = re.findall(r"^\| `([a-z-]+)` \| (\w+) \| ([\w, ]+) \| \[([a-z-]+)\]\(#[a-z-]+\) \|$", REWRITES, re.M)
        documented = {row[0]: row[1:] for row in rows}
        self.assertEqual(sorted(documented), sorted(rule_catalog.RULES))
        for rule_id, rule in rule_catalog.RULES.items():
            self.assertEqual(documented[rule_id], (rule.severity, ", ".join(rule.dialects), rule.fix), rule_id)


class CliTests(unittest.TestCase):
    def test_lists_every_rule_sorted(self):
        code, out, _ = run_cli([])
        ids = [line.split()[0] for line in out.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(ids, sorted(rule_catalog.RULES))

    def test_dialect_filter(self):
        code, out, _ = run_cli(["--dialect", "mysql"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 8)
        self.assertNotIn("create-index-not-concurrent", out)

    def test_single_rule_detail(self):
        code, out, _ = run_cli(["--rule", "set-not-null"])
        self.assertEqual(code, 0)
        self.assertIn("lock:      ACCESS EXCLUSIVE while the whole table is scanned", out)
        self.assertIn("fix:       not-null-via-check", out)

    def test_json_output(self):
        code, out, _ = run_cli(["--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["rules"]), len(rule_catalog.RULES))
        self.assertEqual(sorted(data["rules"][0]), ["dialects", "fix", "lock", "risk", "rule", "severity"])

    def test_unknown_rule_is_a_usage_error(self):
        code, _, err = run_cli(["--rule", "no-such-rule"])
        self.assertEqual(code, 2)
        self.assertIn("unknown rule", err)

    def test_help_exits_zero(self):
        code, out, _ = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Exit codes", out)


if __name__ == "__main__":
    unittest.main()
