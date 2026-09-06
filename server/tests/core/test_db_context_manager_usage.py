"""Garde structurelle : on ne jette pas un gestionnaire de contexte de connexion.

`core.db.get_connection` est un @contextmanager. Écrire

    conn = get_connection().__enter__()

appelle `__enter__()` sur un objet TEMPORAIRE : la référence au gestionnaire est
perdue, le générateur est finalisé par le ramasse-miettes, son
`finally: conn.close()` s'exécute — et la connexion est morte avant la première
requête.

Ce n'est pas une hypothèse. Le 2026-07-25, la suppression RGPD d'organisation
(`_delete_org`) renvoyait 500 sur chaque appel en production, avec
`psycopg.OperationalError: the connection is closed`, pour exactement cette raison.
Le chemin de suppression était entièrement non fonctionnel, et rien ne le signalait.

Ce test attrape la CLASSE de défaut, pas seulement l'instance corrigée : un
gestionnaire de contexte dont on ne garde pas la référence est toujours un bug,
quel que soit le fichier.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2]

# `get_connection().__enter__()` sous toutes ses formes d'espacement.
DISCARDED_CM = re.compile(r"get_connection\s*\(\s*\)\s*\.\s*__enter__\s*\(")


def _python_sources() -> list[Path]:
    return [
        p
        for p in SERVER.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    ]


def _code_lines(text: str) -> list[tuple[int, str]]:
    """The file's CODE, with comments and string literals blanked out.

    IT USED TO READ RAW LINES, and that made it fire on prose. A comment
    explaining why the discarded-manager shape was retired -- the most useful
    place to put that explanation, right above the repair -- was reported as an
    offender. A guard that a reader can only satisfy by rewording an explanation
    teaches exactly the wrong lesson: it says "do not describe this", when what
    it means is "do not write this".

    Tokenizing keeps the guard on code. A file that will not tokenize is
    returned whole rather than skipped: a syntax error must not become a hole
    the pattern can hide in.
    """
    import io
    import tokenize

    blanked: dict[int, list[str]] = {}
    lines = text.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return list(enumerate(lines, start=1))
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            source = blanked.get(row) or list(lines[row - 1])
            first = start_col if row == start_row else 0
            last = end_col if row == end_row else len(source)
            for index in range(first, min(last, len(source))):
                source[index] = " "
            blanked[row] = source
    return [
        (index, "".join(blanked[index]) if index in blanked else line)
        for index, line in enumerate(lines, start=1)
    ]


def test_no_discarded_connection_context_manager() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in _code_lines(text):
            if DISCARDED_CM.search(line):
                offenders.append(f"{path.relative_to(SERVER)}:{lineno} — {line.strip()}")

    assert not offenders, (
        "Un gestionnaire de contexte de connexion est jeté aussitôt créé, donc la "
        "connexion se fermera avant usage. Garde la référence :\n"
        "    cm = get_connection()\n"
        "    conn = cm.__enter__()\n"
        "    ...\n"
        "    finally: cm.__exit__(None, None, None)\n\n" + "\n".join(offenders)
    )
