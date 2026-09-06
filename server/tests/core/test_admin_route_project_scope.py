"""AI-171 -- aucune route projet d'``admin_api.py`` n'autorise plus par accident.

**Ce que ce fichier tient, et pourquoi il est ecrit en AST plutot qu'en requetes.**

Le constat annoncait vingt-sept routes « qui s'authentifient sans qu'on voie leur
autorisation », et disait de son propre instrument qu'il avait deja rendu 96 puis
10 faux positifs. Il en restait onze une fois l'instrument rendu monotone, et
elles ont ete OUVERTES une par une :

* **cinq etaient gardees** par une garde que l'instrument ne connaissait pas --
  ``_enforce_platform_admin`` (le tick DQ interne), ``_enforce_org_manage`` (lier
  et delier un flux), ``identity_has_org_access`` (lister les projets d'un flux),
  ``identity_can_manage_org`` (revoquer une autorisation). Aucune n'etait un
  defaut ; c'est le vocabulaire de gardes qui etait incomplet, et il l'est
  desormais ici.
* **six ne l'etaient pas**, et le sont maintenant :

  =========================== ========== ============================================
  handler                     capability ce qui passait avant
  =========================== ========== ============================================
  ``_health_proxy``           view       ``get("project_id", "default")`` -- le motif
                                         que la story 7.1 AC5 interdit, mot pour mot
  ``_list_alert_definitions`` view       seuils d'alerte d'un autre tenant
  ``_create_alert_definition``edit       ecriture chez le tenant de son choix
  ``_delete_notebook``        (notebook) seule ecriture notebook sans son propre helper
  ``_delete_project``         manage     archivage + revocation de TOUTES ses connexions
  ``_rotate_project_key``     manage     rotation de la cle de chiffrement d'un tenant
  =========================== ========== ============================================

**Pourquoi un gate AST.** Une garde retiree par un refactor ne se rattrape pas au
comportement si la meme session adapte les doublures -- c'est l'idiome deja
retenu par ``test_project_resolver_gate`` et par le cliquet d'AI-125. Et le
verdict qui compte est de CLASSE : pas « ces six-la sont gardees » mais « aucune
route projet ne l'est pas ». Une septieme qui paraitrait sans garde fait rougir
ce fichier le jour ou elle est ecrite.

**L'instrument, et sa seule propriete non negociable : la monotonie.** Le
verdict ne peut se lire que si chercher PLUS loin les gardes rend MOINS de
routes. La premiere version lisait « cette route touche a un projet » sur les
corps transitifs et rendait 72 / 101 / 65 aux profondeurs 1 / 2 / 3 -- un compte
qui MONTE quand on cherche mieux mesure autre chose que ce qu'il annonce. Le
``project_id`` vient de la REQUETE : seul le corps du handler peut le lire. Avec
cette correction : 66 / 55 / 17 / 11 / 11, convergent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ADMIN_API = Path(__file__).resolve().parents[2] / "core" / "admin_api.py"
CORE_DIR = ADMIN_API.parent

# Toute fonction dont l'appel PROUVE que la portee est bornee. Une garde absente
# de cette liste produit un faux positif -- c'est ainsi que les cinq routes flux
# et internes ont d'abord ete accusees a tort. L'ajouter est la bonne reaction,
# APRES avoir ouvert la route et verifie qu'elle borne vraiment.
SCOPE_GUARDS = frozenset(
    {
        # portee projet
        "_strict_project_capability_allowed",
        "_refuse_unless_project_allowed",
        "resolve_strict_resource_access",
        "_enforce_notebook_project_scope",
        "_resolve_conn_project_scoped",
        "identity_can_read_project",
        # portee organisation -- une route qui borne par l'org borne ses projets
        "_enforce_org_manage",
        "identity_has_org_access",
        "identity_can_manage_org",
        # portee plateforme -- l'allow-list TOOROW_SUPER_ADMINS
        "_enforce_platform_admin",
        # seam gouvernee : l'autorisation est portee par l'operation
        "execute_operation",
    }
)

AUTHN = frozenset({"_check_auth", "_check_canonical_principal", "_authorize_internal"})

# Combien de niveaux d'appel sont suivis pour trouver une garde. Genereux par
# construction : une garde invoquee deux etages plus bas EST une garde, et un
# instrument qui l'ignore accuse a tort.
GUARD_SEARCH_DEPTH = 5


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, node)
    return found


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            target = sub.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _reads_a_project(node: ast.AST) -> bool:
    """Le handler lit-il un identifiant de projet DANS SON PROPRE CORPS ?

    Volontairement non transitif : le ``project_id`` arrive par la requete, donc
    c'est le handler qui le lit. Elargir aux corps appeles est ce qui rendait le
    compte croissant avec la profondeur.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value in ("project_id", "projectId"):
            return True
        if isinstance(sub, ast.Name) and sub.id == "project_id":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "project_id":
            return True
    return False


