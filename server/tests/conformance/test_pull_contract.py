"""Un connecteur est-il PILOTABLE par le worker, sans jamais le connecter ?

POURQUOI CE FICHIER EXISTE. Le 2026-07-31, un premier appel reel au connecteur
`gsc` -- le module que la skill `add-connector` designe comme reference mecanique
-- a echoue en::

    TypeError: pull() missing 1 required positional argument: 'site_url'

L'erreur etait DOCUMENTEE avant d'etre commise. `docs/adding-a-connector.mdx` et
`server/modules/README.md` fixent la signature ET le retour::

    def pull(connection_id, date_from, date_to, project_id, pull_id) -> dict
    ...
    return {"pull_id": ..., "row_count": ..., "date_from": ..., "date_to": ...}

et rangent le compte du cote de la SELECTION (`account_topology` +
`discover_accounts`), jamais des arguments requis. Aucune couche de conformance
ne verifiait cette signature, donc le contrat et le code pouvaient diverger sans
que rien ne rougisse -- et la seule chose qui trouvait l'ecart etait un compte
reel. C'est precisement ce qu'un depot de 38 connecteurs ne peut pas se
permettre : personne ne dispose des 38 comptes.

CE QUE CE FICHIER PROUVE (hors ligne, sans compte, sans reseau)
---------------------------------------------------------------
1. Chaque point d'entree que le manifeste declare EXISTE dans `connector.py` --
   soit en `def`, soit lie a une fabrique (`pull_x = _make_profile_pull(...)`).
2. Aucun de ces points d'entree n'exige un argument que le worker ne passe pas.
3. Chacun RETOURNE l'enveloppe documentee : chaque sortie atteignable porte
   `pull_id`, `row_count`, `date_from`, `date_to`.
4. Une `account_topology` declaree respecte le contrat de core
   (`core.account_topology.validate_topology`), sa fonction de decouverte
   existe, et le compte choisi par un humain a un chemin jusqu'a CHAQUE point
   d'entree de profil -- pas seulement jusqu'a `pull`.
5. Le compte n'est jamais lu dans la selection de RAPPORT.
6. Une `error_map` declaree a une forme que `core.pull_errors.classify_http_error`
   sait consulter, des valeurs de la taxonomie, au moins une entree qui RAFFINE
   la classification pure-HTTP, et un lecteur dans le connecteur.

CE QU'IL NE PROUVE PAS -- la frontiere, dite explicitement
----------------------------------------------------------
- Que l'API distante repond ce qu'on croit. Aucun test hors ligne ne le peut ;
  c'est le role de `scripts/ratify_connector.py` contre un compte reel.
- Que la VALEUR de `row_count` est juste : on prouve que la cle existe a chaque
  sortie, pas que le compte de lignes est exact, ni que des lignes ont atterri.
- Que la branche prise a l'execution est celle qu'on a lue. L'analyse suit
  toutes les sorties atteignables depuis le point d'entree declare, y compris
  celles d'un helper partage entre profils : c'est une SUR-approximation
  assumee (une sortie non conforme est signalee meme si un profil donne ne la
  prend jamais), jamais une sous-approximation.
- Que les codes declares dans `error_map` sont ceux que le provider emet
  reellement. On prouve que la map est CONSULTABLE et VIVANTE, pas qu'elle est
  exacte -- seule la sonde live le dit.
- Rien du corps du pull : ni la requete emise, ni le parsing, ni l'atterrissage.

LE PONT WORKER <-> CONNECTEUR, lu et non suppose
------------------------------------------------
- `core/main.py:243-278` (`get_module_pull_fn`) : le worker resout le callable
  du profil ACTIF via `source_capabilities.reports[].dispatch.callable`, et
  echoue FERME (`None`) si le nom declare n'existe pas. `pull` n'est que le
  chemin par defaut, pas le seul point d'entree.
- `core/loader.py::dispatch_pull` : meme resolution, avec un `ValueError`
  explicite (`declared callable=... is unavailable`).
- `core/queue.py` (branche `_execute_job`) : le callable est appele par mot-cle
  avec exactement `connection_id, date_from, date_to, project_id, pull_id`,
  plus `selection=` UNIQUEMENT quand le profil actif est `catalog_driven`, plus
  les kwargs de compte et d'entite suivie.
- `core/queue.py::_account_kwargs` : le compte selectionne est passe sous le nom
  que le manifeste DECLARE (`account_topology.pull_parameter`), avec un repli
  historique sur `account_id, site_url, property_id, account` -- et seulement si
  une selection existe. Un parametre de compte requis est donc un defaut a deux
  temps : sans selection le pull leve un TypeError nu au lieu d'une erreur typee.
- `core/queue.py` apres l'appel : `row_count = result.get("row_count", 0) if
  isinstance(result, dict) else 0`. Une enveloppe sans `row_count` n'est pas une
  erreur : elle journalise ZERO ligne pour un pull qui en a peut-etre ecrit dix
  mille.

AUCUN SKIP N'EST UN CONSTAT POSITIF. Un `skip` dont le message dit << c'est
declare, donc je ne verifie rien >> ressemble a un vert et ne prouve rien : la
version du matin en produisait soixante (`topologie declaree`, `error_map
declaree`, `le compte a son parametre declare`). Les verifications ci-dessous
n'ont plus de branche muette -- declare : on verifie la declaration ; non
declare : on exige la note qui dit pourquoi.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from core.account_topology import validate_topology
from core.pull_errors import ERROR_CLASSES, classify_http_error

_MODULES = Path(__file__).resolve().parents[2] / "modules"

#: La signature ratifiee. Tout parametre REQUIS hors de cette liste est un ecart.
DOCUMENTED_REQUIRED = frozenset(
    {"connection_id", "date_from", "date_to", "project_id", "pull_id"}
)

#: Les cles que le contrat declare en retour (`server/modules/README.md`,
#: `docs/adding-a-connector.mdx`). `row_count` est celle que le worker LIT.
DOCUMENTED_ENVELOPE = ("pull_id", "row_count", "date_from", "date_to")

#: LES FABRIQUES D'ENVELOPPE QUE CORE POSSEDE -- AI-307.
#:
#: Un `return <appel>` que l'AST ne sait pas suivre est refuse ici, et c'est la
#: bonne regle par defaut : une cle maison rendue par une fonction opaque est
#: exactement le defaut que ce fichier attrape. Mais `core.pull_envelope`
#: CONSTRUIT l'enveloppe documentee, pour les 39 connecteurs, et ses propres
#: tests l'epinglent (`tests/core/test_queue_prevented_pull.py`). La lister ici
#: n'affaiblit rien : la preuve se deplace du site d'appel vers l'unique
#: fabrique, ou elle est faite une fois au lieu de trente-neuf.
#:
#: Une fonction n'entre dans cette liste que si elle vit dans `server/core/`,
#: qu'elle rend litteralement les quatre cles, et qu'un test le prouve.
#:
#: LE NOM NE SUFFIT PAS, ET C'EST TOUT L'INTERET DE LA LISTE. Version d'origine :
#: `elif callee in CORE_ENVELOPE_BUILDERS`, c'est-a-dire une confiance accordee a
#: une CHAINE. Un connecteur qui ecrivait `from .helpers import
#: prevented_envelope` -- ou n'importe quel import d'un homonyme -- traversait la
#: conformite sans qu'une seule cle soit lue. L'ORIGINE est verifiee :
#: `_core_builders_imported_by` ne retient un nom que si ce fichier le lie par un
#: `from core.pull_envelope import ...`.
CORE_ENVELOPE_MODULE = "core.pull_envelope"
CORE_ENVELOPE_BUILDERS = frozenset({"prevented_envelope"})


def _core_builders_imported_by(tree: ast.Module) -> frozenset:
    """Les noms de CE fichier qui designent VRAIMENT une fabrique de core.

    Suit l'alias (`import prevented_envelope as build`) parce que Python le suit,
    et refuse tout ce qui vient d'ailleurs : c'est le nom lie qui compte, pas le
    nom d'origine.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != CORE_ENVELOPE_MODULE:
            continue
        for alias in node.names:
            if alias.name in CORE_ENVELOPE_BUILDERS:
                bound.add(alias.asname or alias.name)
    return frozenset(bound)

