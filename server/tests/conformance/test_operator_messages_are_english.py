"""Refuse a French -- or half-French -- sentence composed for the operator.

WHY THIS EXISTS, and why the guard delivered by story 57.6 could not do it.
`test_manifest_labels_are_english.py` sweeps `LABEL_PATHS` of the connector
manifests: `report_profiles[].display_name`, `source_capabilities[].description`.
Those are STATIC labels sitting in JSON. The class it protects is real and it
stays protected -- but a sentence composed in Python never passes through it.

Measured 2026-08-08, AI-216: two strings rendered by `PremierRapportCard.tsx`
lived in `first_report_draft.py`, and four more of the same shape lived in
`admin_api.py`, `datamodel.py` and `bounded_recovery.py`. The manifest guard was
green throughout, because it never reads those files.

AND THEY WERE NOT FRENCH -- THEY WERE MIXED. That is the detail that matters for
what this guard looks for:

    "No exposed account is eligible. Expose exactly one account
     avant de recommander un rapport."
    "Cannot delete field '{name}': it is used by {n} mapping(s) actif(s).
     Retirez les mappings avant de supprimer ce champ."

A sentence that starts in English passes every eye that skims the first words, so
"is this file French?" is the wrong question. The question is whether FRENCH
FUNCTION WORDS appear in a string the operator will read.

WHAT IT READS, AND WHY BY AST RATHER THAN BY GREP. Only string literals in the
three positions this repository uses to speak to a person: the value of a
`message` / `reason` / `explanation` key in a dict literal, and the same three as
keyword arguments. A raw grep over the file would also read comments and
docstrings, which are French across most of this repository by history -- CLAUDE's
English rule is more recent than the tree -- and a guard that fires on those is a
guard nobody can keep green.

ITS REACH, STATED SO IT IS NOT MISTAKEN FOR MORE. A message composed by
concatenation at runtime, or fetched from a table, is invisible here. This is the
same limit `AI-232` records for the firing-message guard: an AST check sees the
form it was taught. It is a net under the common form, not a proof.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

CORE = pathlib.Path(__file__).resolve().parents[2] / "core"

#: The three keys this repository composes operator sentences under.
OPERATOR_KEYS = frozenset({"message", "reason", "explanation"})

#: French function words. Chosen to be words that are NOT English words, so an
#: English sentence cannot collect them by accident -- `sur`, `son`, `mine` and
#: other cross-language traps are deliberately absent.
FRENCH_MARKERS = frozenset({
    # Les mots-outils, qui sont ce qui trahit une phrase francaise. La premiere
    # version de cette liste ne portait que des mots longs et n'aurait PAS attrape
    # << Expose exactly one account avant de recommander un rapport >> : un seul
    # marqueur sur toute la phrase. C'est le test du bas qui l'a montre, avant
    # que le garde ne serve.
    "de", "du", "des", "le", "la", "les", "un", "une", "aux", "ce", "ces",
    "cette", "ne", "se", "qui", "que", "quoi", "ses", "leur", "nos", "vos",
    "dont", "car", "afin", "lors", "selon", "entre", "chaque", "aucun",
    "aucune", "avant", "apres", "avec", "dans", "depuis", "doit", "elle",
    "est", "etait", "etaient", "etre", "faire", "impossible", "jamais",
    "mais", "meme", "pour", "pourquoi", "quand", "sans", "sont", "toujours",
    "tous", "toutes", "vous", "ainsi", "alors", "donc", "puis", "aussi",
    "encore", "par", "sur", "plus",
})

#: Two distinct markers, not one. A single hit is how a false alarm is born --
#: `les` inside a quoted identifier, `est` in a foreign proper noun. Every real
#: case measured on 2026-08-08 carried four or more.
MIN_MARKERS = 2

# `[^\W\d_]+` : les lettres de n'importe quelle langue, sans chiffres ni blancs.
# La premiere version enumerait les accents a la main et un caractere mal encode y a
# glisse UN ESPACE, ce qui faisait rendre la phrase entiere comme un seul << mot >> :
# le garde ne matchait plus aucun marqueur et passait au vert sur tout. Il n'a jamais
# servi dans cet etat -- c'est le test du bas qui l'a attrape, et c'est pour cela
# qu'il existe.
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _markers(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)} & FRENCH_MARKERS


def _literal(node: ast.AST) -> str | None:
    """The text of a string literal, including an f-string's fixed parts.

    An f-string is where the mixed sentences were found, so reading only plain
    `ast.Constant` would miss the exact class this guard exists for.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _operator_strings(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in OPERATOR_KEYS
                    and (text := _literal(value)) is not None
                ):
                    found.append((getattr(value, "lineno", 0), text))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in OPERATOR_KEYS and (text := _literal(kw.value)) is not None:
                    found.append((getattr(kw.value, "lineno", 0), text))
    return found