def _mounted_handlers(tree: ast.Module) -> list[tuple[str, str]]:
    """(chemin, nom du handler) pour chaque ``Route(...)`` de la table.

    Le handler passe par mot-cle ``endpoint=`` compte autant que le positionnel :
    la table les melange, et n'en lire qu'un rendait « 0 route » -- un vert a
    vide, exactement ce que ce depot refuse ailleurs.
    """
    mounted: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Route" or not node.args:
            continue
        path = node.args[0].value if isinstance(node.args[0], ast.Constant) else "?"
        handler = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Name):
            handler = node.args[1].id
        for keyword in node.keywords:
            if keyword.arg == "endpoint" and isinstance(keyword.value, ast.Name):
                handler = keyword.value.id
        if handler:
            mounted.append((str(path), handler))
    return mounted


#: `server/core/**/*.py` on 2026-08-31, and the functions they parse to. FLOORS,
#: not equalities. Criterion 13 of
#: `docs/product-architecture/module-boundaries.md`: the two tests that walk
#: this tree both say `assert not <offenders>`, and an unreachable `core/` makes
#: that sentence true without reading a line.
_CORE_MODULES_AT_2026_08_31 = 528
_CORE_FUNCTIONS_AT_2026_08_31 = 6124


def test_the_walk_still_reaches_core() -> None:
    """La marche doit rougir ICI si `core/` a bouge, pas rester sereinement verte."""
    modules = sorted(CORE_DIR.rglob("*.py"))
    functions = _core_functions()
    assert len(modules) >= _CORE_MODULES_AT_2026_08_31, (
        f"{len(modules)} modules lus sous {CORE_DIR}, "
        f"{_CORE_MODULES_AT_2026_08_31} le 2026-08-31 -- l'arbre a bouge et les "
        "verdicts ci-dessous ne portent sur rien."
    )
    assert len(functions) >= _CORE_FUNCTIONS_AT_2026_08_31, (
        f"{len(functions)} fonctions analysees, "
        f"{_CORE_FUNCTIONS_AT_2026_08_31} le 2026-08-31 -- la marche s'arrete "
        "avant d'atteindre les handlers qu'elle est censee suivre."
    )


def _core_functions() -> dict[str, ast.AST]:
    """Toutes les fonctions de ``core/``, pour que la marche quitte admin_api.py."""
    admin_tree = _parse(ADMIN_API)
    assert admin_tree is not None, "admin_api.py ne s'analyse plus"
    everything: dict[str, ast.AST] = dict(_functions(admin_tree))
    for module in sorted(CORE_DIR.rglob("*.py")):
        tree = _parse(module)
        if tree is None:
            continue
        for name, node in _functions(tree).items():
            everything.setdefault(name, node)
    return everything


def _unguarded_project_routes() -> list[str]:
    admin_tree = _parse(ADMIN_API)
    assert admin_tree is not None
    admin_functions = _functions(admin_tree)
    all_functions = _core_functions()

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        frontier = {start}
        for _ in range(GUARD_SEARCH_DEPTH):
            following: set[str] = set()
            for name in frontier:
                node = all_functions.get(name)
                if node is None:
                    continue
                for callee in _calls(node):
                    if callee not in seen:
                        seen.add(callee)
                        following.add(callee)
            frontier = following
            if not frontier:
                break
        return seen

    offenders: list[str] = []
    for path, handler in _mounted_handlers(admin_tree):
        node = admin_functions.get(handler)
        if node is None or not _reads_a_project(node):
            continue
        reached = reachable(handler)
        if not (reached & AUTHN):
            continue  # une route sans authentification releve d'un autre verdict
        if reached & SCOPE_GUARDS:
            continue
        offenders.append(f"{path} -> {handler} (admin_api.py:{node.lineno})")
    return sorted(offenders)


def test_no_project_route_authenticates_without_authorizing():
    """LE VERDICT DE CLASSE. Zero route projet montee sans garde de portee."""
    offenders = _unguarded_project_routes()
    assert not offenders, (
        "des routes projet s'authentifient sans borner leur portee (AI-171, "
        "story 7.1 AC5). Ouvrir chacune : soit elle borne par une garde que "
        "SCOPE_GUARDS ne connait pas encore -- l'y ajouter APRES l'avoir "
        "verifiee -- soit elle sert le projet d'un autre tenant :\n  "
        + "\n  ".join(offenders)
    )