#: Les noms sous lesquels `core/queue.py::_account_kwargs` sait encore passer le
#: compte selectionne quand le manifeste ne declare pas `pull_parameter`
#: (`_LEGACY_ACCOUNT_PARAM_NAMES`). Un connecteur qui attend le sien sous un
#: autre nom sans le declarer ne le recevra jamais -- silencieusement.
WORKER_ACCOUNT_NAMES = ("account_id", "site_url", "property_id", "account")

#: Les noms que CORE lui-meme lie a un sens : les cinq du contrat plus
#: `selection` (la selection de CHAMPS du catalogue). Un `pull_parameter` qui
#: reutilise l'un d'eux fait porter deux sens au meme argument.
CORE_ASSIGNED_PARAMS = frozenset(DOCUMENTED_REQUIRED | {"selection"})

#: La seule forme de cle que `classify_http_error` puisse trouver : il consulte
#: `error_map[f"{status_code}:{provider_code}"]`.
_ERROR_MAP_KEY = re.compile(r"^(\d{3}):(.+)$")

#: Un identifiant de COMPTE lu dans l'objet de selection de RAPPORT.
_ACCOUNT_KEY_IN_SELECTION = re.compile(
    r"selection(?:\.get\(\s*[\"']|\[\s*[\"'])"
    r"(\w*(?:account|advertiser|profile|customer|property|site|network|"
    r"organization|team|board|channel|club|location|table|app)\w*)",
    re.I,
)


# ---------------------------------------------------------------------------
# Lecture a l'AST -- aucun connecteur n'est importe. Un connecteur importe lit
# des variables d'environnement et ouvre des clients : le verdict dependrait de
# l'etat de la machine. Corollaire assume : une forme que l'AST ne peut pas
# decrire est un ECHEC NOMME, jamais un skip -- << je ne peux pas le prouver >>
# n'est pas << c'est bon >>. La reparation est de rendre le litteral explicite
# au point de sortie, pas d'affaiblir la lecture.
# ---------------------------------------------------------------------------


def _module_dirs() -> list[Path]:
    return sorted(d for d in _MODULES.iterdir() if (d / "connector.py").is_file())


def _ids() -> list[str]:
    return [d.name for d in _module_dirs()]


def _source(module_dir: Path) -> str:
    return (module_dir / "connector.py").read_text(encoding="utf-8")


def _tree(module_dir: Path) -> ast.Module:
    return ast.parse(_source(module_dir))


def _manifest(module_dir: Path) -> dict:
    path = module_dir / "manifest.json"
    if not path.is_file():
        pytest.fail(f"{module_dir.name}: connector.py sans manifest.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{module_dir.name}: manifest.json illisible ({exc})")
        raise  # pragma: no cover