#: LA DETTE MESUREE, PAR FICHIER, ET ELLE NE PEUT QUE DESCENDRE. 66 chaines sur
#: 13 fichiers au 2026-08-08 -- AI-216 en annoncait << six fichiers serveur au
#: moins >>, ce qui etait vrai et deux fois trop petit.
#:
#: Pourquoi un cliquet plutot qu un rouge franc : traduire 66 phrases a l aveugle
#: en fin de session casserait les tests qui en assertent certaines mot pour mot,
#: et un garde qui rougit sur un etat que personne n a decide de changer est un
#: garde qu on apprend a ignorer. Le cliquet, lui, refuse la 67e.
#:
#: DEUX TESTS LE TIENNENT, et le second est celui qui manquait partout ailleurs
#: dans ce depot : l un refuse qu un compte MONTE, l autre refuse qu il reste trop
#: HAUT apres une reparation. Sans le second, un nom repare resterait inscrit pour
#: toujours et le cliquet deviendrait un plafond -- exactement ce qui est arrive au
#: cliquet dbt, qui a mis douze jours a redescendre faute de se lire.
FRENCH_DEBT: dict[str, int] = {
    # AD-40, 2026-08-12 : 13 des 33 lignes d'`admin_api.py` ont DEMENAGE avec la
    # famille `/api/datastreams/…`, exactement comme les trois de `main.py`
    # ci-dessous. Total inchange, dette inchangee -- c'est l'adresse qui bouge,
    # et la traduire dans le meme commit rendrait illisible un diff dont tout
    # l'interet est de se lire << ce code a bouge >>.
    # AD-43, 2026-08-12 : cinq lignes de plus ont DEMENAGE avec les quatre
    # sujets de `/api/projects`. Total inchange, dette inchangee -- c'est
    # l'adresse qui bouge, comme pour AD-40 et pour le decoupage de `main.py`.
    # AD-43, 2026-08-13 : douze lignes de plus ont DEMENAGE avec le parcours
    # OAuth Google. `admin_api.py` passe de 14 a 2 -- le total du depot est
    # inchange, c'est l'adresse qui bouge.
    # AD-43, 2026-08-13 : les deux derniers messages francais d'`admin_api` sont
    # partis avec leurs sujets -- et ont ete REPARES a l'arrivee plutot que
    # reinscrits sous un autre nom de fichier. Un cliquet qui suit le
    # deplacement sans le voir n'est pas un cliquet.
    "admin_api.py": 0,
    "google_oauth_api.py": 12,
    "projects_api.py": 2,
    "org_members_api.py": 1,
    "project_connections_api.py": 3,
    "datastreams_api.py": 8,
    "datastream_collection_api.py": 1,
    "datastream_mapping_api.py": 1,
    "datastream_executions_api.py": 1,
    "bounded_recovery.py": 1,
    "cards_api.py": 2,
    # Les trois lignes de `main.py` ont DEMENAGE, elles n'ont pas ete creees :
    # le decoupage de l'entrypoint par surface (2026-08-11) a emmene deux
    # messages avec `get_card` et un avec la publication d'insight. Total
    # inchange, dette inchangee -- c'est l'adresse qui bouge.
    # 2026-08-16 : les deux nombres ci-dessous DESCENDENT, sur la demande du
    # cliquet lui-meme (`test_the_recorded_debt_is_not_stale`). Le message
    # francais de `daily_insight_mcp` est parti avec les imports morts d'AI-269 ;
    # celui de `cards_mcp` etait deja repare dans l'arbre sans que sa ligne
    # redescende. Un plafond au-dessus de l'arbre laisse rentrer une regression
    # sans rougir.
    # 2026-08-17, chantier 67-14b : `cards_mcp` et `dimension_labels_mcp` passent a
    # ZERO. Le balayage francais de la surface MCP a traduit leurs microcopies avec
    # celles des treize autres `*_mcp.py` -- le cliquet reclame la descente dans le
    # meme commit (`test_the_recorded_debt_is_not_stale`), et la voici.
    "cards_mcp.py": 0,
    "context_events.py": 1,
    "daily_insight_mcp.py": 0,
    # 2026-08-25, story 49.3 AC1 : `datamodel_api` passe a ZERO sans traduction.
    # Ses deux messages francais vivaient dans les corps d'ecriture (creation et
    # upsert de mapping) ; les cinq portes d'ecriture sont retirees, elles rendent
    # 409 `legacy_store_is_read_only` avec une phrase anglaise qui nomme le geste,
    # et les corps sont partis avec le francais dedans. Le cliquet reclame la
    # descente dans le meme commit, et la voici.
    "datamodel_api.py": 0,
    "dimension_labels_mcp.py": 0,
    "dq_api.py": 6,
    "flows.py": 1,
    "flows_api.py": 1,
    "health_enrichment.py": 1,
    "host_preflight.py": 1,
    # 2026-08-24 : 9 -> 4. Cinq messages francais sont partis avec les trois
    # handlers d'import de la story 22.2 (`_set_import_contract`,
    # `_list_import_contracts`, `_import_workbook`), retires en meme temps que le
    # SECOND moteur xlsx dont ils etaient l'unique porte -- « Contrat d'import
    # invalide. », « Le nom d'onglet est requis. », « Fichier trop volumineux »,
    # « Le champ file_base64 est requis. », « Erreur lors de l'import du
    # fichier ». Ce n'est donc PAS une traduction : c'est de la dette qui
    # disparait avec son code, et le cliquet reclame la descente dans le meme
    # commit. Les 4 qui restent sont sur les routes encore montees et sont
    # toujours de la dette a traduire.
    "mediaplan_api.py": 4,
}