def test_the_instrument_is_monotonic_in_its_search_depth():
    """Chercher les gardes PLUS loin ne peut pas rendre PLUS de coupables.

    C'est la propriete qui rend le verdict ci-dessus lisible, et c'est celle que
    la premiere version de cet instrument n'avait pas : elle rendait 72 puis 101
    puis 65. Un compte qui monte quand on cherche mieux mesure autre chose que ce
    qu'il annonce -- et c'est de la que venaient les « 96 puis 10 » faux positifs
    du constat d'origine.
    """
    global GUARD_SEARCH_DEPTH
    original = GUARD_SEARCH_DEPTH
    counts = []
    try:
        for depth in (1, 2, 3, 4, 5):
            GUARD_SEARCH_DEPTH = depth
            counts.append(len(_unguarded_project_routes()))
    finally:
        GUARD_SEARCH_DEPTH = original
    assert counts == sorted(counts, reverse=True), (
        f"l'instrument n'est plus monotone en profondeur : {counts}"
    )


@pytest.mark.parametrize(
    ("handler", "guard"),
    [
        ("_health_proxy", "_refuse_unless_project_allowed"),
        ("_list_alert_definitions", "_refuse_unless_project_allowed"),
        ("_create_alert_definition", "_refuse_unless_project_allowed"),
        ("_delete_notebook", "_enforce_notebook_project_scope"),
        ("_delete_project", "_refuse_unless_project_allowed"),
        ("_rotate_project_key", "_refuse_unless_project_allowed"),
    ],
)
def test_each_repaired_handler_still_calls_its_guard(handler: str, guard: str):
    """Les six nommement, pour que le diff dise laquelle a perdu sa garde.

    Le verdict de classe suffirait, mais il rend une liste ; ceci rend un nom.
    """
    # AD-43 : les handlers ont rejoint le module de LEUR SUJET. Un garde qui
    # cherche une fonction dans un CHEMIN leve `substring not found` le jour ou
    # elle demenage -- ou pire, ne la trouve plus et ne dit rien. Il la cherche
    # donc dans `core/`, la ou elle vit.
    source = next(
        (
            text
            for path in sorted(ADMIN_API.parent.glob("*.py"))
            if f"async def {handler}(" in (text := path.read_text(encoding="utf-8"))
        ),
        "",
    )
    assert source, f"{handler} n'existe plus dans core/ -- il a ete supprime, pas deplace"
    start = source.index(f"async def {handler}(")
    # Le dernier handler d un module n a pas de `async def` apres lui : `.index`
    # levait la ou `.find` rend -1 et laisse lire le corps jusqu au bout.
    end = source.find(chr(10) + "async def ", start + 1)
    end = end if end != -1 else len(source)
    body = source[start:end]
    executable = [
        line for line in body.splitlines() if not line.strip().startswith("#")
    ]
    assert any(f"{guard}(" in line for line in executable), (
        f"{handler} n'appelle plus {guard} (AI-171)"
    )


def test_the_silent_default_binding_has_not_come_back():
    """``or "default"`` / ``get("project_id", "default")`` -- story 7.1 AC5.

    Le premier motif est deja tenu par ``test_project_resolver_gate`` pour
    ``_list_procedures`` ; le second est celui que ``_health_proxy`` portait, et
    aucun gate ne le voyait parce qu'il s'ecrit avec une valeur par defaut plutot
    qu'avec un ``or``. Les deux formes sont refusees ici, sur tout le fichier.

    Sur les lignes EXECUTABLES seulement : la prose de ce fichier et les
    commentaires des handlers citent le motif interdit, et un gate qui se
    declenche sur son propre commentaire ne mesure rien.
    """
    import re

    forbidden = re.compile(
        r"""(or\s*["']default["'])|(get\(\s*["']project_id["']\s*,\s*["']default["']\s*\))"""
    )
    offenders = []
    for number, line in enumerate(ADMIN_API.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip().startswith("#") or "`" in line:
            continue
        if forbidden.search(line):
            offenders.append(f"admin_api.py:{number}: {line.strip()}")
    assert not offenders, (
        "le rattachement silencieux au projet `default` est revenu (story 7.1 AC5) :\n  "
        + "\n  ".join(offenders)
    )
