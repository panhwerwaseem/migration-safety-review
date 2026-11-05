#!/usr/bin/env python3
"""Split a SQL file into statements, with the line each one starts on.

lint_migration.py imports this module. Run it directly to see exactly how a
file was split, for example when a finding points at a line you did not
expect.

Handled when splitting: line comments (-- and, for MySQL, #), block comments
(nested for PostgreSQL), single-quoted strings with doubled quotes, E'...'
strings and MySQL strings with backslash escapes, quoted identifiers,
PostgreSQL dollar-quoted bodies ($$...$$ and $tag$...$tag$), psql
meta-command lines, and the mysql client's DELIMITER command.

Not handled: function bodies written without dollar quotes (BEGIN ATOMIC ...
END, or a MySQL procedure body without a DELIMITER change) are split at each
semicolon; COPY ... FROM stdin data blocks; MySQL /*! ... */ version comments
are treated as comments.

Examples:
  sqlsplit.py migrations/0042_orders.sql
  sqlsplit.py --dialect mysql --json V9__rename.sql
  python manage.py sqlmigrate shop 0007 | sqlsplit.py -

Exit codes: 0 = the file was split, 2 = usage or input error (unreadable
file, not a SQL file, unterminated quote or comment, no statements). Exit
code 1 is not used: this script reports no findings.
Standard library only. Reads the files you name. Writes only to stdout and
stderr. Makes no network calls.
"""
import argparse
import json
import os
import re
import sys
from collections import namedtuple

# kind: word (unquoted keyword or identifier; value is upper-cased), ident (quoted
# identifier), string (literal), number, punct. name is the identifier as the
# database would fold it; depth is the parenthesis nesting level.
Token = namedtuple("Token", "kind value name depth line")
Statement = namedtuple("Statement", "line text tokens")

WORD_RE = re.compile(r"[^\W\d][\w$]*")              # a letter or underscore, then word characters
NUMBER_RE = re.compile(r"[0-9][0-9A-Za-z_.]*")
DOLLAR_TAG_RE = re.compile(r"\$(?:[^\W\d]\w*)?\$")  # $$ or $tag$
NOT_SQL = (".py", ".rb", ".js", ".mjs", ".cjs", ".ts", ".php", ".cs", ".java", ".kt", ".go", ".ex",
           ".exs", ".prisma", ".xml", ".yaml", ".yml", ".json", ".toml")


class SqlError(Exception):
    """Input that cannot be read or split into statements."""


def quoted_end(text, start, quote, backslash):
    """Index just past the closing quote, or -1. A doubled quote is an escape."""
    i = start + 1
    while i < len(text):
        if backslash and text[i] == "\\":
            i += 2
        elif text[i] != quote:
            i += 1
        elif text[i + 1:i + 2] == quote:
            i += 2
        else:
            return i + 1
    return -1


def comment_end(text, start, nested):
    """Index just past the end of a /* */ comment, or -1. PostgreSQL nests them."""
    depth, i = 1, start + 2
    while i < len(text):
        two = text[i:i + 2]
        if two == "*/":
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i
        elif nested and two == "/*":
            depth, i = depth + 1, i + 2
        else:
            i += 1
    return -1


def split_statements(text, dialect="postgres"):
    """Split SQL text into Statement(line, text, tokens). Raises SqlError."""
    mysql = dialect == "mysql"
    statements, tokens, shown = [], [], []
    i, line, depth, delim = 0, 1, 0, ";"

    def line_end(pos):
        found = text.find("\n", pos)
        return len(text) if found == -1 else found

    def flush():
        if tokens:
            statements.append(Statement(tokens[0].line, " ".join("".join(shown).split()), list(tokens)))
        del tokens[:], shown[:]

    while i < len(text):
        ch, two = text[i], text[i:i + 2]
        word = WORD_RE.match(text, i)
        end = i + 1
        if ch.isspace():
            shown.append(" ")
        elif text.startswith(delim, i):
            flush()
            depth, end = 0, i + len(delim)
        elif (mysql and ch == "#") or (two == "--" and (not mysql or not text[i + 2:i + 3].strip())):
            end = line_end(i)                       # MySQL needs whitespace after "--"
        elif two == "/*":
            end = comment_end(text, i, nested=not mysql)
            if end == -1:
                raise SqlError("line %d: unterminated /* comment" % line)
            shown.append(" ")
        elif ch == "\\" and not tokens and not mysql:
            end = line_end(i)                       # psql meta-command such as \set or \connect
        elif ch in "'\"" or (ch == "`" and mysql):
            backslash = mysql and ch != "`"
            if (ch == "'" and not mysql and tokens and tokens[-1].kind == "word"
                    and tokens[-1].value == "E" and text[i - 1] in "eE"):
                tokens.pop()                        # E'...' string: backslash escapes apply
                backslash = True
            end = quoted_end(text, i, ch, backslash)
            if end == -1:
                raise SqlError("line %d: unterminated %s quote" % (line, ch))
            body = text[i + 1:end - 1]
            if ch == "'":
                tokens.append(Token("string", body, "", depth, line))
            else:
                name = body.replace(ch * 2, ch)
                tokens.append(Token("ident", body, name.lower() if mysql else name, depth, line))
            shown.append(text[i:end])
        elif ch == "$" and not mysql and DOLLAR_TAG_RE.match(text, i):
            tag = DOLLAR_TAG_RE.match(text, i).group(0)
            close = text.find(tag, i + len(tag))
            if close == -1:
                raise SqlError("line %d: unterminated dollar-quoted string %s" % (line, tag))
            end = close + len(tag)
            tokens.append(Token("string", text[i + len(tag):close], "", depth, line))
            shown.append(tag + "..." + tag)
        elif word and mysql and not tokens and word.group(0).upper() == "DELIMITER":
            end = line_end(i)                       # mysql client command, not SQL
            delim = text[word.end():end].strip()
            if not delim:
                raise SqlError("line %d: DELIMITER without a value" % line)
        elif word or NUMBER_RE.match(text, i):
            match = word or NUMBER_RE.match(text, i)
            end = match.end()
            raw = match.group(0)
            tokens.append(Token("word", raw.upper(), raw.lower(), depth, line) if word
                          else Token("number", raw, "", depth, line))
            shown.append(raw)
        else:
            if ch == ")":
                depth = max(depth - 1, 0)
            tokens.append(Token("punct", ch, "", depth, line))
            if ch == "(":
                depth += 1
            shown.append(ch)
        line += text.count("\n", i, end)
        i = end
    flush()
    return statements