def _offenders(path: pathlib.Path) -> list[tuple[int, str, list[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (lineno, text, sorted(hits))
        for lineno, text in _operator_strings(tree)
        if len(hits := _markers(text)) >= MIN_MARKERS
    ]


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_no_new_french_operator_message(path: pathlib.Path) -> None:
    """A file may not gain a French `message` / `reason` / `explanation`."""
    offenders = _offenders(path)
    allowed = FRENCH_DEBT.get(path.name, 0)
    listing = [
        f"  {path.name}:{lineno} ({', '.join(hits)}): {text[:120]}"
        for lineno, text, hits in offenders
    ]
    assert len(offenders) <= allowed, (
        f"{path.name} carries {len(offenders)} French operator strings, "
        f"{allowed} are recorded. All of them:\n" + "\n".join(listing)
    )


def test_the_recorded_debt_is_not_stale() -> None:
    """A repaired string must LEAVE the table in the same commit.

    This is the half the dbt ratchet did not have until 2026-08-08, and its
    absence is why that one sat at 41 for twelve days: a count that may only be
    an upper bound never comes down on its own.
    """
    stale = {
        name: (len(_offenders(CORE / name)), allowed)
        for name, allowed in FRENCH_DEBT.items()
        if (CORE / name).is_file() and len(_offenders(CORE / name)) < allowed
    }
    assert not stale, (
        "FRENCH_DEBT is higher than the tree -- lower these numbers, the repair "
        f"is already done: {stale}"
    )


def test_the_guard_would_catch_the_shape_it_was_written_for() -> None:
    """The two 2026-08-08 sentences, verbatim, must be refused.

    Without this, a later simplification of the marker list could silently make
    the guard blind to the very class it was built for and still pass -- the
    defect this repository names `vert par absence`.
    """
    mixed = ast.parse(
        'x = {"explanation": "No exposed account is eligible. '
        'Expose exactly one account avant de recommander un rapport."}\n'
        'y = f"Cannot delete field: it is used by {n} mapping(s) actif(s). '
        'Retirez les mappings avant de supprimer ce champ."\n'
        'z = dict(reason="No retained source data: first run a synchronisation '
        'ou un rechargement, car le retraitement ne rappelle jamais la source.")\n'
    )
    caught = [t for _, t in _operator_strings(mixed) if len(_markers(t)) >= MIN_MARKERS]
    assert len(caught) == 2, caught


# ---------------------------------------------------------------------------
# THE SERVED FORMS THE TABLE ABOVE NEVER SAW (chantier 67-14b, 2026-08-17).
# ---------------------------------------------------------------------------
#
# Everything above reads ONE shape: a `message` / `reason` / `explanation` key or
# keyword. The MCP surface does not speak that shape. It raises
# `_tool_error(code, message)` -- the sentence is the SECOND POSITIONAL argument --
# it returns `_result(summary, data)` with the sentence FIRST, and the App tools
# answer with `TextContent(type="text", text=...)`. Three served forms, none of
# them visible to the guard, which is why `feedback_mcp` could answer
# "Merci pour votre retour." for months with the ratchet green: the audit of
# 2026-08-17 (report 10) named exactly this -- "sinon la classe se reproduit hors
# de son instrument".
#
# NO DEBT TABLE HERE, DELIBERATELY. The sweep of 67-14b took these three forms to
# ZERO across `server/core` (17 MCP modules plus `metric_semantics_api`,
# `publication_reviews_api`, `conflict_resolutions*`, `datastream_diagnosis`), so
# the honest guard is a hard floor, not a ratchet. A table would only invite the
# next sentence to be recorded instead of translated.
#
# ITS REACH, stated so it is not mistaken for more: same AST limit as above -- a
# sentence built by concatenation at runtime, or read from a table, is invisible.

#: The served call shapes, and WHICH argument carries the human sentence.
_SERVED_POSITIONAL = {"_tool_error": 1, "_result": 0}

#: WHY A SECOND WORD LIST, and why the first one could not be reused as-is.
#: `FRENCH_MARKERS` above is deliberately function-words-only, and it needs TWO of
#: them -- a rule tuned for the long mixed SENTENCES of 2026-08-08. The MCP surface
#: refuses in three words: "Flux introuvable.", "datastream_id est requis.",
#: "Ressource introuvable." Those carry ZERO function words, so the guard written
#: on the first list alone was green on every one of them. Measured while writing
#: this: its own self-test caught it.
#:
#: So: words that are French and have NO English twin, where ONE is decisive.
#: `flux` is deliberately ABSENT -- it is an English noun AND the wire spelling of
#: `app.project_flux` and the `flux:` resource path, so it would fire on SQL and on
#: authorization tokens that are identifiers, not copy.
FRENCH_STRONG = frozenset({
    "introuvable", "indisponible", "requis", "requise", "invalide", "refusee",
    "refusees", "perimee", "expiree", "deja", "gouvernee", "gouverne",
    "confirmee", "preparee", "modifiee", "enregistree", "declaree", "rejete",
    "rejetee", "choisir", "veuillez", "merci", "echec", "donnees", "champ",
    "horloge", "metrique", "ressource", "aucune", "inchange", "inchangee",
})


def _strong(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)} & FRENCH_STRONG


def _served_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Sentences passed positionally to the tool helpers, or as `text=`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in _SERVED_POSITIONAL:
            index = _SERVED_POSITIONAL[name]
            if len(node.args) > index and (text := _literal(node.args[index])) is not None:
                found.append((getattr(node.args[index], "lineno", 0), text))
        for kw in node.keywords:
            if kw.arg == "text" and (text := _literal(kw.value)) is not None:
                found.append((getattr(kw.value, "lineno", 0), text))
    return found


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_no_french_in_a_served_tool_string(path: pathlib.Path) -> None:
    """A tool error, a tool summary and a `TextContent` are all English. Zero."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        f"  {path.name}:{lineno} ({', '.join(sorted(hits))}): {text[:120]}"
        for lineno, text in _served_strings(tree)
        if (hits := _markers(text) | _strong(text))
        and (len(_strong(text)) >= 1 or len(_markers(text)) >= MIN_MARKERS)
    ]
    assert not offenders, (
        f"{path.name} serves French to an agent or an operator through a tool "
        "helper. Translate it -- there is no debt table for this shape:\n"
        + "\n".join(offenders)
    )


def test_the_served_guard_reads_the_forms_the_key_guard_cannot() -> None:
    """Positional `_tool_error`, positional `_result`, and `text=` are all read.

    The same `vert par absence` protection the key guard carries: if a later edit
    narrowed `_SERVED_POSITIONAL` or dropped the `text=` branch, the floor above
    would still pass on a clean tree and protect nothing.
    """
    sample = ast.parse(
        'raise _tool_error("not_found", "Flux introuvable.")\n'
        'return _result("Revue gouvernee de la proposition, secret non expose.", d)\n'
        'c = [TextContent(type="text", text="Merci pour votre retour a tous.")]\n'
        'ok = _tool_error("not_found", "Datastream not found.")\n'
    )
    caught = [
        t for _, t in _served_strings(sample)
        if len(_strong(t)) >= 1 or len(_markers(t)) >= MIN_MARKERS
    ]
    assert len(caught) == 3, caught
    # And the English line beside them is NOT caught -- a floor that fires on
    # "Datastream not found." would be abandoned within a week.
    assert "Datastream not found." not in caught
