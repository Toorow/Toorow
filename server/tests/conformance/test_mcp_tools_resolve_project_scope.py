"""Un outil MCP qui prend un `project_id` resout l'acces -- Story 53.1, AC7.

POURQUOI UNE GARDE CALCULEE ET PAS UNE LISTE. Le plan d'epic 53 nommait QUATRE
outils sans controle le 2026-07-31. Remesure le 2026-08-05 : **neuf**. Entre les
deux, la famille daily-insight, `flows_get` et `flows_upsert` ont atterri sans
garde, et `get_procedure` en a gagne une. Personne n'avait tort ; simplement,
rien ne comptait. Une liste ecrite a la main aurait vieilli exactement comme
celle du plan.

POURQUOI ELLE A DU ETRE REECRITE (relecture du 2026-08-10). La premiere version
ne lisait qu'UN fichier : `_MAIN = _CORE / "main.py"`. Or vingt et un modules de
`core/` enregistrent des outils MCP. Un outil scope projet ajoute a
`inbound_mcp.py` passait la garde sans rougir -- mutation appliquee en relecture,
**0 ligne rouge**. Ce n'etait pas une porte ouverte ce jour-la, c'etait un
cliquet qui ne cliquetait que sur un vingt-et-unieme du perimetre. La garde
elargie a immediatement trouve CINQ outils scopes projet reellement non gardes
hors de `main.py` -- dont trois ecritures : `dimension_labels_mcp`,
`schedule_mcp`, `stop_run_mcp`. Le balayage n'etait donc pas une precaution.

CE QUE CE FICHIER FAIT. Il derive, a chaque execution, sur TOUT `core/*.py` :

    outil enregistre -- `mcp.tool(f)`, `@mcp.tool()`, `register_profiled(mcp, f)`
      -> prend-il un `project_id` dans sa signature ?
      -> son corps, OU une fonction de `core/` qu'il appelle, resout-il l'acces ?

Un outil qui prend un projet et ne resout rien echoue ICI, avec son nom et son
module, plutot que d'etre livre en silence.

TROIS CHOIX DE DERIVATION, ET CHACUN VIENT D'UN FAUX POSITIF MESURE.

* **Les DELEGUES comptent.** La moitie des gardes existantes vivent ailleurs :
  `get_source_capabilities` ne contient aucun controle et est pourtant protege --
  `get_scoped_source_capabilities` appelle `identity_can_read_project(...)`. Une
  garde qui ne lirait que le corps de l'outil le declarerait fautif et exigerait
  un doublon. `flows_list` demande deux sauts (`_flows.list_flows` ->
  `_assert_access`), d'ou la fermeture transitive bornee.
* **Les ALIAS comptent, l'import seul ne compte plus** (reparation du
  2026-08-17). Ce fichier creditait un outil des qu'un nom de garde APPARAISSAIT
  dans son texte -- import compris. `get_procedure` etait le cas qui avait motive
  cette lecture textuelle, et c'est lui qui l'a invalidee : il importait
  `resolve_strict_resource_access as _access`, la garde le comptait protege, et
  le corps de la Skill partait quand meme puisque le refus ne retirait qu'un
  sous-champ. Un import n'est pas un appel. La lecture est donc redevenue un
  arbre : les appels du corps, avec les alias d'import resolus vers ce qu'ils
  importent -- `_access(...)` compte, `import ... as _access` seul ne compte pas.
* **Les gardes ORG comptent comme des gardes.** `metric_mapping_confirm` est
  protege par `_guard_org_manage`, qui appelle `identity_can_manage_org`. Le
  reconnaitre est la bonne reponse ; l'exempter par son nom aurait ete la
  mauvaise. (L'exemple d'origine etait `metric_definition_upsert`, retire le
  2026-08-25 avec les ecrivains semantiques herites -- story 49.3 AC1 ; ses trois
  freres de curation portent la meme garde et le point tient mot pour mot.)

L'EXCEPTION EST UNIQUE ET ELLE EST ECRITE. `health` prend un `project_id` de
parametre-echafaudage qu'il renvoie sans lire aucune ligne, et c'est une sonde de
disponibilite : lui faire demander a la base si l'appelant a droit au projet la
ferait repondre << indisponible >> pour la panne meme qu'elle existe pour
signaler. La raison vit aussi a sa ligne dans `main.py`.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "core"

#: Ce qui compte comme << resoudre l'acces >>. `refuse_unless_project_scope` est
#: le seam de 53.1 (et son alias `_refuse_unless_project_scope` dans `main.py`) ;
#: les autres sont les mecanismes qui existaient avant et qui restent valables --
#: la garde verifie qu'une decision est prise, pas laquelle.
#:
#: Ce n'est pas une liste d'exemptions, c'est un VOCABULAIRE, et
#: `test_the_guard_vocabulary_knows_every_access_decision_helper` le fait vieillir
#: bruyamment : une fonction de decision ajoutee a `core/project_access.py` et
#: absente d'ici ferait passer pour non garde un outil qui l'est.
_GUARDS = frozenset({
    "refuse_unless_project_scope",
    "_refuse_unless_project_scope",
    "_assert_project_access",
    "_require_org_read",
    # Les points d'entree de `core/project_access.py` (voir le test de vocabulaire).
    "identity_can_read_project",
    "resolve_strict_resource_access",
    "identity_can_manage_org",
    "identity_has_org_access",
    "identity_has_project_role",
    "identity_can_access_project_in_org",
    "resolve_org_role",
    "resolve_provider_account_access",
    "effective_project_role",
    # AI-301 : la meme decision pour un appelant qui n'est pas une personne --
    # l'horloge tire son autorite de l'ACTIVATION. Elle rend un `AccessDecision`
    # comme les autres, donc elle appartient au vocabulaire.
    "resolve_scheduled_account_access",
})

#: LES TROIS FORMES DE PORTEE, et non plus la seule (audit 12, P1-3, 2026-08-17).
#:
#: Le filtre de ce fichier etait `if "project_id" not in params: continue` : un
#: outil MCP prenant `org_id` ou `datastream_id` SEUL sortait du compte. Seize
#: outils etaient dans ce cas -- toute la famille `inbound_mcp`, les cinq lectures
#: `operations_mcp` scopees datastream, `mapping_proposal_mcp`,
#: `datastream_diagnosis`, `recovery_mcp`. Ils etaient gardes a la main, et rien
#: ne le comptait : exactement la configuration qui a fait passer le compte des
#: outils projet non gardes de 4 a 9 en cinq jours, et le travers << un garde
#: partiel cache le reste >>.
_SCOPE_PARAMS = ("project_id", "org_id", "datastream_id")

#: LA PORTEE PLATEFORME EST UNE PORTEE. Cinq outils d'horloge et trois lectures
#: connecteur ne nomment aucune ressource de tenant : leur decision d'acces est
#: l'allow-list `TOOROW_SUPER_ADMINS`, resolue par `identity_is_super_admin`
#: (l'unique resolution serveur, meme audit, P1-2). Ne pas la reconnaitre les
#: aurait fait exempter par leur nom -- la mauvaise reponse, celle que
#: `_guard_org_manage` avait deja appris a ce fichier.
_PLATFORM_GUARDS = frozenset({"is_super_admin", "identity_is_super_admin"})

#: Ce que la garde accepte comme une decision d'acces, toutes portees confondues.
_ALL_GUARDS = _GUARDS | _PLATFORM_GUARDS

#: SENS UNIQUE. Retirer un nom est une reparation qui tient ; en ajouter un exige
#: d'ecrire pourquoi cet outil prend un projet sans jamais demander s'il y a droit.
_KNOWN_UNGUARDED = frozenset({"main.py:health"})

#: LES OUTILS QUI NE NOMMENT AUCUNE RESSOURCE, ET POURQUOI C'EST LEGITIME.
#:
#: Meme modele que `_PROVEN_BY_READING_ONLY` : un ledger nom -> RAISON, a sens
#: unique, que deux tests font vieillir bruyamment. Un outil enregistre qui ne
#: prend aucune portee et ne resout aucun acces doit etre ICI, avec la phrase qui
#: dit ce qu'il touche -- sinon il n'est compte nulle part, ce qui est la panne
#: que ce fichier existe pour empecher. Ajouter un nom ici coute une phrase
#: verifiable ; c'est le prix, et il est volontairement plus cher que la garde.
_NO_TENANT_SCOPE = {
    "adaptation_executor_mcp.py:file_source_test_adaptation": (
        "N'ouvre aucune ressource : le code et les octets viennent de l'appelant, "
        "partent au worker isole (AD-3), et rien n'est lu ni ecrit en base -- il n'y "
        "a pas de ressource de tenant dont l'acces pourrait se resoudre."
    ),
    "flows_mcp.py:flows_validate": (
        "Validation de schema d'un document fourni par l'appelant, sans aucune "
        "lecture (<< No DB touch >>, Story 8.7 AC4). Elle repond a << ce document "
        "est-il conforme >>, jamais a << que contient ce projet >>."
    ),
    "operations_mcp.py:list_inbound_templates": (
        "Catalogue de PLATEFORME immuable (template_code, version, champs requis). "
        "Aucun secret, aucune donnee inter-tenant : la meme reponse pour tout le "
        "monde, donc rien a scoper. La decouverte reste bornee par le profil "
        "Operations."
    ),
}

#: `project_exists` repond a l'EXISTENCE, pas a l'acces -- un outil qui ne
#: l'appellerait qu'elle resterait ouvert au voisin. Elle est donc nommee ici
#: pour que le test de vocabulaire ne la reclame pas, et pas dans `_GUARDS`.
_NOT_AN_ACCESS_DECISION = frozenset({"project_exists"})

#: Ou vit la PREUVE PAR LE COMPORTEMENT : les harnais qui appellent l'outil avec
#: une identite sans droit et exigent le refus. Une garde derivee du source dit
#: << une decision est prise >> ; seul un appel refuse dit << elle gouverne la
#: reponse >>. `get_procedure` est la raison de cette distinction : il appelait
#: bien une garde, dans le bloc de ses remarques, et servait le corps de la Skill
#: au refuse (audit du 2026-08-17).
_PROOFS = sorted(
    (Path(__file__).resolve().parents[1] / "isolation").glob("*scope_refusal*.py")
)

#: Les outils que la garde credite sur le SOURCE et qu'aucun appel refuse ne
#: prouve. SENS UNIQUE, comme `_KNOWN_UNGUARDED` : un nom en sort quand son
#: harnais est ecrit, et en ajouter un exige d'expliquer pourquoi sa garde n'est
#: verifiable que par lecture. Ce n'est pas une exemption, c'est la dette rendue
#: comptable -- elle valait 38 outils le 2026-08-17.
_PROVEN_BY_READING_ONLY = frozenset({
    "agent_surface_mcp.py:resolve_business_path",
    "analyze_render_mcp.py:analyze_result",
    "analyze_render_mcp.py:app_read_result_manifest",
    "analyze_render_mcp.py:app_read_result_slice",
    "analyze_render_mcp.py:compose_analyze_pivot",
    "analyze_render_mcp.py:discover_analyze_matches",
    "analyze_render_mcp.py:execute_analyze_query_spec",
    "analyze_render_mcp.py:explore_analyze_query",
    "analyze_render_mcp.py:list_analyze_facets",
    "analyze_render_mcp.py:render_analyze_result",
    "cards_mcp.py:list_card_templates",
    "connectors_mcp.py:get_source_capabilities",
    # AUDIT 12, P1-3 (2026-08-17) : les onze outils que l'elargissement de la
    # portee aux `org_id`/`datastream_id` a fait entrer dans le compte. Leur garde
    # est reelle et lue ici ; aucun harnais ne l'a encore prouvee par un appel
    # refuse. Ils entrent donc comme DETTE, pas comme exemption -- c'est la seule
    # facon honnete de les compter le jour ou on commence a les compter.
    "datastream_diagnosis.py:datastream_diagnose",
    "datastream_diagnosis.py:datastream_pull_history",
    "daily_insight_mcp.py:get_card_capabilities",
    "daily_insight_mcp.py:get_daily_insight_readiness",
    "daily_insight_mcp.py:preview_daily_insight",
    "daily_insight_mcp.py:publish_daily_insights",
    "data_quality_mcp.py:get_data_quality_report",
    "datastream_first_candidate_mcp.py:get_datastream_arming",
    "datastream_first_candidate_mcp.py:publish_activate_datastream_candidate",
    "datastream_first_candidate_mcp.py:start_datastream_first_candidate",
    "evidence_inspection_mcp.py:app_record_evidence_inspection",
    "first_report_render_mcp.py:render_starter_report",
    "first_report_render_mcp.py:reproduce_starter_report",
    "flows_mcp.py:flows_list",
    "mapping_proposal_mcp.py:inspect_mapping",
    "mapping_proposal_mcp.py:propose_mapping_correction",
    "mapping_proposal_mcp.py:test_mapping_candidate",
    "master_data_mcp.py:edit_country_master_data",
    "master_data_mcp.py:prepare_country_master_data_publish",
    "master_data_mcp.py:publish_country_master_data",
    "metric_semantics_mcp.py:metric_reference",
    "metric_semantics_mcp.py:metric_route",
    "metric_semantics_mcp.py:metric_verified_queries",
    "object_kind_mcp.py:declare_client_object_kind",
    "object_kind_mcp.py:describe_client_object_kind",
    "object_kind_mcp.py:release_client_object_source",
    "operations_mcp.py:get_connector_activation_status",
    "operations_mcp.py:get_datastream_readiness",
    "operations_mcp.py:get_inbound_credential_status",
    "operations_mcp.py:list_datastream_runs",
    "operations_mcp.py:prepare_datastream_recovery",
    "project_capabilities_mcp.py:confirm_project_capability_change",
    # Story 71.4 -- la paire de decision SUR UNE LIGNE. Elle rejoint ses quatre
    # soeurs ici et pour la meme raison exactement : sa garde est
    # `_authorize` -> `admin_api._strict_project_capability_allowed`, qui exige
    # une CONNEXION avant de decider, la ou le harnais de refus double
    # `core.project_access.resolve_strict_resource_access` sans base. La dette
    # est celle de la famille entiere, pas une exemption de ces deux-la : le
    # jour ou `_authorize` sera prouve par un appel refuse, les six sortent
    # ensemble.
    "project_capabilities_mcp.py:confirm_project_capability_row_decision",
    "project_capabilities_mcp.py:prepare_project_capability_change",
    "project_capabilities_mcp.py:prepare_project_capability_row_decision",
    "project_capabilities_mcp.py:preview_project_capability_impact",
    "project_capabilities_mcp.py:read_project_capability",
    # Les TROIS que la reparation du lecteur de boucles a RENDUS VISIBLES le
    # 2026-08-28 (elle n'en a cree aucun : ils etaient enregistres, gardes, et
    # simplement hors du compte parce que leur module ouvre DEUX boucles
    # `for handler in (...)` et que la garde ne lisait que la premiere). Ils
    # resolvent bien un acces -- ils ne figurent pas dans `offenders` -- mais
    # aucun appel refuse ne le prouve. La dette est inscrite ici plutot que
    # laissee invisible : c'est la difference entre un compte qui monte de 102 a
    # 110 et un compte qui n'a jamais su qu'il lui manquait huit outils.
    "metric_semantics_mcp.py:metric_mapping_confirm",
    "metric_semantics_mcp.py:metric_mapping_reject",
    "metric_semantics_mcp.py:metric_mapping_rename",
    "recovery_mcp.py:propose_datastream_recovery",
    "report_mcp.py:get_report",
})

#: Combien de sauts de delegation la garde suit. Deux suffisent aux chaines
#: mesurees (`flows_list -> list_flows -> _assert_access`) ; une fermeture
#: complete ferait passer pour garde tout outil qui appelle n'importe quoi.
_DELEGATION_DEPTH = 2


#: UNE SEULE LECTURE POUR LES DEUX, et c'est une reparation, pas un style.
#:
#: Le texte et l'arbre etaient lus a DEUX moments : `_SOURCES` a l'import du
#: module (donc a la collecte de pytest) et `_modules()` au premier test, cache.
#: Sur une suite complete, quinze minutes separent les deux -- et une session
#: voisine qui ecrit dans `server/core/` pendant ce temps donne un arbre dont les
#: numeros de ligne depassent la fin du texte. `ast.get_source_segment` leve
#: alors `IndexError` et les TROIS tests de ce fichier meurent sans rien dire du
#: code : mesure du 2026-08-11, `token_service.py` et `admin_api.py` reecrits a
#: 20:25 et 20:40 pendant un run lance a 20:32.
#:
#: Un garde qui explose quand le voisin travaille n'est pas un garde ; c'est un
#: faux rouge qu'on apprend a ignorer.
@lru_cache(maxsize=1)
def _read() -> tuple[dict[str, str], dict[str, ast.Module]]:
    sources: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for path in sorted(_CORE.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        sources[path.name] = text
        trees[path.name] = ast.parse(text)
    return sources, trees


def _modules() -> dict[str, ast.Module]:
    return _read()[1]


def _functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _called_names(node) -> set[str]:
    """Les noms REELLEMENT APPELES dans un corps, alias d'import resolus.

    Un import n'est pas un appel. Les outils de `core/` importent leurs gardes
    dans le corps (le seam anti-cycle documente dans `agent_surface_mcp`), et
    souvent sous un alias -- `resolve_strict_resource_access as _access`. Lire le
    texte creditait l'import ; lire les seuls noms appeles ne verrait que
    `_access`. Les deux erreurs se corrigent au meme endroit : on collecte les
    appels, et on traduit chaque alias vers ce qu'il importe.
    """
    aliases: dict[str, str] = {}
    for child in ast.walk(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name.rsplit(".", 1)[-1]
    called: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            called.add(aliases.get(name, name))
    return called


@lru_cache(maxsize=1)
def _definitions() -> dict[str, list[ast.AST]]:
    """Chaque fonction de `core/`, par nom (homonymes cumules)."""
    defined: dict[str, list[ast.AST]] = {}
    for tree in _modules().values():
        for node in _functions(tree):
            defined.setdefault(node.name, []).append(node)
    return defined


def _guard_bearing(defined: dict[str, list[ast.AST]]) -> set[str]:
    """Les fonctions qui prennent une decision d'acces, delegues compris."""
    bearing = {name for name, nodes in defined.items()
               if any(_called_names(n) & _ALL_GUARDS for n in nodes)}
    for _ in range(_DELEGATION_DEPTH):
        grown = {
            name for name, nodes in defined.items()
            if set().union(*(_called_names(n) for n in nodes)) & bearing
        }
        if grown <= bearing:
            break
        bearing |= grown
    return bearing