def _all_functions(tree: ast.Module) -> dict:
    """Toutes les fonctions du fichier, imbriquees comprises, dernier lie gagne.

    Python lie le dernier `def` d'un nom ; on fait pareil. Les fonctions
    imbriquees entrent dans la table parce qu'une fabrique
    (`pull_x = _make_profile_pull(...)`) renvoie l'une d'elles.
    """
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _module_level_defs(tree: ast.Module) -> dict:
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _factory_bindings(tree: ast.Module) -> dict:
    """`pull_page_daily = _make_profile_pull(...)` -> {nom: nom_de_fabrique}."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        factory = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if not factory:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = factory
    return out


def _resolve_entrypoint(name: str | None, tree: ast.Module):
    """(noeud, comment) pour le callable *name*, ou (None, None) s'il n'existe pas.

    Deux liaisons legitimes, toutes deux utilisees dans le depot :
      - un `def` au niveau module ;
      - une affectation depuis une fabrique, dont on suit la fonction rendue.
        Neuf profils `gsc` et les trois profils `ias` n'existent QUE sous cette
        forme (`pull_page_daily = _make_profile_pull("page_daily", [...])`) : une
        lecture qui ne cherche que des `def` les declare absents a tort.
    """
    if not name:
        return None, None
    defs = _module_level_defs(tree)
    if name in defs:
        return defs[name], "def"

    factory_name = _factory_bindings(tree).get(name)
    if factory_name and factory_name in defs:
        factory = defs[factory_name]
        inner_names = [
            node.value.id
            for node in ast.walk(factory)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        ]
        for inner in inner_names:
            for node in ast.walk(factory):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == inner
                ):
                    return node, f"fabrique {factory_name}()"
    return None, None


def _signature(fn) -> tuple[list[str], list[str]]:
    """(requis, optionnels) -- positionnels sans defaut vs le reste."""
    positional = [a.arg for a in fn.args.posonlyargs] + [a.arg for a in fn.args.args]
    n_defaults = len(fn.args.defaults)
    required = positional[: len(positional) - n_defaults] if n_defaults else list(positional)
    optional = positional[len(positional) - n_defaults :] if n_defaults else []
    optional += [a.arg for a in fn.args.kwonlyargs]
    return required, optional


def _defaults(fn) -> dict:
    """{parametre: valeur} pour les defauts litteraux."""
    args = [a.arg for a in fn.args.posonlyargs] + [a.arg for a in fn.args.args]
    out: dict[str, object] = {}
    offset = len(args) - len(fn.args.defaults)
    for index, default in enumerate(fn.args.defaults):
        if isinstance(default, ast.Constant):
            out[args[offset + index]] = default.value
    for kwarg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if isinstance(default, ast.Constant):
            out[kwarg.arg] = default.value
    return out


def _terminal_returns(
    fn,
    funcs: dict,
    seen: frozenset = frozenset(),
    depth: int = 0,
    *,
    builders: frozenset = frozenset(),
):
    """Les sorties atteignables depuis *fn* : [(ligne, cles|None, note)].

    Une delegation vers une fonction du meme fichier est SUIVIE : la moitie des
    `pull()` du depot sont des aiguillages d'une ligne
    (`return _pull_profile(...)`), et s'arreter la ne prouverait rien.
    `cles is None` signale une sortie que l'AST ne peut pas decrire.
    """
    name = getattr(fn, "name", "?")
    if name in seen or depth > 8:
        return []
    seen = seen | {name}
    nested = {
        id(node)
        for sub in ast.walk(fn)
        if sub is not fn and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(sub)
    }
    out: list[tuple[int, list[str] | None, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or id(node) in nested:
            continue
        value = node.value
        if value is None:
            out.append((node.lineno, None, "`return` nu -- le worker lira None"))
        elif isinstance(value, ast.Dict):
            keys: list[str] = []
            for key in value.keys:
                if key is None:
                    keys.append("**<expansion>")
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(key.value)
                else:
                    keys.append("<cle calculee>")
            out.append((node.lineno, sorted(keys), ""))
        elif isinstance(value, ast.Call):
            func = value.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            # LE SITE D'APPEL, PAS SEULEMENT L'ORIGINE DU NOM. `_core_builders_
            # imported_by` prouve d'ou vient le NOM lie ; il ne dit rien de ce
            # qui est APPELE ici. Mesure 2026-08-21 : un fichier qui importait la
            # vraie fabrique ET appelait `helpers.prevented_envelope(...)`
            # obtenait les quatre cles, parce que `func.attr` rend le meme mot.
            # Un attribut est un objet dont ce fichier ne prouve rien : seul un
            # `ast.Name` designe le nom que l'import a lie.
            bare = getattr(func, "id", None) if isinstance(func, ast.Name) else None
            if bare and bare in funcs:
                out.extend(
                    _terminal_returns(
                        funcs[bare], funcs, seen, depth + 1, builders=builders
                    )
                )
            elif bare and bare in builders:
                # Une fabrique de core, IMPORTEE DEPUIS CORE et prouvee chez elle.
                # Voir CORE_ENVELOPE_BUILDERS : le nom seul ne suffit pas.
                out.append((node.lineno, sorted(DOCUMENTED_ENVELOPE), ""))
            else:
                out.append((node.lineno, None, f"appel opaque {callee}(...)"))
        elif isinstance(value, ast.Name):
            # `result = pull(...) ; ... ; return result` est le meme contrat que
            # `return pull(...)`. On resout la DERNIERE affectation locale du nom
            # (hors defs imbriquees) ; ce qui reste irresoluble est un echec nomme.
            bound = None
            for assign in ast.walk(fn):
                if (
                    isinstance(assign, ast.Assign)
                    and id(assign) not in nested
                    and any(
                        isinstance(t, ast.Name) and t.id == value.id for t in assign.targets
                    )
                ):
                    bound = assign.value
            if isinstance(bound, ast.Call):
                func = bound.func
                callee = (
                    func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                )
                if callee and callee in funcs:
                    out.extend(
                        _terminal_returns(
                            funcs[callee], funcs, seen, depth + 1, builders=builders
                        )
                    )
                    continue
            if isinstance(bound, ast.Dict):
                out.append(
                    (
                        node.lineno,
                        sorted(
                            k.value
                            for k in bound.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        ),
                        "",
                    )
                )
                continue
            out.append((node.lineno, None, f"variable {value.id} -- forme indecidable"))
        else:
            out.append((node.lineno, None, f"expression {type(value).__name__}"))
    return out


def _declared_entrypoints(manifest: dict) -> list[tuple[str, str, str]]:
    """[(profile_id, callable, availability)] -- ce que le worker resoudra.

    Source : `source_capabilities.reports[].dispatch.callable`, le nom que
    `core/main.py::get_module_pull_fn` va chercher pour le profil actif. `pull`
    reste le chemin par defaut (profile_id=None) et doit exister dans tous les cas.
    """
    reports = ((manifest.get("source_capabilities") or {}).get("reports")) or []
    out: list[tuple[str, str, str]] = []
    for report in reports:
        callee = (report.get("dispatch") or {}).get("callable")
        if callee:
            out.append(
                (
                    report.get("id", "?"),
                    callee,
                    (report.get("availability") or {}).get("status", "?"),
                )
            )
    if not any(callee == "pull" for _, callee, _ in out):
        out.append(("<defaut>", "pull", "legacy"))
    return sorted(set(out))


def _reachable_entrypoints(manifest: dict) -> list[tuple[str, str, str]]:
    """Ceux qu'un humain peut reellement declencher (+ le chemin par defaut)."""
    return [e for e in _declared_entrypoints(manifest) if e[2] in ("selectable", "legacy")]


