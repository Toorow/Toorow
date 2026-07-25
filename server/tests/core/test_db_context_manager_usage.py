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


def test_no_discarded_connection_context_manager() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
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
