"""Une erreur que le worker ne sait pas nommer ne declenche AUCUNE des trois decisions.

POURQUOI CE GARDE EXISTE. `core/pull_errors.py` definit une taxonomie fermee, et
`queue.py` s'en sert pour decider trois choses -- pas pour etiqueter :

  1. **Si on reessaie.** `queue.py:1254-1266` : `auth_expired`, `auth_revoked`,
     `permission_denied` et `invalid_request` sont terminales A LA PREMIERE
     TENTATIVE. `provider_transient` et `unclassified` repartent jusqu'a
     `max_attempts` puis `dead_letter`.
  2. **Si l'operateur est prevenu.** `user_action="reconnect"` est porte par la
     classe SEULE et remonte en `recommended_action`
     (`datastream_diagnosis.py:352-358`). Sans classe, l'ecran n'affiche aucune
     action et le Datastream meurt en silence.
  3. **Si la derive de catalogue est signalee.** `invalid_request` emet
     `pull_invalid_request_drift` (`queue.py:1279`), le seul evenement qui dit
     << l'API a bouge sous le connecteur >>.

Une exception qui n'herite pas de `ConnectorError` tombe dans le `except
Exception` generique : `error_class="unclassified"`, `user_action=None`,
retryable. Donc un token expire est rejoue jusqu'au `dead_letter` contre un
credential qui ne marchera jamais, et personne n'est invite a se reconnecter.

MESURE DU 2026-08-01, avant reparation : **12 connecteurs sur 38** levaient 23
classes hors taxonomie sur le chemin de `pull()` -- et neuf d'entre elles
CONNAISSAIENT deja la bonne reponse, ecrite dans leur nom ou leur docstring
(`MondayGraphQLError("auth_expired", ...)` la nomme litteralement) avant de la
perdre dans un `RuntimeError` nu.

CE QUE CE FICHIER NE FAIT PAS. Il ne juge pas SI la classe choisie est la bonne
-- ca, c'est la lecture du site de levee, et elle est faite dans les modules. Il
tient une propriete plus faible et verifiable mecaniquement : ce qui peut
atteindre le worker sait se nommer. C'est exactement la propriete qui manquait,
et aucun test par module ne pouvait la voir puisque chacun n'observait que son
propre connecteur.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"

#: Les deux racines qu'un worker sait lire. `RateLimitError` vit dans
#: `core/quota.py` et garde son propre contrat (elle alimente le disjoncteur de
#: quota, pas la politique de reprise par classe) -- volontairement hors de la
#: hierarchie `ConnectorError`, donc acceptee ici a part.
_CANONICAL_ROOTS = ("ConnectorError", "RateLimitError")


def _module_dirs() -> list[Path]:
    return sorted(p for p in MODULES_DIR.iterdir() if (p / "connector.py").is_file())


def _ids(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def _raised_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call):
            func = child.exc.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def _raised_on_the_pull_path(source: str) -> set[str]:
    """Les exceptions levees par `pull()` ou par ce que `pull()` appelle.

    Fermeture transitive intra-fichier. Elle est volontairement LARGE (elle suit
    tout appel dont le nom existe comme fonction du fichier) : un faux positif se
    lit et se corrige, un faux negatif laisse passer exactement le defaut qu'on
    cherche.
    """
    tree = ast.parse(source)
    funcs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "pull" not in funcs:
        return set()

    seen: set[str] = set()
    stack = ["pull"]
    while stack:
        name = stack.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        stack.extend(_called_names(funcs[name]))

    return {name for fn in seen for name in _raised_names(funcs[fn])}


def _classes_defined_here(source: str) -> set[str]:
    return {node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef)}


def _import_connector(module_dir: Path):
    sys.path.insert(0, str(MODULES_DIR.parent))
    try:
        return importlib.import_module(f"modules.{module_dir.name.replace('-', '_')}.connector")
    except Exception:  # noqa: BLE001 -- re-tente sous le nom reel du repertoire
        spec = importlib.util.spec_from_file_location(
            f"_taxonomy_probe_{module_dir.name.replace('-', '_')}",
            module_dir / "connector.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids(_module_dirs()))
def test_what_pull_can_raise_knows_how_to_name_itself(module_dir: Path) -> None:
    """Toute exception PROPRE au module et atteignable depuis `pull()` est canonique.

    Le filtre est volontairement restreint aux classes DEFINIES dans le fichier :
    une `ValueError` ou un `KeyError` du langage tombent deja, et par dessein, dans
    le filet `unclassified` -- ce garde ne demande pas au connecteur de typer ce
    qu'il n'a pas vu venir. Il demande que ce qu'il a explicitement DECLARE comme
    un mode d'echec sache se nommer.
    """
    source = (module_dir / "connector.py").read_text(encoding="utf-8")
    own = _classes_defined_here(source)
    raised = _raised_on_the_pull_path(source) & own
    if not raised:
        return

    connector = _import_connector(module_dir)

    untyped: list[str] = []
    for name in sorted(raised):
        cls = getattr(connector, name, None)
        assert cls is not None, (
            f"{module_dir.name}.{name} est leve mais introuvable a l'import -- "
            "l'instrument est casse, pas le connecteur"
        )
        lineage = {base.__name__ for base in cls.__mro__}
        if not lineage & set(_CANONICAL_ROOTS):
            untyped.append(f"{name}({', '.join(b.__name__ for b in cls.__bases__)})")

    assert not untyped, (
        f"{module_dir.name} : ces exceptions atteignent le worker sans classe canonique, "
        f"donc `unclassified` -- rejouees jusqu'au dead_letter, sans action affichee : "
        f"{untyped}"
    )


def test_the_probe_reads_the_whole_fleet() -> None:
    """Le garde couvre les 38 connecteurs, pas ceux qui veulent bien s'importer.

    Sans cette assertion, retirer un `connector.py` ou casser la decouverte rendrait
    la suite verte en ne mesurant plus rien -- un skip n'est jamais un constat
    positif.
    """
    assert len(_module_dirs()) == 39, [p.name for p in _module_dirs()]
