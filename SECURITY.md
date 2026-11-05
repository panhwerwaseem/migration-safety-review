# Security

An agent skill is instructions and code that your coding agent will follow and run. Review a skill before installing it, the same way you would review any dependency.

## What this skill does on your machine

| | |
|---|---|
| Reads | The SQL files you name on the command line, or standard input |
| Writes | Nothing. Results go to standard output and errors to standard error |
| Executes | Nothing. The bundled scripts start no processes and open no database connection |
| Network | None. The bundled scripts import no network modules. `tools/validate.py` checks this in CI |
| Dependencies | Python standard library only |
| Pre-approved tools | None. `SKILL.md` has no `allowed-tools` entry, so your agent's normal permission prompts apply |
| Hooks, MCP servers, background processes | None |

The skill instructs the agent to render ORM migrations to SQL with your own migration tool, for example `python manage.py sqlmigrate`. Those commands are not part of this skill. They run your project's code and may connect to whichever database your settings point at, so check that the environment points at a development database before approving them.

The skill instructs the agent not to apply a migration to any shared database, and to apply one to a disposable local database only with your agreement. Any edit to a migration file goes through your agent's normal review and permission flow.

The linter reports statements that match its rules. A clean result is not proof that a migration is safe: it does not know table sizes, does not read `DO` blocks or function bodies, and does not assess MySQL lock behaviour.

The files under `skills/migration-safety-review/evals/files/` are deliberately unsafe migrations used as test fixtures. Do not run them against a real database.

## Reporting a vulnerability

Please report security problems privately through the repository's **Security** tab ("Report a vulnerability"). If that option is not available, open an issue that says a private channel is needed, without including exploit details.

## Verifying what you install

- Install from a tagged release and read the diff between tags before updating.
- The whole skill is the `skills/migration-safety-review/` folder: one Markdown protocol, three Python scripts, three reference files, and eval fixtures.