# ---------------------------------------------------------------------------
# 1. Le point d'entree que le manifeste declare existe-t-il ?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_every_declared_entrypoint_exists(module_dir: Path) -> None:
    """Un `dispatch.callable` declare et absent = un profil mort en production.

    `core/main.py:264-274` echoue FERME : `getattr(module, callable_name, None)`
    rend None, `get_module_pull_fn` rend None, et le job meurt ; `core/loader.py`
    leve `dispatch_pull: declared callable=... is unavailable`. Rien de tout cela
    n'apparait avant qu'un humain ne lance ce profil-la -- c'est la meme classe
    que le TypeError de `gsc`, un cran plus haut : le contrat declare une chose,
    le code en offre une autre, et seul un appel reel les confronte.
    """
    tree = _tree(module_dir)
    missing = []
    for profile_id, callee, status in _reachable_entrypoints(_manifest(module_dir)):
        node, _how = _resolve_entrypoint(callee, tree)
        if node is None:
            missing.append(f"profil {profile_id!r} -> {callee}() [{status}]")
    assert not missing, (
        f"{module_dir.name}: le manifeste declare des points d'entree que "
        f"connector.py ne definit pas : {missing}. core/main.py:264-274 rend None "
        f"et le profil est INJOIGNABLE ; core/loader.py leve 'declared "
        f"callable=... is unavailable'. Definissez la fonction, ou retirez le "
        f"profil / sortez son availability de `selectable`."
    )


# ---------------------------------------------------------------------------
# 2. Exige-t-il seulement ce que le contrat autorise ?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_pull_requires_only_what_the_contract_allows(module_dir: Path) -> None:
    """Aucun parametre REQUIS en dehors des cinq documentes, sur AUCUN profil.

    Un parametre requis supplementaire rend le connecteur inappelable par un
    harnais generique : il faut connaitre ce connecteur-la pour l'appeler, ce qui
    est exactement ce que le contrat existe pour eviter. C'est l'erreur `gsc` du
    2026-07-31 -- et elle vaut pour chaque callable de profil, pas pour `pull`
    seul : c'est le callable du profil ACTIF que le worker resout et appelle.
    """
    tree = _tree(module_dir)
    offenders = []
    for profile_id, callee, _status in _reachable_entrypoints(_manifest(module_dir)):
        node, _how = _resolve_entrypoint(callee, tree)
        if node is None:
            continue  # signale par test_every_declared_entrypoint_exists
        required, _optional = _signature(node)
        extra = sorted(set(required) - DOCUMENTED_REQUIRED)
        if extra:
            offenders.append(f"{callee}() [profil {profile_id!r}] exige {extra}")
    assert not offenders, (
        f"{module_dir.name}: {offenders}, hors de la signature documentee "
        f"{sorted(DOCUMENTED_REQUIRED)}. Le worker appelle par mot-cle avec ces "
        f"cinq-la ; le compte se resout par account_topology + discover_accounts "
        f"et arrive en argument OPTIONNEL (core/queue.py::_account_kwargs). Un "
        f"requis leve un TypeError nu des que la selection est vide."
    )


# ---------------------------------------------------------------------------
# 3. Le RETOUR -- ce que ce fichier ne prouvait pas du tout.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_pull_returns_the_documented_envelope(module_dir: Path) -> None:
    """Chaque sortie atteignable rend l'enveloppe documentee.

    Le contrat (`server/modules/README.md`, `docs/adding-a-connector.mdx`) est
    `-> dict` portant `pull_id`, `row_count`, `date_from`, `date_to`. Le worker
    lit `result.get("row_count", 0) if isinstance(result, dict) else 0` : une
    enveloppe sans `row_count` NE LEVE RIEN, elle journalise ZERO -- dans le log
    du job, dans la ligne `audit_log` du pull, et dans le signal que la
    verification post-pull consomme. Un pull qui a ecrit dix mille lignes et en
    declare zero est indiscernable d'un pull vide : c'est exactement l'inverse
    de ce a quoi un journal sert.

    Une cle maison (`rows_written`, `rows_inserted`, `event_count`) n'est lue par
    personne. La reparation est un renommage au point de sortie, pas une lecture
    speciale cote core -- core ne connait aucun vocabulaire de module (AD-2).
    """
    tree = _tree(module_dir)
    funcs = _all_functions(tree)
    offenders = []
    for profile_id, callee, _status in _reachable_entrypoints(_manifest(module_dir)):
        node, _how = _resolve_entrypoint(callee, tree)
        if node is None:
            continue  # signale par test_every_declared_entrypoint_exists
        returns = _terminal_returns(node, funcs, builders=_core_builders_imported_by(tree))
        if not returns:
            offenders.append(f"{callee}() [profil {profile_id!r}]: aucune sortie -- rend None")
        for lineno, keys, note in returns:
            if keys is None:
                offenders.append(f"{callee}() L{lineno}: {note}")
                continue
            if "**<expansion>" in keys:
                continue  # un `**base` porte les cles de base ; indecidable, tolere
            missing = [k for k in DOCUMENTED_ENVELOPE if k not in keys]
            if missing:
                offenders.append(f"{callee}() L{lineno}: rend {keys} -- manque {missing}")
    assert not offenders, (
        f"{module_dir.name}: enveloppe de retour non conforme.\n  "
        + "\n  ".join(sorted(set(offenders)))
        + f"\nLe contrat est {list(DOCUMENTED_ENVELOPE)} (server/modules/README.md, "
        f"docs/adding-a-connector.mdx). `row_count` absente ne leve pas : le "
        f"worker lit 0 et le pull declare zero ligne."
    )