def kw(tokens):
    """Keyword view of a token list: upper-cased words, '' for everything else."""
    return [t.value if t.kind == "word" else "" for t in tokens]


def top(tokens):
    """The tokens that sit outside every parenthesis."""
    return [t for t in tokens if t.depth == 0]


def has_seq(tokens, *words):
    """True if the keywords occur consecutively outside parentheses."""
    seq = kw(top(tokens))
    return any(tuple(seq[k:k + len(words)]) == words for k in range(len(seq) - len(words) + 1))


def skip(tokens, k, *words):
    """Index of the first token at or after k that is not one of the keywords."""
    while k < len(tokens) and tokens[k].kind == "word" and tokens[k].value in words:
        k += 1
    return k


def read_name(tokens, k):
    """Read [schema.]name at tokens[k]. Returns (last name part or None, next index)."""
    name = None
    while k < len(tokens) and tokens[k].kind in ("word", "ident"):
        name, k = tokens[k].name, k + 1
        if k < len(tokens) and tokens[k].kind == "punct" and tokens[k].value == ".":
            k += 1
        else:
            break
    return name, k


def split_top(tokens):
    """Split a token list on commas that are outside parentheses."""
    parts = [[]]
    for tok in tokens:
        if tok.kind == "punct" and tok.value == "," and tok.depth == 0:
            parts.append([])
        else:
            parts[-1].append(tok)
    return [part for part in parts if part]


def read_sql(path):
    """Return the text of a SQL file, or of standard input for "-". Raises SqlError."""
    if path == "-":
        return sys.stdin.read().lstrip("\ufeff")
    if os.path.splitext(path)[1].lower() in NOT_SQL:
        raise SqlError("not a SQL file. Render the migration to SQL first; "
                       "references/render-and-rollback.md lists the command for each tool.")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:   # drops a byte order mark
            return handle.read()
    except OSError as exc:
        raise SqlError("cannot read: %s" % (exc.strerror or exc))


def load_statements(path, dialect):
    """Read and split one file. Raises SqlError with the file name in the message."""
    name = "<stdin>" if path == "-" else path
    try:
        statements = split_statements(read_sql(path), dialect)
        if not statements:
            raise SqlError("no SQL statements found")
    except SqlError as exc:
        raise SqlError("%s: %s" % (name, exc))
    return name, statements


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", metavar="FILE",
                        help="SQL file; use - to read standard input")
    parser.add_argument("--dialect", choices=("postgres", "mysql"), default="postgres",
                        help="SQL dialect (default postgres)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    result = []
    for path in args.files:
        try:
            name, statements = load_statements(path, args.dialect)
        except SqlError as exc:
            print("sqlsplit.py: error: %s" % exc, file=sys.stderr)
            return 2
        result.append({"file": name, "statements": [
            {"line": stmt.line, "text": stmt.text} for stmt in statements]})
    if args.json:
        print(json.dumps({"dialect": args.dialect, "files": result}, indent=2))
        return 0
    for entry in result:
        print("%s: %s, %d statement(s)" % (entry["file"], args.dialect, len(entry["statements"])))
        for stmt in entry["statements"]:
            print("  line %d: %s" % (stmt["line"], stmt["text"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
