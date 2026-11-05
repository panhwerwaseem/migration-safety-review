# Changelog

All notable changes to this skill are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## 0.1.0 - 2026-11-06

### Added

- `SKILL.md` with the review protocol: hard rules and an eight-step workflow.
- `scripts/lint_migration.py`, a statement-level linter for rendered SQL migrations. Full rule set for PostgreSQL 12 or later; data-loss, rename and unbounded UPDATE/DELETE rules for MySQL.
- `scripts/rule_catalog.py` with the rule table: severity, dialects, lock, risk and fix name for each rule.
- `scripts/sqlsplit.py`, the statement splitter, which respects comments, string literals, dollar-quoted bodies and the MySQL `DELIMITER` command.
- References for PostgreSQL lock levels, safe rewrites, and rendering SQL from ORM migrations with rollback planning.
- Fixtures with planted defects (lock-heavy constraints, a backfill inside a schema-change transaction, destructive changes with a down migration that restores no data, a rendered Django migration, a MySQL migration), two safe fixtures, eval cases, and trigger queries.
- Unit tests, a format validator, and a CI workflow.