# ---------------------------------------------------------------------------
# 4. Le compte choisi par un humain arrive-t-il jusqu'au pull ?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_a_declared_selection_is_reachable_by_the_worker(module_dir: Path) -> None:
    """Si le module declare qu'il faut SELECTIONNER un compte, le compte arrive.

    C'est le cas `site_url` : GSC declare `selection_level: site` et implemente
    `discover_accounts`, donc le produit sait demander le site a un humain. Ce
    qui manquait etait le dernier metre : que le callable l'accepte comme le
    worker le donne.

    Trois choses se verifient ici, et aucune ne se contente d'une declaration :

    a. le compte ATTEINT chaque callable de profil -- `_account_kwargs` inspecte
       la signature du callable que le worker a resolu pour CE profil, pas celle
       de `pull`. Sinon `queue.py` journalise `selected_account_unreachable` et
       LAISSE TOMBER la selection : le pull tire sur le compte par defaut du
       jeton, c'est-a-dire les donnees d'un compte que personne n'a choisi ;
    b. le parametre declare ne porte QUE le compte -- ni un nom que core lie deja
       (les cinq du contrat, ou `selection`), ni un parametre dont le defaut est
       un id de `report_profiles` : le worker y ecrirait l'identifiant du compte
       a la place du selecteur de profil, et l'appel echoue des la premiere
       selection ;
    c. il est OPTIONNEL -- le worker ne passe le compte que si une selection
       existe, donc un requis leve un TypeError nu au lieu d'une erreur typee.

    Branche sans `selection_level` : le module affirme qu'un jeton = un compte.
    On verifie alors qu'aucun callable ne porte en douce un parametre de compte
    du repli historique -- un fil vivant sous une declaration qui dit qu'il n'y
    en a pas est precisement ce qu'on ne peut pas voir a la lecture.
    """
    manifest = _manifest(module_dir)
    topology = manifest.get("account_topology") or {}
    tree = _tree(module_dir)
    entrypoints = _reachable_entrypoints(manifest)

    if not topology.get("selection_level"):
        undeclared_wire = []
        for profile_id, callee, _status in entrypoints:
            node, _how = _resolve_entrypoint(callee, tree)
            if node is None:
                continue
            required, optional = _signature(node)
            carried = [n for n in WORKER_ACCOUNT_NAMES if n in required + optional]
            if carried:
                undeclared_wire.append(f"{callee}() [profil {profile_id!r}] porte {carried}")
        assert not undeclared_wire, (
            f"{module_dir.name}: aucune selection de compte n'est declaree "
            f"(account_topology.selection_level absent) et pourtant "
            f"{undeclared_wire}. `_account_kwargs` remplit ces noms par repli des "
            f"qu'une ligne existe dans app.connection_account_scope : le fil est "
            f"vivant sous une declaration qui dit qu'il n'y en a pas. Declarez la "
            f"topologie, ou retirez le parametre."
        )
        return

    declared = topology.get("pull_parameter")
    profile_ids = {p.get("id") for p in (manifest.get("report_profiles") or [])}

    if declared:
        assert declared not in CORE_ASSIGNED_PARAMS, (
            f"{module_dir.name}: account_topology.pull_parameter={declared!r} est "
            f"un nom que CORE lie deja a un autre sens "
            f"({sorted(CORE_ASSIGNED_PARAMS)}). Le meme argument porterait deux "
            f"significations et l'une ecraserait l'autre a l'appel."
        )

    unreachable: list[str] = []
    collides: list[str] = []
    still_required: list[str] = []
    for profile_id, callee, _status in entrypoints:
        node, _how = _resolve_entrypoint(callee, tree)
        if node is None:
            continue  # signale par test_every_declared_entrypoint_exists
        required, optional = _signature(node)
        params = required + optional

        if declared:
            if declared not in params:
                unreachable.append(f"{callee}() [profil {profile_id!r}] n'a pas {declared!r}")
                continue
            default = _defaults(node).get(declared)
            if default in profile_ids:
                collides.append(
                    f"{callee}(): {declared!r} vaut par defaut {default!r}, "
                    f"qui est un id de report_profile"
                )
            if declared in required:
                still_required.append(f"{callee}() exige {declared!r} en positionnel")
        else:
            fallback = [n for n in WORKER_ACCOUNT_NAMES if n in params]
            if not fallback:
                unreachable.append(
                    f"{callee}() [profil {profile_id!r}] n'a aucun des noms de repli "
                    f"{list(WORKER_ACCOUNT_NAMES)}"
                )
            still_required += [
                f"{callee}() exige {n!r} en positionnel" for n in fallback if n in required
            ]

    assert not unreachable, (
        f"{module_dir.name}: selection_level={topology.get('selection_level')!r}, "
        f"donc un humain choisira un compte -- et il n'arrivera pas : {unreachable}. "
        f"core/queue.py::_account_kwargs journalise `selected_account_unreachable` "
        f"puis LAISSE TOMBER la selection ; le pull tire alors sur le compte par "
        f"defaut du jeton. Declarez `account_topology.pull_parameter` ET ajoutez ce "
        f"parametre optionnel a CHAQUE callable de profil."
    )
    assert not collides, (
        f"{module_dir.name}: {collides}. Le worker y ecrirait l'identifiant du "
        f"compte choisi, la ou le connecteur attend un identifiant de profil de "
        f"rapport : l'appel echoue des la premiere selection. Un parametre, un sens."
    )
    assert not still_required, (
        f"{module_dir.name}: {still_required}. Le worker ne passe le compte QUE si "
        f"une selection existe (core/queue.py::_account_kwargs rend {{}} sans "
        f"compte), donc un datastream sans selection fait lever un TypeError nu au "
        f"lieu d'une erreur typee. Donnez-lui un defaut et refusez explicitement "
        f"l'absence."
    )


# ---------------------------------------------------------------------------
# 5. Le compte n'est pas le rapport.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_the_account_is_not_read_out_of_the_report_selection(module_dir: Path) -> None:
    """`selection` est la selection de RAPPORT, jamais le compte.

    Deux objets portent ce nom et ce ne sont pas les memes. Celui que le plan
    fournit est decrit dans `core/schemas/datastream-intent.schema.json`
    (`$defs.selection`) : `selection_mode`, `metrics`, `dimensions`, `grain`,
    `filters` -- et rien d'autre, `additionalProperties: false`. Neuf
    connecteurs lisaient pourtant `selection["advertiser_id"]`,
    `selection["profile_id"]`, `selection["account_id"]`... c'est-a-dire un
    objet de COMPTE que le plan ne produit pas et ne peut pas produire.

    Consequence mesuree le 2026-07-31 : `queue.py` ne remplit jamais
    `job["selection"]` (son propre commentaire le dit : << None today; wired by
    the datastream path later >>), donc ces connecteurs levaient ou tiraient a
    l'aveugle. Le compte passe par le parametre que le manifeste declare
    (`account_topology.pull_parameter`), comme pour les vingt autres.

    La verification vaut pour les 38, y compris ceux qui declarent deja un
    `pull_parameter` : declarer le bon canal n'empeche pas d'avoir garde
    l'ancien. La version du matin sautait ici sur `le compte a son parametre
    declare` -- un constat positif qui ne verifiait rien.
    """
    found = _ACCOUNT_KEY_IN_SELECTION.findall(_source(module_dir))
    assert not found, (
        f"{module_dir.name}: le connecteur lit {sorted(set(found))} dans "
        f"`selection`, mais la selection que le plan fournit ne porte que "
        f"selection_mode / metrics / dimensions / grain / filters "
        f"(datastream-intent.schema.json, additionalProperties: false). Le compte "
        f"doit arriver par le parametre declare dans "
        f"account_topology.pull_parameter."
    )


