"""Un test qui pilote dbt doit declarer son temps, sinon il tue la session.

CE N'EST PAS UNE REGLE DE STYLE. `server/pyproject.toml` pose `timeout = 180` et
`timeout_method = "thread"`. La methode « thread » ne peut PAS interrompre un
`subprocess.run` bloquant : quand le plafond tombe, pytest vide la pile et
**arrete toute la session**. Le fichier fautif n'echoue donc pas tout seul --
tout ce qui restait a jouer apres lui n'est jamais rapporte, et la suite ne rend
ni vert ni rouge. Elle ne rend RIEN.

Mesure du 2026-08-05, en essayant de repondre a la norme d'avant-deploiement
« lancer la suite pg-gated » :

    python -m pytest server/tests -q
    -> +++ Timeout +++  dans test_org_mart_equivalence_24_4.py, aucun compte

    (deselectionne)     -> +++ Timeout +++  dans test_seed_to_mart_loop.py,
                           fixture `seeded_db`, appel `dbt seed`

Deux fichiers, la meme cause, et la meme consequence : la norme etait
inapplicable et personne ne le voyait, parce qu'un resume tronque ressemble a un
resume.

ET LES TESTS N'ETAIENT PAS CASSES, ILS ETAIENT AFFAMES. Une fois le plafond leve :

    python -m pytest tests/integration/test_org_mart_equivalence_24_4.py -q
    -> 1 passed in 347.80s (0:05:47)

348 secondes de travail sous un plafond de 180. La garde ci-dessous ferme la
classe : le prochain fichier qui lancera dbt sans declarer son temps sera rouge
ICI -- proprement, avec son nom -- plutot que muet en emportant la suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]

#: Ce qui compte comme « piloter dbt » : le module CLI, qui est la SEULE facon
#: dont ce depot le lance (`python -m dbt.cli.main`). Mesure 2026-08-05 :
#: `grep -c dbt.cli.main` rend 2, 5 et 2 sur les trois fichiers qui le pilotent,
#: et 0 sur les deux qui n'en parlent que dans leur prose
#: (`test_dbt_span.py` decrit `scheduler.run_dbt` et le simule) ou qui n'ont
#: qu'un chemin nomme `dbt` (`test_epic41_verification_overlay_additive.py`,
#: dont le seul sous-processus est `git show`). Chercher le mot nu « dbt »
#: attraperait ces deux-la et rendrait la garde bruyante donc ignoree.
_DBT_TOKEN = "dbt.cli.main"


def _test_files() -> list[Path]:
    return [
        path
        for path in _TESTS_ROOT.rglob("test_*.py")
        if "__pycache__" not in path.parts
    ]


def _spawns_dbt(source: str) -> bool:
    """Le fichier lance-t-il REELLEMENT un dbt en sous-processus ?

    Les deux moities comptent, et il faut les DEUX. Un fichier qui parle de dbt
    dans sa prose (`test_dbt_span.py` decrit `scheduler.run_dbt` et le simule) ne
    paie aucun temps ; un fichier qui appelle `subprocess.run(["git", ...])` non
    plus. Seule leur rencontre coute les minutes qui font tomber le plafond.

    LE JETON N'EST PAS CHERCHE DANS LES ARGUMENTS DE L'APPEL, et c'est le point.
    `test_currency_rederivation.py` construit `cmd = [sys.executable, "-m",
    "dbt.cli.main", ...]` puis appelle `subprocess.run(cmd, ...)` : une garde qui
    ne lit que les arguments voit `cmd` et conclut « pas de dbt ». C'est
    exactement l'indirection par variable qui avait desarme le scanner AD-2, et
    elle m'a echappe ici aussi a la premiere ecriture. Plutot que de resoudre les
    liaisons -- fragile des qu'un nom traverse une fonction -- la question est
    posee au niveau du FICHIER : il lance un sous-processus, et son code
    executable nomme le CLI dbt.

    Les commentaires et docstrings sont retires avant la recherche du jeton : une
    garde qui compte la prose devient bruyante, et une garde bruyante est
    desactivee.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    spawns = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name in ("run", "Popen", "check_call", "check_output"):
            spawns = True
            break
    if not spawns:
        return False

    # `ast.unparse` du module entier : les commentaires ont disparu au parse, et
    # les docstrings sont rendues en litteraux qu'on retire explicitement.
    executable = ast.unparse(tree)
    for docstring in _docstrings(tree):
        executable = executable.replace(docstring, "")
    return _DBT_TOKEN in executable


def _docstrings(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = ast.get_docstring(node, clean=False)
            if text:
                found.append(text)
    return found


def _declares_a_ceiling(source: str) -> bool:
    return "pytest.mark.timeout" in source


def test_every_dbt_driving_test_declares_its_own_ceiling():
    offenders: list[str] = []
    for path in _test_files():
        source = path.read_text(encoding="utf-8", errors="ignore")
        if _spawns_dbt(source) and not _declares_a_ceiling(source):
            offenders.append(str(path.relative_to(_TESTS_ROOT)))
    assert not offenders, (
        "ces fichiers lancent dbt sans declarer leur temps, donc ils n'echoueront "
        f"pas : ils arreteront la session et masqueront tout ce qui suit -- {offenders}. "
        "Ajouter `pytest.mark.timeout(<secondes>)` au `pytestmark` du fichier, avec "
        "la mesure qui justifie le nombre. Ne PAS les desactiver : les deux cas "
        "connus etaient verts, seulement affames."
    )


def test_the_two_known_dbt_files_still_carry_theirs():
    """Les deux fichiers reellement mesures, nommes plutot que sous-entendus.

    La garde generique passerait aussi si ces fichiers disparaissaient ou
    cessaient d'appeler dbt. Les nommer rend la disparition visible au lieu de la
    laisser ressembler a un succes.
    """
    for relative in (
        "integration/test_org_mart_equivalence_24_4.py",
        "integration/test_seed_to_mart_loop.py",
    ):
        source = (_TESTS_ROOT / relative).read_text(encoding="utf-8")
        assert _spawns_dbt(source), f"{relative} ne pilote plus dbt -- garde a relire"
        assert _declares_a_ceiling(source), f"{relative} a perdu son plafond"
