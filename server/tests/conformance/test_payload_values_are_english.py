"""A value that crosses the wire is written in English -- or nothing catches it.

WHY THIS FILE EXISTS. `core/mediaplan_mapping.py` shipped two payload values in
French and kept them for a year:

    REASON_UNMAPPED    = "sans_mapping"
    REASON_OUT_OF_WINDOW = "hors_fenetre_lignes_mappees"

They travelled on `unmapped.rows[].reason`, the console compared one of them
literally (`WorkbenchPlacementsPage.tsx`, `row.reason === "sans_mapping"`), and
`cards.py` printed both into a card block. CLAUDE.md §2 says « toujours écrire en
anglais : le code, les commentaires, LES VALEURS », and nothing in the repository
could see the breach: `ruff`, `finished_work_audit.py --gate` and every test
passed on both spellings.

IT IS A GUARD OF CLASS, NOT OF INSTANCE. Story 61.2 renamed the two, and renaming
two strings is what makes the third one appear somewhere else six months later --
which is exactly how four spellings of one capability word came to exist
(AI-261). What is checked here is the shape: a module-level string constant of
the media-plan family whose value looks like a French token.

WHAT IT DOES *NOT* CHECK, said so nobody trusts a guard that is not there:

  * SENTENCES are not checked. A label is prose, prose is where accents and
    apostrophes live, and a heuristic over prose is a heuristic that cries wolf.
    Only IDENTIFIER-SHAPED values are read -- `snake_case` tokens, the kind that
    end up in a `===` on a screen or in a `WHERE` clause;
  * COMMENTS are not checked. Several of these modules carry French comments from
    epic 22, and a comment reaches no wire. That is a real gap, and it is named
    rather than silently in scope;
  * only `server/core/mediaplan_*.py` and `server/core/plan_*.py` are in scope --
    the family story 61.2's test plan named. `core/warehouse.py:1582` still
    mentions `hors_fenetre_lignes_mappees` in a COMMENT and is held by another
    session; that is inventory, not an exemption of substance.
"""

from __future__ import annotations

import ast
import pathlib
import re

CORE = pathlib.Path(__file__).resolve().parents[2] / "core"

#: The modules whose module-level constants are read. The media-plan family, by
#: the two globs story 61.2 named -- not "every file", because a guard that reads
#: everything is a guard somebody turns off.
GLOBS = ("mediaplan_*.py", "plan_*.py")

#: A value shaped like an identifier: what ends up compared, stored or routed on.
#: Anything with a space is prose and is deliberately out of scope.
_IDENTIFIER_VALUE = re.compile(r"^[a-z][a-z0-9_]*$")

#: French markers, in the shape they take inside a `snake_case` token. Words and
#: not letters: `hors_fenetre` carries no accent at all once somebody has typed it
#: without one, so an accent test would have missed the exact value that caused
#: this file. Each entry is a whole segment between underscores.
FRENCH_SEGMENTS = frozenset(
    {
        "sans", "avec", "hors", "dans", "sous", "sur", "pour", "par", "vers",
        "fenetre", "fenetres", "ligne", "lignes", "mappee", "mappees", "mapping",
        "colonne", "colonnes", "champ", "champs", "valeur", "valeurs",
        "fichier", "fichiers", "erreur", "erreurs", "motif", "motifs",
        "aucun", "aucune", "tous", "toutes", "chaque", "plusieurs",
        "non", "oui", "vide", "casse", "pret", "prete", "termine", "terminee",
        "reussi", "reussie", "echec", "echoue", "attente", "brouillon",
        "nouveau", "nouvelle", "ancien", "ancienne", "date", "jour", "mois",
        "annee", "semaine", "devise", "montant", "depense", "budget",
    }
)

#: `mapping`, `date` and `budget` are English words too. They are in the set
#: because they are French words as well, and a token that is BOTH proves
#: nothing -- so a value is only refused when it carries a segment from this set
#: that is NOT also plain English. This is the list of the ones that overlap.
ALSO_ENGLISH = frozenset({"mapping", "date", "budget", "non", "sur", "par", "dans"})


def _constant_values(path: pathlib.Path) -> list[tuple[str, str]]:
    """`[(constant name, string value)]` for every module-level `NAME = "..."`.

    Read with `ast` and not with a regular expression: a regex over source text
    cannot tell a constant from a docstring, and the first version of this guard
    that did flagged three sentences inside comments.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not targets:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found.extend((name, node.value.value) for name in targets)
        elif isinstance(node.value, (ast.Tuple, ast.List)):
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    found.extend((name, element.value) for name in targets)
        elif isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.extend((name, key.value) for name in targets)
    return found


def _french_segments(value: str) -> set[str]:
    if not _IDENTIFIER_VALUE.match(value):
        return set()
    return {
        segment
        for segment in value.split("_")
        if segment in FRENCH_SEGMENTS and segment not in ALSO_ENGLISH
    }


def _modules() -> list[pathlib.Path]:
    return sorted({path for glob in GLOBS for path in CORE.glob(glob)})


def test_no_payload_value_of_the_media_plan_family_is_a_french_token() -> None:
    offenders: list[str] = []
    for path in _modules():
        for name, value in _constant_values(path):
            french = _french_segments(value)
            if french:
                offenders.append(f"{path.name}:{name} = {value!r} ({sorted(french)})")

    assert not offenders, (
        "payload value(s) written in French:\n  " + "\n  ".join(offenders) + "\n\n"
        "CLAUDE.md §2: « toujours écrire en anglais : le code, les commentaires, LES "
        "VALEURS ». These are values on the wire, not labels: a console that compares "
        "them literally, a card that prints them and an MCP that returns them all "
        "carry the word.\n\n"
        "Rename the VALUE and leave the constant name alone -- story 61.2 turned "
        "`sans_mapping` into `no_match` and `hors_fenetre_lignes_mappees` into "
        "`outside_matched_line_window` that way, so every symbolic caller followed "
        "with no edit. Then follow the literal comparisons: `grep -rn '<old value>' "
        "ui/admin/src server dbt`."
    )


def test_the_two_values_story_61_2_renamed_are_the_english_ones() -> None:
    """The instance, pinned beside the class -- one of them is what caused this file."""
    from core.mediaplan_mapping import REASON_OUT_OF_WINDOW, REASON_UNMAPPED

    assert REASON_UNMAPPED == "no_match"
    assert REASON_OUT_OF_WINDOW == "outside_matched_line_window"


def test_the_guard_is_not_vacuous() -> None:
    """It really opens the corpus, and it really refuses the two old spellings.

    Three assertions, because each covers a different way this file could pass on
    nothing: an empty corpus, a corpus with no constants in it, and a rule with no
    teeth.
    """
    modules = _modules()
    assert len(modules) >= 6, [path.name for path in modules]

    values = [value for path in modules for _, value in _constant_values(path)]
    assert len(values) > 20, len(values)
    # And identifier-shaped values really are in there, or the rule reads nothing.
    assert any(_IDENTIFIER_VALUE.match(value) for value in values)

    # THE TEETH. The two spellings this file was written for, and a sentence that
    # must stay out of scope.
    assert _french_segments("sans_mapping") == {"sans"}
    assert _french_segments("hors_fenetre_lignes_mappees") == {
        "hors", "fenetre", "lignes", "mappees",
    }
    assert _french_segments("no_match") == set()
    assert _french_segments("outside_matched_line_window") == set()
    assert _french_segments("Aucune ligne de ce plan ne nomme ce connecteur") == set()