# ---------------------------------------------------------------------------
# 6. La topologie : declaree -> valide et joignable ; absente -> justifiee.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_a_declared_topology_actually_validates(module_dir: Path) -> None:
    """Une topologie malformee est une topologie MORTE, pas une topologie bancale.

    `core/account_topology.py::get_topology` rend `None` des que
    `validate_topology` trouve la moindre erreur, et `_resolve_discovery_callable`
    s'arrete la. Le manifeste peut donc declarer un `selection_level`, le
    connecteur peut implementer `discover_accounts` -- et la decouverte ne tourne
    jamais. Aucun humain ne se voit proposer de compte, et rien ne le dit.

    Trouve le 2026-07-31 sur taboola et linkedin-company-pages : `levels` etait
    un tableau de CHAINES la ou le contrat veut des objets `{id, label}`. Les
    deux modules avaient une decouverte ecrite, testee, et injoignable.

    Deux verifications de plus, qui remplacent deux skips :
      - la fonction de decouverte DECLAREE existe dans `connector.py`. Une
        topologie valide qui pointe un nom absent ne propose rien non plus ;
      - l'absence de topologie est une DECISION qui s'ecrit. Sans
        `_account_topology_note`, on ne distingue pas << cette API n'a qu'un
        compte par jeton >> de << personne n'a regarde >> (playbook
        `server/modules/README.md`, etape 5).
    """
    manifest = _manifest(module_dir)
    topology = manifest.get("account_topology")

    if not topology:
        assert manifest.get("_account_topology_note"), (
            f"{module_dir.name}: ni account_topology ni _account_topology_note. "
            f"Le playbook (server/modules/README.md) exige l'un des deux : une API "
            f"multi-comptes dont la topologie n'est pas declaree livre des donnees "
            f"d'un compte que personne n'a choisi."
        )
        return

    errors = validate_topology(topology)
    assert not errors, (
        f"{module_dir.name}: account_topology ne valide pas -> {errors}. "
        f"get_topology() rend None, donc _resolve_discovery_callable ne trouve "
        f"pas discover_accounts : la decouverte de comptes est MORTE et l'ecran "
        f"ne proposera jamais rien a choisir."
    )

    discovery = (topology.get("discovery") or {}).get("callable")
    node, _how = _resolve_entrypoint(discovery, _tree(module_dir))
    assert node is not None, (
        f"{module_dir.name}: account_topology.discovery.callable={discovery!r} "
        f"n'existe pas dans connector.py. Le produit ne peut proposer aucun compte "
        f"a l'humain : la selection ne peut pas commencer."
    )


# ---------------------------------------------------------------------------
# 7. La taxonomie d'erreurs : declaree -> vivante ; absente -> justifiee.
# ---------------------------------------------------------------------------


def _pure_http_class(status: int) -> str:
    """Ce que core classe SANS error_map -- l'etalon de ce qu'une map ajoute."""
    return classify_http_error(status, None, None).error_class


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_error_taxonomy_is_declared_or_its_absence_is(module_dir: Path) -> None:
    """Une error_map declaree doit pouvoir CHANGER une classification.

    << error_map declaree >> etait, ce matin, le message de skip de vingt-neuf
    modules. Il ne prouvait rien. `core.pull_errors.classify_http_error` classe
    d'abord sur le seul HTTP (401 -> auth_expired, 403 -> permission_denied,
    400 -> invalid_request, 5xx -> provider_transient), puis consulte
    `error_map[f"{status_code}:{provider_code}"]` et n'applique la valeur que si
    elle appartient a la taxonomie. Trois consequences mecaniques :

    a. FORME. Une cle sans la forme `<status>:<code>` n'est JAMAIS trouvee -- une
       cle nue `"401"` ne classe rien, et une note de documentation rangee DANS
       la map est une cle morte de plus (sa place est a cote, en
       `_error_map_note` au niveau du manifeste). Une cle en 429 non plus : 429
       leve `ValueError` avant d'arriver la, il passe par
       `core.quota.RateLimitError` (chemin du disjoncteur). Une valeur hors
       taxonomie est ignoree en silence.
    b. COMPLETUDE. Une map dont toutes les entrees redisent la classification
       pure-HTTP ne peut changer AUCUN verdict : la declarer ne prouve rien de
       plus que son absence. La seule classe que le pur HTTP ne produit jamais
       est `auth_revoked` -- << ton acces a ete revoque, redemande-le >> contre
       << reconnecte-toi >>, deux actions differentes pour l'utilisateur.
    c. LECTURE. Une map que le connecteur ne mentionne nulle part est morte :
       ni passee en 3e argument de `classify_http_error` (la voie documentee,
       README etape 4), ni relue localement avant de deleguer -- les deux
       consommations existent dans le depot et les deux comptent.

    Sans error_map, la note reste obligatoire : un 403 et un 401 se ressemblent,
    et le produit ne sait pas distinguer << reconnecte-toi >> de
    << demande l'acces >>.
    """
    manifest = _manifest(module_dir)
    error_map = manifest.get("error_map") or {}

    if not error_map:
        assert manifest.get("_error_map_note"), (
            f"{module_dir.name}: ni error_map ni _error_map_note. Le playbook "
            f"(server/modules/README.md, etape 4) exige l'un des deux."
        )
        return

    malformed = []
    for key, value in error_map.items():
        match = _ERROR_MAP_KEY.match(str(key))
        if not match:
            malformed.append(
                f"{key!r}: pas de la forme '<status>:<provider_code>' -- "
                f"classify_http_error ne la cherchera jamais (seule une lecture "
                f"locale au module peut l'atteindre, et alors la taxonomie a deux "
                f"implementations)"
            )
        elif match.group(1) == "429":
            malformed.append(
                f"{key!r}: 429 n'atteint jamais classify_http_error (il leve "
                f"ValueError) -- le 429 passe par core.quota.RateLimitError"
            )
        elif value not in ERROR_CLASSES:
            malformed.append(
                f"{key!r} -> {value!r}: hors taxonomie {list(ERROR_CLASSES)} -- "
                f"classify_http_error ignore la valeur en silence"
            )
    assert not malformed, (
        f"{module_dir.name}: error_map non consultable ({len(malformed)}/"
        f"{len(error_map)} entrees).\n  "
        + "\n  ".join(malformed)
        + "\nUne note explicative se declare A COTE de la map (`_error_map_note` "
        "au niveau du manifeste), jamais dedans."
    )

    refining = [
        f"{key}->{value}"
        for key, value in error_map.items()
        if value != _pure_http_class(int(_ERROR_MAP_KEY.match(str(key)).group(1)))
    ]
    assert refining, (
        f"{module_dir.name}: les {len(error_map)} entrees de error_map redisent "
        f"toutes la classification que core produit deja sans elles "
        f"(core.pull_errors.classify_http_error). Aucune ne peut changer un "
        f"verdict -- notamment aucune ne produit `auth_revoked`, la seule classe "
        f"que le pur HTTP n'atteint jamais. Ajoutez les codes qui raffinent "
        f"vraiment, ou declarez `_error_map_note` et retirez la map."
    )

    assert "error_map" in _source(module_dir), (
        f"{module_dir.name}: error_map declaree ({len(error_map)} entrees) et "
        f"connector.py ne la mentionne jamais -- ni passee en 3e argument de "
        f"classify_http_error (README etape 4), ni relue localement. Elle ne "
        f"classe rien : chaque erreur retombe sur le pur HTTP."
    )


