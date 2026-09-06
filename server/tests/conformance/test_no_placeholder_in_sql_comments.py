"""No `%s` inside a `--` comment of a SQL literal -- psycopg counts it as a placeholder.

AI-321 (2026-08-29). `inbound_ingest._fetch_projection_plan` carried, inside its
own SQL comment, the sentence "jamais un `%s IS NULL` nu". psycopg does not
read comments: it saw six placeholders, received five parameters, and the first
governed upload on a Datastream the wizard had just brought to ACTIVE died with
`ProgrammingError: the query has 6 placeholders but 5 parameters were passed`
-- behind a 503 that logged the exception's type and nothing else. Every test
of that path mocks the connection, so nothing could see it (the AI-313 class:
a statement that cannot execute, invisible until a database is asked).

This walks every string constant of the server tree that looks like SQL and
refuses a `%s` (or a `%(name)s`) on a `--` comment line. The fix is always the
same: say "a placeholder" in prose, or write `%%s`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2]
_TREES = ("core", "inbound", "modules")
_SQL_MARKER = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER)\b")
_COMMENT_PLACEHOLDER = re.compile(r"--[^\n]*(?<!%)%(s|\([A-Za-z_][A-Za-z0-9_]*\)s)")


def _offenders() -> list[str]:
    found: list[str] = []
    for tree in _TREES:
        for path in sorted((_SERVER / tree).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "--" not in source or "%s" not in source and "%(" not in source:
                continue
            module = ast.parse(source)
            for node in ast.walk(module):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                text = node.value
                if "--" not in text or not _SQL_MARKER.search(text):
                    continue
                for match in _COMMENT_PLACEHOLDER.finditer(text):
                    line = node.lineno + text[: match.start()].count("\n")
                    found.append(f"{path.relative_to(_SERVER)}:{line}")
    return found


def test_no_placeholder_hides_in_a_sql_comment() -> None:
    offenders = _offenders()
    assert not offenders, (
        "a `%s` inside a `--` comment of a SQL literal is a placeholder psycopg will "
        "count -- say 'a placeholder' in prose or write `%%s`:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_sees_the_shape_it_exists_for() -> None:
    sample = 'SELECT 1 FROM t WHERE a = %s\n  -- never a bare `%s IS NULL` here\n  AND b = %s'
    assert _COMMENT_PLACEHOLDER.search(sample) is not None
    escaped = "SELECT 1 -- write `%%s` in prose\n WHERE a = %s"
    assert _COMMENT_PLACEHOLDER.search(escaped) is None
    named = "SELECT 1 -- %(name)s in a comment\n WHERE a = %(name)s"
    assert _COMMENT_PLACEHOLDER.search(named) is not None