def _registered_tools(tree: ast.Module) -> set[str]:
    """Les trois formes d'enregistrement, ou qu'elles soient dans le module.

    `mcp.tool(f)` et `register_profiled(mcp, f, ...)` sont souvent INDENTEES dans
    un `def register(mcp)` -- la version regex precedente, ancree en `^mcp.tool(`,
    n'en voyait aucune. `ast.walk` ne se soucie pas de l'indentation.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
        if isinstance(node, ast.Call):
            func, arg = node.func, None
            if isinstance(func, ast.Attribute) and func.attr == "tool" and node.args:
                arg = node.args[0]
            elif (isinstance(func, ast.Name) and func.id == "register_profiled"
                  and len(node.args) >= 2):
                arg = node.args[1]
            if isinstance(arg, ast.Name):
                names.add(arg.id)
    # `for handler in (a, b): register_profiled(mcp, handler, ...)` --
    # plusieurs modules enregistrent ainsi, et nommer la variable `handler` les
    # aurait fait disparaitre du compte sans un mot.
    #
    # LA GARDE DE `node.target.id in names` A ETE RETIREE, et c'est la reparation.
    # Elle ne tenait que pour la PREMIERE boucle d'un module : celle-ci retirait
    # `handler` de `names`, si bien que la deuxieme boucle -- dont la cible porte
    # le meme nom -- ne satisfaisait plus la condition et etait ignoree en entier.
    # Mesure du 2026-08-28 : `context_hub_mcp`, `inbound_mcp` et
    # `metric_semantics_mcp` ont DEUX boucles chacun, et huit outils reels (sept
    # ecrivains) etaient invisibles a ce cliquet. Le compte scope est passe de 102
    # a 110 en retirant cette seule condition ; la liste des non gardes n'a pas
    # bouge. La lecture correcte vivait deja dans
    # `test_no_tax_specific_mcp_tools._registered_tool_names`, qui ne conditionne
    # rien : les deux lectures d'une meme question sont maintenant d'accord.
    loop_targets: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            continue
        registers = any(
            isinstance(inner, ast.Call)
            and (
                (isinstance(inner.func, ast.Name) and inner.func.id == "register_profiled")
                or (isinstance(inner.func, ast.Attribute) and inner.func.attr == "tool")
            )
            for inner in ast.walk(node)
        )
        if not registers:
            continue
        loop_targets.add(node.target.id)
        names.update(e.id for e in node.iter.elts if isinstance(e, ast.Name))
    # La variable de boucle n'est un outil que si la boucle elle-meme la nomme.
    names -= loop_targets - {
        element.id
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List))
        for element in node.iter.elts
        if isinstance(element, ast.Name)
    }
    return names


def _is_the_registrar(module: str, tree: ast.Module, name: str) -> bool:
    """`mcp_profiles.register_profiled` fait `mcp.tool(handler)` sur son PARAMETRE.

    Ce n'est pas un outil, c'est le guichet d'enregistrement lui-meme. On le
    reconnait a sa forme -- un nom qui est un parametre de la fonction ou l'appel
    vit -- et non a son nom de module : exempter `mcp_profiles.py` par son nom
    laisserait passer le prochain guichet.
    """
    del module
    for node in _functions(tree):
        params = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if name in params and name not in {f.name for f in _functions(tree)}:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    if isinstance(func, ast.Attribute) and func.attr == "tool":
                        if any(isinstance(a, ast.Name) and a.id == name
                               for a in inner.args):
                            return True
    return False


@lru_cache(maxsize=1)
def scan() -> tuple[list[str], list[str], list[str], list[str]]:
    """Rend (outils scopes, non gardes, enregistrements non resolus, sans portee).

    TOUT outil enregistre tombe desormais dans exactement un des trois seaux, et
    c'est la reparation de l'audit 12 (P1-3) : ``scoped`` (il nomme un projet, une
    org ou un datastream), ``unscoped`` (il n'en nomme aucun), ``unresolved`` (la
    garde ne sait pas lire son enregistrement). Aucun outil ne sort du compte en
    silence -- un compte qui baisse sans un mot est le contraire d'un cliquet.
    """
    defined_everywhere = _definitions()
    bearing = _guard_bearing(defined_everywhere)
    scoped: list[str] = []
    offenders: list[str] = []
    unresolved: list[str] = []
    unscoped: list[str] = []
    for module, tree in _modules().items():
        defined: dict[str, ast.AST] = {}
        for node in _functions(tree):
            defined.setdefault(node.name, node)
        for name in sorted(_registered_tools(tree)):
            node = defined.get(name)
            if node is None:
                if not _is_the_registrar(module, tree, name):
                    unresolved.append(f"{module}:{name}")
                continue
            params = [a.arg for a in node.args.args + node.args.kwonlyargs]
            label = f"{module}:{name}"
            calls = _called_names(node)
            guarded = bool(calls & _ALL_GUARDS) or bool(calls & (bearing - {name}))
            if not any(param in params for param in _SCOPE_PARAMS):
                if not guarded:
                    unscoped.append(label)
                continue
            scoped.append(label)
            if not guarded:
                offenders.append(label)
    return scoped, offenders, unresolved, unscoped


def test_every_scoped_mcp_tool_resolves_access(capsys):
    scoped, offenders, _, _ = scan()
    with capsys.disabled():
        print()
        print(f"  modules de core/ enregistrant des outils MCP : "
              f"{len({m for m, t in _modules().items() if _registered_tools(t)})}")
        print(f"  outils MCP enregistres, scopes ({'/'.join(_SCOPE_PARAMS)}) : {len(scoped)}")
        print(f"  sans resolution d'acces                      : "
              f"{len(offenders)}  {offenders}")

    unexpected = sorted(set(offenders) - _KNOWN_UNGUARDED)
    assert not unexpected, (
        "outil(s) MCP acceptant un `project_id`, un `org_id` ou un "
        "`datastream_id` de l'appelant sans jamais resoudre son acces :\n  "
        + "\n  ".join(unexpected) + "\n\n"
        "Appeler `core.mcp_scope.refuse_unless_project_scope(project_id, identity)` "
        "AVANT la premiere lecture, ou inscrire `<module>:<outil>` dans "
        "_KNOWN_UNGUARDED avec la raison. Le compte est passe de 4 a 9 en cinq "
        "jours precisement parce que rien ne le surveillait."
    )


#: LE VOCABULAIRE DU REFUS, et c'est un vocabulaire comme `_GUARDS` : il vieillit,
#: donc `test_every_proof_harness_is_readable` le fait vieillir bruyamment.
#:
#: Un test qui n'en nomme aucun mesure autre chose -- le rang exige, la
#: classification, la symetrie d'un porteur -- et ne prouve donc pas un refus.
#:
#: Les trois premiers sont les codes qu'une surface qui LEVE nomme. `_denied` est
#: la quatrieme forme et elle a ete trouvee par ce resserrement meme : la surface
#: inbound est scopee datastream et RETOURNE son refus (`inbound_mcp._denied(
#: "not_readable")`) au lieu de lever. Une premiere version de cette liste ne
#: connaissait que les codes leves et decredibilisait les six outils inbound --
#: dont la preuve est reelle, complete, et plus stricte que la moyenne (elle
#: compte les instructions SQL depensees avant le refus).
_REFUSAL_CODES = frozenset({
    "PROJECT_NOT_FOUND_CODE",
    "ORG_NOT_FOUND_CODE",
    "PLATFORM_SCOPE_CODE",
    "_denied",
})


def _parametrized_over(node) -> set[str]:
    """Les noms de dictionnaires qu'un test PARCOURT via `parametrize`.

    `@pytest.mark.parametrize("tool_name", sorted(_REPAIRED))` -- on rend
    `{"_REPAIRED"}`. Un dictionnaire qu'aucun `parametrize` ne parcourt
    n'engendre aucun cas de test, donc il ne prouve rien.
    """
    sources: set[str] = set()
    for raw in node.decorator_list:
        if not isinstance(raw, ast.Call):
            continue
        target = raw.func
        if not (isinstance(target, ast.Attribute) and target.attr == "parametrize"):
            continue
        for argument in raw.args[1:]:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Name):
                    sources.add(inner.id)
    return sources


@lru_cache(maxsize=1)
def _tools_proven_by_a_refused_call() -> frozenset[str]:
    """Les outils qu'un harnais appelle vraiment, et pour lesquels il exige un refus.

    CE QUE CETTE FONCTION LISAIT, ET POURQUOI C'ETAIT LA MEME PANNE QU'AILLEURS
    (reparation du 2026-08-24, chantier 67-1). Elle collectait les cles de
    N'IMPORTE QUEL dictionnaire de niveau module dans les fichiers de preuve. Or
    un dictionnaire n'est pas un appel refuse : `_VIEW_RANK_WRITES` explique un
    RANG, `_ORG_RANK_DEMANDED` en nomme un autre, et rien n'empechait le
    prochain `_QUELQUE_CHOSE = {...}` de crediter ses cles d'une preuve que
    personne n'avait ecrite. L'instrument lisait une liste et appelait cela une
    preuve -- exactement ce que son voisin `_PROVEN_BY_READING_ONLY` existe pour
    denoncer, reproduit a l'interieur de la fonction qui le mesure.

    Ce qui compte desormais est une CHAINE VERIFIABLE, derivee a chaque
    execution : un dictionnaire n'est credite que si un test le parcourt par
    `parametrize` **et** que le corps de ce test nomme une enveloppe de refus
    canonique. Un test qui mesure le rang, la classification ou la symetrie
    d'un porteur ne credite plus rien -- il ne prouve pas un refus.
    """
    proven: set[str] = set()
    for path in _PROOFS:
        proven |= _proven_by(path)
    assert proven, (
        "aucun harnais d'appel refuse n'a ete trouve dans tests/isolation/ : "
        f"cherche {[p.name for p in _PROOFS]}"
    )
    return frozenset(proven)


def _proven_by(path: Path) -> set[str]:
    """Ce qu'UN harnais credite : dictionnaire parcouru x test qui exige un refus."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dicts = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Dict)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    proven: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        if not _demands_a_refusal(node):
            continue
        for source in _parametrized_over(node) & set(dicts):
            proven.update(
                key.value
                for key in dicts[source].keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
    return proven


def _demands_a_refusal(node) -> bool:
    """Le corps nomme-t-il une enveloppe de refus canonique ?

    Les deux formes sont lues : un nom nu (`PROJECT_NOT_FOUND_CODE`) et un
    attribut (`inbound_mcp._denied`). Ne lire que la premiere est ce qui a fait
    perdre leur credit aux six outils inbound au premier tir.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id in _REFUSAL_CODES:
            return True
        if isinstance(inner, ast.Attribute) and inner.attr in _REFUSAL_CODES:
            return True
    return False


def test_every_proof_harness_is_readable(capsys):
    """UN harnais dont la garde ne sait pas lire le refus doit ROUGIR, pas se taire.

    C'est le mecanisme de vieillissement du vocabulaire `_REFUSAL_CODES`, et il
    est la parce que son absence a failli couter cher : la premiere version de
    ce resserrement ne connaissait que les codes LEVES, et
    `test_inbound_mcp_scope_refusal.py` -- qui retourne son refus au lieu de le
    lever -- ne creditait plus rien. Le symptome fut cinq outils accuses d'un
    coup, ce qui est lisible ; le symptome d'un harnais AJOUTE demain dans une
    quatrieme forme serait un credit silencieusement absent, ce qui ne l'est
    pas. Un harnais qui ne credite RIEN est un harnais que la garde ne sait pas
    lire, jamais un harnais sans preuve.
    """
    silent = [path.name for path in _PROOFS if not _proven_by(path)]
    with capsys.disabled():
        print()
        print(f"  harnais de refus lus : {len(_PROOFS)}  "
              f"{[p.name for p in _PROOFS]}")

    assert not silent, (
        "harnais de refus dont cette garde ne credite AUCUN outil :\n  "
        + "\n  ".join(silent) + "\n\n"
        "Soit le harnais ne parametre plus ses appels par un dictionnaire, soit "
        "il exprime son refus dans une forme absente de `_REFUSAL_CODES` -- "
        "ajoute-la la, avec ce qu'elle nomme. Un harnais muet fait passer pour "
        "non prouves des outils qui le sont, et la reparation evidente serait "
        "alors de les inscrire en dette : la garde aurait fabrique la dette "
        "qu'elle mesure."
    )


def test_the_proof_is_a_chain_and_not_a_dictionary(capsys):
    """L'instrument ne credite que ce qu'un test de refus PARCOURT.

    Sans ce test, la reparation ci-dessus serait invisible : rien ne dirait que
    la lecture s'est resserree, et rien n'empecherait de la relacher. La
    mutation qu'il refuse est exactement celle qui vivait ici -- crediter les
    cles d'un dictionnaire que nul `parametrize` ne parcourt.
    """
    loose: set[str] = set()
    for path in _PROOFS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                loose.update(
                    key.value
                    for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
    strict = _tools_proven_by_a_refused_call()
    with capsys.disabled():
        print()
        print(f"  cles de dictionnaire dans les harnais de preuve : {len(loose)}")
        print(f"  creditees par un test de refus parametre        : {len(strict)}")
        print(f"  creditees par la seule presence dans une liste  : "
              f"{len(loose - strict)}  {sorted(loose - strict)}")

    assert strict <= loose, (
        "la lecture stricte credite un nom que la lecture large ne voit pas : "
        "les deux ne lisent plus la meme chose"
    )


def test_a_guard_is_proven_by_a_refused_call_and_not_by_an_import(capsys):
    """La difference entre << une garde est nommee >> et << une garde refuse >>.

    CE QUE CE TEST EXISTE POUR ATTRAPER. `get_procedure` importait
    `resolve_strict_resource_access` dans son corps et l'appelait -- mais dans le
    bloc qui construit ses remarques. Le refus retirait les remarques ; le corps
    de la Skill, sa sequence standardisee et ses references MDM partaient quand
    meme a qui savait nommer le projet. Le scan du source ne peut pas voir cela :
    il voit une decision prise, jamais ce qu'elle gouverne. Un appel refuse, si.
    """
    scoped, _, _, _ = scan()
    proven = _tools_proven_by_a_refused_call()
    reading_only = {
        label for label in scoped
        if label.split(":", 1)[1] not in proven and label not in _KNOWN_UNGUARDED
    }
    with capsys.disabled():
        print()
        print(f"  outils scopes projet prouves par un appel refuse : "
              f"{len(scoped) - len(reading_only) - len(_KNOWN_UNGUARDED)}")
        print(f"  credites par la seule lecture du source          : "
              f"{len(reading_only)}")

    unexpected = sorted(reading_only - _PROVEN_BY_READING_ONLY)
    assert not unexpected, (
        "outil(s) MCP scopes projet dont la garde n'est verifiee que par lecture "
        "du source :\n  " + "\n  ".join(unexpected) + "\n\n"
        "Ajoute-le au harnais `tests/isolation/test_mcp_tool_scope_refusal.py` "
        "(`_REPAIRED`), qui l'appelle avec une identite sans droit et exige le "
        "refus AVANT toute lecture. Un import de garde n'est pas une garde."
    )


def test_the_reading_only_debt_does_not_go_stale():
    """Un outil prouve par un appel refuse ne reste pas inscrit comme dette."""
    scoped, _, _, _ = scan()
    proven = _tools_proven_by_a_refused_call()
    still_reading_only = {
        label for label in scoped if label.split(":", 1)[1] not in proven
    }
    repaired = sorted(_PROVEN_BY_READING_ONLY - still_reading_only)
    assert not repaired, (
        "outil(s) desormais prouves par un appel refuse mais toujours inscrits "
        "comme dette :\n  " + "\n  ".join(repaired)
        + "\n\nRetire-les de _PROVEN_BY_READING_ONLY : la reparation doit compter."
    )


def test_the_exception_list_does_not_go_stale():
    """Un nom reste exempte tant qu'il l'est, et pas une execution de plus."""
    _, offenders, _, _ = scan()
    repaired = sorted(_KNOWN_UNGUARDED - set(offenders))
    assert not repaired, (
        "outil(s) desormais gardes mais toujours inscrits comme exception :\n  "
        + "\n  ".join(repaired)
        + "\n\nRetire-les de _KNOWN_UNGUARDED : la reparation doit compter."
    )


def test_every_registered_tool_is_accounted_for(capsys):
    """AUCUN outil enregistre ne sort du compte -- audit 12, P1-3.

    Le cliquet ne comptait qu'une forme de portee, donc un outil qui n'en prenait
    aucune n'existait pour personne : ni garde, ni exempte, ni compte. Ce test
    ferme le troisieme seau. Tout outil enregistre est desormais soit scope (et
    alors il resout un acces, teste plus haut), soit garde par une decision de
    plateforme, soit inscrit dans `_NO_TENANT_SCOPE` avec la phrase qui dit
    pourquoi il n'a rien a scoper. Une raison vide ne compte pas pour une raison.
    """
    scoped, _, _, unscoped = scan()
    with capsys.disabled():
        print()
        print(f"  outils MCP enregistres, toutes formes         : "
              f"{len(scoped) + len(unscoped) + len(_NO_TENANT_SCOPE)}")
        print(f"  sans portee ET sans decision d'acces          : {len(unscoped)}")

    unexpected = sorted(set(unscoped) - set(_NO_TENANT_SCOPE))
    assert not unexpected, (
        "outil(s) MCP enregistres qui ne nomment aucune ressource et ne resolvent "
        "aucun acces :\n  " + "\n  ".join(unexpected) + "\n\n"
        "Soit l'outil touche une ressource de tenant -- alors il doit prendre sa "
        "portee (`project_id`, `org_id` ou `datastream_id`) et la resoudre ; soit "
        "il n'en touche aucune -- alors inscris `<module>:<outil>` dans "
        "`_NO_TENANT_SCOPE` avec la phrase qui dit ce qu'il lit. Un outil compte "
        "nulle part est exactement ce que ce fichier existe pour empecher."
    )
    empty = sorted(name for name, why in _NO_TENANT_SCOPE.items() if not (why or "").strip())
    assert not empty, (
        "entree(s) de `_NO_TENANT_SCOPE` sans raison ecrite :\n  " + "\n  ".join(empty)
    )


def test_the_no_scope_ledger_does_not_go_stale():
    """Un outil qui a gagne une portee ou une garde ne reste pas inscrit sans."""
    scoped, _, _, unscoped = scan()
    repaired = sorted(set(_NO_TENANT_SCOPE) - set(unscoped))
    assert not repaired, (
        "outil(s) desormais scopes ou gardes mais toujours inscrits comme sans "
        "portee :\n  " + "\n  ".join(repaired)
        + "\n\nRetire-les de `_NO_TENANT_SCOPE` : la reparation doit compter."
    )
    del scoped


def test_no_registration_escapes_the_scan():
    """Un enregistrement que la garde ne sait pas resoudre est un angle mort.

    Sans ce test, une forme d'enregistrement nouvelle (un decorateur maison, une
    boucle sur un dict) ferait DISPARAITRE ses outils du compte -- et un compte
    qui baisse en silence est le contraire d'un cliquet. Le scan doit dire
    << je ne sais pas lire ceci >>, jamais << il n'y a rien >>.
    """
    _, _, unresolved, _ = scan()
    assert not unresolved, (
        "enregistrement(s) MCP que la garde ne sait pas relier a une fonction :\n  "
        + "\n  ".join(sorted(unresolved))
        + "\n\nCes outils ne sont comptes NULLE PART. Etendre `_registered_tools`."
    )


def test_the_guard_vocabulary_knows_every_access_decision_helper():
    """`core/project_access.py` grandit ; `_GUARDS` doit grandir avec lui.

    Le vocabulaire de la garde est une liste, et une liste vieillit. Celle-ci
    vieillit BRUYAMMENT : le jour ou quelqu'un ajoute une fonction de decision
    d'acces et l'emploie dans un outil, la garde declarerait cet outil non garde
    -- un faux positif qui pousse a exempter plutot qu'a reparer. Ce test le
    transforme en une ligne a ajouter ici.
    """
    tree = ast.parse((_CORE / "project_access.py").read_text(encoding="utf-8"))
    public = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    missing = sorted(public - _GUARDS - _NOT_AN_ACCESS_DECISION)
    assert not missing, (
        "point(s) d'entree de `core/project_access.py` que la garde ne reconnait "
        "pas comme une decision d'acces :\n  " + "\n  ".join(missing) + "\n\n"
        "Ajoute-les a `_GUARDS`, ou a `_NOT_AN_ACCESS_DECISION` si la fonction "
        "repond a autre chose que << cet appelant a-t-il droit >>."
    )


def test_the_refusal_reuses_the_canonical_not_found_envelope():
    """Une seconde enveloppe << interdit >> serait un oracle d'enumeration.

    Le refus de portee doit etre indistinguable d'un projet absent. La preuve
    par le comportement vit dans `tests/isolation/test_mcp_tool_scope_refusal.py` ;
    ce test-ci empeche qu'un futur contributeur introduise le mot `forbidden`
    dans le seam en croyant bien faire.
    """
    tree = ast.parse((_CORE / "mcp_scope.py").read_text(encoding="utf-8"))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "refuse_unless_project_scope"
    )
    # LE CODE, PAS LA PROSE. La premiere version de ce test lisait le texte brut
    # de la fonction et echouait sur son PROPRE docstring, qui explique pourquoi
    # une enveloppe << forbidden >> serait un oracle. Un test qui ne sait pas
    # distinguer un mot employe d'un mot cite mesure la documentation.
    node.body = [n for n in node.body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    )]
    code = ast.unparse(node)

    assert "_raise_not_found" in code, (
        "le seam de refus n'utilise plus l'enveloppe canonique de project_resolver"
    )
    for word in ("forbidden", "unauthorized", "access_denied", "permission_denied"):
        assert word not in code, (
            f"le seam introduit un code distinct ({word!r}) : comparer deux refus "
            "apprendrait alors qu'un projet existe"
        )