# ---------------------------------------------------------------------------
# 8. La borne de re-collecte : declaree -> lue ; absente -> dite.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_dir", _module_dirs(), ids=_ids())
def test_backfill_bound_is_declared_or_its_absence_is(module_dir: Path) -> None:
    """How far back a report may be re-collected is answered, or its silence is.

    `max_provider_backfill_days` was READ by three core modules and DECLARABLE
    nowhere. `datastream_source_catalogue._declared_backfill_bound` looks it up
    per report, `datastream_dimension_history` turns it into
    `earliest_recoverable`, the Workbench shows that -- and the manifest schema
    had no such property, so no module had ever been told the key exists.
    Measured 2026-08-12 and again 2026-08-17: 0 of the 140 declared reports
    carried it, and every screen answered "unknown" to "how far back can I
    re-collect this?".

    The key is in the schema since 2026-08-17. This test is the other half: a
    bound that nobody may declare is a dead key, and a bound that everybody must
    invent is worse. So each module answers ONCE, in one of two ways:

      * its reports declare `max_provider_backfill_days` (a positive integer, the
        provider's own published reporting window); or
      * the manifest carries `_max_provider_backfill_days_note` saying why this
        provider publishes no hard bound -- a customer-configurable retention
        setting, an unlimited history, or a documented "we do not commit".

    A NOTE IS NOT A LESSER ANSWER. For most providers it is the TRUE one, and it
    is what keeps a guess out of the manifest: this repository has already paid
    for one fabricated default (meta-ads landed `'USD'` for every account whose
    currency the API did not return). A re-collection bound invented here would
    promise the operator a history the provider never committed to.

    Partial declaration is allowed and is not a gap: a connector may know the
    window of one report and not of another. Declaring ONE report is enough to
    prove the module considered the question.
    """
    manifest = _manifest(module_dir)
    # `source_capabilities.reports`, NOT `report_profiles`: this is the list
    # `_declared_backfill_bound` looks the key up in (via `_raw_report`), and a
    # gate that read the other list would be satisfied by a declaration the
    # product never sees. Measured while writing this test -- 22 manifests were
    # first filled in `report_profiles` and the reader still answered 0 of 140.
    reports = (manifest.get("source_capabilities") or {}).get("reports") or []
    if not reports:
        pytest.skip(f"{module_dir.name} declares no source_capabilities.reports")

    # Read through the PRODUCT's own reader, not by re-implementing the lookup
    # here. A second reading of the same key is free to disagree with the first,
    # and this test's entire job is to prove the answer the product publishes.
    from core.datastream_source_catalogue import (  # noqa: PLC0415
        _declared_backfill_bound,
    )

    declared = {
        report.get("id"): _declared_backfill_bound(manifest, str(report.get("id")))
        for report in reports
        if _declared_backfill_bound(manifest, str(report.get("id"))) is not None
    }

    if not declared:
        assert manifest.get("_max_provider_backfill_days_note"), (
            f"{module_dir.name}: not one of its {len(reports)} report(s) declares "
            f"`max_provider_backfill_days`, and the manifest does not say why. "
            f"The product asks 'how far back may this be re-collected?' "
            f"(core.datastream_source_catalogue._declared_backfill_bound -> "
            f"datastream_dimension_history.earliest_recoverable) and gets no "
            f"answer at all. Declare the provider's PUBLISHED window on the "
            f"reports that have one, or declare `_max_provider_backfill_days_note` "
            f"at manifest level saying the provider publishes none. Do not invent "
            f"a number."
        )
        return

    bad = [
        f"{report_id!r} -> {value!r}"
        for report_id, value in declared.items()
        if value <= 0
    ]
    assert not bad, (
        f"{module_dir.name}: `max_provider_backfill_days` must be a positive "
        f"number of days -- `0` would claim no day may ever be re-collected, "
        f"which no provider says, and the reader "
        f"(datastream_source_catalogue._declared_backfill_bound) drops it back to "
        f"None anyway: " + "; ".join(bad)
    )


# ---------------------------------------------------------------------------
# La liste des fabriques de core est-elle une confiance ou une preuve ?
# ---------------------------------------------------------------------------


