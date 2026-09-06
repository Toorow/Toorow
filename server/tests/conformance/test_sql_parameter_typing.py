"""Un `%s IS NULL` nu ne peut pas revenir dans une requete du depot (AI-186).

**Le defaut, mesure et non suppose.** psycopg 3 envoie une `str` et un `None`
avec l'oid UNKNOWN et laisse Postgres deduire le type du contexte. Un
`$n IS NULL` isole n'en offre aucun : le serveur repond
`IndeterminateDatatype: n'a pas pu determiner le type de donnees du parametre
$n` et **la requete entiere echoue**, pas seulement la garde.

Probe du 2026-08-04, sur un cluster jetable :

    SELECT %s IS NULL   avec "abc" -> IndeterminateDatatype
    SELECT %s IS NULL   avec None  -> IndeterminateDatatype
    SELECT %s IS NULL   avec 42    -> False      (int/float portent un oid)
    SELECT %s::text IS NULL        -> repond, dans les trois cas

**Pourquoi une garde, et pourquoi seulement maintenant.** Cette classe est
connue et documentee depuis le 2026-07-27 : l'en-tete de
`server/tests/core/test_canonical_identity_pg.py` la nomme mot pour mot, avec
son effet -- deux statements que Postgres refusait, avales dans une exception
sanitisee, et PERSONNE ne pouvait s'authentifier. La lecon a ete ecrite dans une
docstring que rien n'applique, et la classe est revenue **huit fois** : huit
sites vivants au 2026-08-04, dont `advance_state` (aucun candidat ne pouvait
quitter l'etat `created`) et `advance_dispatch` (aucun dispatch de fichier ne
pouvait aboutir). Une lecon sans garde n'est pas une lecon.

Elle est STRUCTURELLEMENT invisible aux doubles de test -- un curseur
`MagicMock` accepte n'importe quel SQL et ne verifie aucun type -- et elle ne se
declenche qu'a l'execution reelle, souvent sur la branche `None` qui est la
branche par defaut. D'ou une garde de SOURCE.

La reparation est un cast explicite au type de la colonne comparee :
`%s::text`, `%s::bigint`, `%s::double precision`.

**La forme voisine, desormais gardee elle aussi.** Un parametre non type passe a
une fonction POLYMORPHE (`jsonb_build_array(%s)`) echoue pour exactement la meme
raison : la fonction accepte `any`, donc elle n'offre aucun contexte au serveur
pour deduire l'oid, et la requete entiere est refusee.

Cette forme etait NOMMEE ici le 2026-08-04 comme << une forme voisine que cette
garde ne voit pas >>, avec l'espoir qu'elle soit reconnue plutot que
rediagnostiquee. Elle a ete rediagnostiquee le 2026-08-09, sur trois autres
sites, dans `test_inbound_raw_imports.py` -- rouges depuis leur ecriture,
invisibles parce que pg-gated.

C'est la these de cet en-tete appliquee a lui-meme : **une lecon sans garde
n'est pas une lecon**, et une garde qui documente son propre trou ne le ferme
pas. Les deux formes sont maintenant detectees par le meme scan.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCANNED = ("server/core", "server/inbound", "server/modules", "server/tests", "scripts")

#: Un `%s` colle a `IS NULL` / `IS NOT NULL` sans cast intercale. La forme
#: reparee (`%s::text IS NULL`) ne matche pas, et c'est exactement le but.
BARE = re.compile(r"%s\s+IS\s+(?:NOT\s+)?NULL", re.IGNORECASE)

#: Les fonctions POLYMORPHES du depot : elles declarent `any`, donc un `%s` nu
#: qu'on leur passe n'a aucun contexte de type. La liste est fermee plutot
#: qu'heuristique -- une fonction qui declare un type concret (`lower(text)`)
#: donne le contexte et n'a pas besoin de cast, et l'y inclure produirait du
#: bruit qu'on apprendrait a ignorer.
POLYMORPHIC = (
    "jsonb_build_array",
    "jsonb_build_object",
    "json_build_array",
    "json_build_object",
    "to_jsonb",
)

#: Un `%s` argument direct d'une de ces fonctions, sans `::` qui suive. On lit
#: l'argument jusqu'a la virgule ou la parenthese suivante pour ne pas exiger
#: un cast la ou il y en a deja un.
UNTYPED_POLY = re.compile(
    r"\b(?:" + "|".join(POLYMORPHIC) + r")\s*\(([^()]*)\)",
    re.IGNORECASE,
)
BARE_ARG = re.compile(r"(?<!:)%s\s*(?:,|$)")

#: Un commentaire SQL (`-- ...` jusqu'a la fin de ligne) VIT DANS le litteral,
#: donc l'AST ne le distingue pas du SQL qui l'entoure. Les reparations posees
#: le 2026-08-04 expliquent la forme fautive juste au-dessus de la forme
#: corrigee, a l'endroit exact ou le prochain lecteur la cherchera : les retirer
#: pour faire taire cette garde reviendrait a effacer la raison de la garde.
SQL_COMMENT = re.compile(r"--[^\n]*")

#: Ce fichier cite la forme fautive pour l'exercer (`test_the_guard_can_actually_fail`).
SELF = Path(__file__).resolve()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Les docstrings, reperees par identite -- elles DECRIVENT le defaut.

    Ce fichier et `test_canonical_identity_pg.py` citent tous deux la forme
    fautive en prose pour l'expliquer. Une garde qui se declenche sur sa propre
    explication oblige a la reecrire en charabia, ce qui est la facon la plus
    sure de la rendre inutile. Un scan de lignes ne pouvait pas les distinguer
    (une ligne de SQL triple-quote ne porte pas plus de guillemets qu'une ligne
    de prose) : c'est la raison du passage par l'AST, pas une preference.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _untyped_polymorphic_arg(text: str) -> bool:
    """Un `%s` nu passe a une fonction qui accepte `any`.

    Le meme defaut que `%s IS NULL` par le meme mecanisme : rien dans la requete
    ne dit au serveur quel type le parametre porte, donc il refuse la requete
    ENTIERE plutot que ce seul argument.
    """
    return any(
        BARE_ARG.search(match.group(1)) for match in UNTYPED_POLY.finditer(text)
    )


def _offenders() -> tuple[list[str], int]:
    found: list[str] = []
    scanned = 0
    for folder in SCANNED:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or path.resolve() == SELF:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            upper = source.upper()
            if "IS NULL" not in upper and not any(
                name.upper() in upper for name in POLYMORPHIC
            ):
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover - un fichier casse a son propre rouge
                continue
            scanned += 1
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings:
                    continue
                text = SQL_COMMENT.sub("", node.value)
                if not BARE.search(text) and not _untyped_polymorphic_arg(text):
                    continue
                found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found, scanned


def test_no_bare_untyped_is_null_parameter_in_any_query() -> None:
    offenders, scanned = _offenders()
    assert scanned > 0, "aucun fichier candidat n'a ete lu : la garde ne mesure rien"
    assert not offenders, (
        "un `%s IS NULL` sans cast rend la requete indeterminee contre un vrai "
        "Postgres (str et None n'ont pas d'oid) et la fait echouer EN ENTIER. "
        "Caster le parametre au type de la colonne comparee -- `%s::text IS NULL`, "
        "`%s::bigint IS NULL`, `%s::double precision IS NULL`. Meme defaut, meme "
        "reparation pour un `%s` nu passe a une fonction polymorphe "
        "(`jsonb_build_array(%s::text)`). Sites :\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """Une garde qui ne peut pas echouer est une garde qui ment.

    Le detecteur est exerce sur les deux formes et sur une docstring, pour
    prouver qu'il distingue le cast de son absence et le SQL de la prose --
    sans quoi un vert ne dirait rien.
    """
    assert BARE.search("WHERE (%s IS NULL OR project_id = %s)")
    assert BARE.search("CASE WHEN %s IS NOT NULL THEN 1 END")
    assert not BARE.search("WHERE (%s::text IS NULL OR project_id = %s)")
    assert not BARE.search("WHERE (%s::bigint IS NULL OR row_count = %s)")

    # La forme voisine, exercee dans les deux sens elle aussi -- sans quoi
    # l'avoir ajoutee ne prouverait rien.
    assert _untyped_polymorphic_arg("SET path = jsonb_build_array(%s)")
    assert _untyped_polymorphic_arg("SET path = jsonb_build_array(%s::text, %s)")
    assert not _untyped_polymorphic_arg("SET path = jsonb_build_array(%s::text)")
    assert not _untyped_polymorphic_arg(
        "SET path = jsonb_build_array(%s::text, %s::text, %s::text)"
    )
    # Une fonction qui DECLARE un type concret donne le contexte : pas de bruit.
    assert not _untyped_polymorphic_arg("WHERE lower(%s) = name")

    module = ast.parse(
        '"""Doc citant %s IS NULL."""\n'
        'SQL = "WHERE %s IS NULL"\n'
        'COMMENTE = "-- jamais %s IS NULL nu\\nWHERE id = %s"\n'
    )
    docstrings = _docstring_nodes(module)
    flagged = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and BARE.search(SQL_COMMENT.sub("", node.value))
    ]
    assert flagged == [2], (
        "la docstring et le commentaire SQL doivent etre ignores, l'affectation SQL non"
    )