def test_a_core_envelope_builder_is_trusted_by_ORIGIN_and_not_by_name():
    """AI-307 : `callee in CORE_ENVELOPE_BUILDERS` accordait sa confiance a un MOT.

    Un connecteur qui liait `prevented_envelope` a n'importe quoi -- un helper
    maison, un import d'un autre paquet -- traversait `_terminal_returns` avec
    l'enveloppe documentee accordee d'office, sans qu'une seule cle soit lue.
    C'est exactement le defaut que ce fichier existe pour attraper, arrive par la
    porte que ce fichier avait ouverte lui-meme.
    """
    genuine = ast.parse("from core.pull_envelope import prevented_envelope\n")
    assert _core_builders_imported_by(genuine) == {"prevented_envelope"}

    # L'alias suit, parce que Python le suit.
    aliased = ast.parse("from core.pull_envelope import prevented_envelope as build\n")
    assert _core_builders_imported_by(aliased) == {"build"}

    # Le meme nom, une autre origine : aucune confiance.
    for impostor in (
        "from .helpers import prevented_envelope\n",
        "from connector_helpers import prevented_envelope\n",
        "from core.pull_errors import prevented_envelope\n",
        "import core.pull_envelope\n",
    ):
        assert _core_builders_imported_by(ast.parse(impostor)) == frozenset(), impostor


def test_the_core_builder_branch_is_actually_exercised_by_a_connector():
    """Une branche que personne ne prend prouverait la conformite de rien.

    Le connecteur qui a eu besoin de l'enveloppe empechee le premier passe par
    cette branche : si un jour plus aucun ne le fait, la liste ci-dessus est du
    code mort et cette ligne le dit.
    """
    users = [
        module_dir.name
        for module_dir in _module_dirs()
        if _core_builders_imported_by(_tree(module_dir))
    ]
    assert users, (
        "aucun connecteur n'importe de fabrique d'enveloppe depuis "
        f"{CORE_ENVELOPE_MODULE} : CORE_ENVELOPE_BUILDERS n'est plus qu'une "
        "exception accordee a personne, et elle doit etre retiree."
    )


def test_an_impostor_builder_does_not_traverse_the_envelope_check():
    """La MEME preuve, sur le chemin qui decide -- pas sur l'aide qui le nourrit.

    Verifier `_core_builders_imported_by` seul laisserait le cablage libre : c'est
    `_terminal_returns` qui accorde ou refuse les quatre cles, et c'est lui qu'on
    interroge ici, sur deux fichiers qui ne different que par une ligne d'import.
    """
    source = (
        "{importline}\n"
        "def pull(connection_id, date_from, date_to, project_id, pull_id):\n"
        "    return prevented_envelope(\n"
        "        pull_id=pull_id, date_from=date_from, date_to=date_to,\n"
        "        reason='gate', message='Ask for the grant, then re-ask.',\n"
        "    )\n"
    )

    def _keys(importline: str):
        tree = ast.parse(source.format(importline=importline))
        funcs = _all_functions(tree)
        return _terminal_returns(
            funcs["pull"], funcs, builders=_core_builders_imported_by(tree)
        )

    # Depuis core : l'enveloppe documentee est accordee, prouvee chez elle.
    genuine = _keys("from core.pull_envelope import prevented_envelope")
    assert [keys for _line, keys, _note in genuine] == [sorted(DOCUMENTED_ENVELOPE)]

    # Le meme nom, une autre origine : appel opaque, donc ecart signale. Sans
    # cette ligne, un homonyme obtenait les quatre cles sans qu'aucune soit lue.
    impostor = _keys("from connector_helpers import prevented_envelope")
    assert [keys for _line, keys, _note in impostor] == [None], impostor


def test_the_trust_is_read_at_the_CALL_SITE_and_not_only_at_the_import():
    """L'ORIGINE prouvee ne dit rien de ce qui est APPELE deux lignes plus bas.

    Mesure 2026-08-21 : `_terminal_returns` lisait `func.attr` pour un appel
    d'attribut, c'est-a-dire le MEME mot que pour un appel nu. Un fichier qui
    importait la vraie fabrique -- donc `builders == {"prevented_envelope"}` --
    et rendait `helpers.prevented_envelope(...)` obtenait les quatre cles
    documentees sans qu'une seule soit lue, par la porte que le garde d'origine
    venait d'ouvrir. Seul un `ast.Name` designe le nom que l'import a lie.
    """
    header = "from core.pull_envelope import prevented_envelope\n"
    body = (
        "def pull(connection_id, date_from, date_to, project_id, pull_id):\n"
        "    return {call}(\n"
        "        pull_id=pull_id, date_from=date_from, date_to=date_to,\n"
        "        reason='gate', message='Ask for the grant, then re-ask.',\n"
        "    )\n"
    )

    def _keys(call: str):
        tree = ast.parse(header + body.format(call=call))
        funcs = _all_functions(tree)
        return [
            keys
            for _line, keys, _note in _terminal_returns(
                funcs["pull"], funcs, builders=_core_builders_imported_by(tree)
            )
        ]

    # Le nom lie par l'import : accorde, prouve chez lui.
    assert _keys("prevented_envelope") == [sorted(DOCUMENTED_ENVELOPE)]
    # Le meme mot porte par un objet quelconque : appel opaque, ecart signale --
    # et l'import legitime au-dessus ne le couvre pas.
    for attribute in (
        "helpers.prevented_envelope",
        "self.prevented_envelope",
        "_compat.prevented_envelope",
    ):
        assert _keys(attribute) == [None], attribute


def test_a_local_function_is_followed_by_its_NAME_and_not_by_an_attribute():
    """La meme confusion, sur l'autre branche : `funcs[callee]`.

    `helpers.pull_window(...)` et `pull_window(...)` rendaient le meme `callee`,
    donc une methode d'un objet etranger etait SUIVIE jusqu'a la fonction locale
    qui porte ce nom -- et l'enveloppe prouvee etait celle de la locale, pas
    celle qui tourne.
    """
    source = (
        "def _window(pull_id):\n"
        "    return {'pull_id': pull_id, 'row_count': 0, 'date_from': 'a', 'date_to': 'b'}\n"
        "def pull(connection_id, date_from, date_to, project_id, pull_id):\n"
        "    return CALL(pull_id)\n"
    )

    def _keys(call: str):
        tree = ast.parse(source.replace("CALL", call))
        funcs = _all_functions(tree)
        return [keys for _line, keys, _note in _terminal_returns(funcs["pull"], funcs)]

    assert _keys("_window") == [sorted(DOCUMENTED_ENVELOPE)]
    assert _keys("adapter._window") == [None]
