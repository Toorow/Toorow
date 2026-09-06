"""AI-172 -- `/api/procedures` n'existe plus, et son absence est TENUE ici.

**L'histoire, gardee parce qu'elle explique pourquoi ce fichier ne teste plus un
comportement.**

AI-125 avait ferme sur cette route une fuite inter-tenant : elle n'avait ni
authentification ni autorisation, et un appelant sans jeton nommant le projet
d'un autre recevait `200` avec ses methodes de reconciliation. La reparation
etait juste, et les cinq tests qu'elle portait ont tenu jusqu'ici.

Ce que cette reparation ne disait pas -- elle l'ecrivait meme noir sur blanc --
c'est ce qu'un ecran devait montrer de ces methodes. La question a ete instruite
le 2026-08-05 (AI-172), et la reponse retire la route plutot qu'elle n'ajoute un
ecran :

* **la surface existe deja** : lens `reconciliation` de
  `governance/controls-quality`, servie par
  `core.governance_read_model._reconciliation_lens`, qui liste les rule sets
  gouvernes de la famille `metric_reconciliation` ;
* **`app.metric_procedures` est un magasin retire** par la migration 145 (story
  49.4), qui nomme elle-meme le travail restant : « They stop being AUTHORITIES
  in this story; the DROP belongs to the commit that removes the last reader. »
  Ce handler etait ce dernier lecteur ;
* **0 ligne en production** le 2026-08-05, six jours apres la mesure de 145 ;
* **les vocabulaires different** : `sum_then_divide | priority_source |
  dedup_union | weighted_blend | manual_override` (migration 095) contre
  `SUM | PRIORITY | DEDUP_ID | ESTIMATE | KEEP_SEPARATE`
  (`core/controls_quality.py`). Batir un ecran sur le premier aurait cree le
  second magasin, le second vocabulaire et le second ecran pour une seule chose
  -- ce que le « Incomplete if » de `docs/product-architecture/governance.md`
  interdit explicitement.

**Pourquoi un fichier de test pour une absence.** Un retrait silencieux se fait
remonter par le lecteur suivant, qui n'a aucun moyen de savoir qu'il rouvre une
porte : c'est exactement ce qui est arrive une fois deja
(`_create_datastream_mapping_version`, SESSIONS.md). Une route remontee fait
rougir ce fichier, avec la raison en toutes lettres.

La garde de portee d'AI-125 n'est pas perdue avec le handler : elle est devenue
`_refuse_unless_project_allowed`, que six routes appellent depuis AI-171, et
`tests/core/test_admin_route_project_scope.py` tient la classe entiere.
"""

from __future__ import annotations

import ast
from pathlib import Path

ADMIN_API = Path(__file__).resolve().parents[2] / "core" / "admin_api.py"


def _mounted_paths() -> set[str]:
    """Les chemins de la table de routes, lus dans l'AST plutot qu'au grep.

    Un `grep "/api/procedures"` rendrait aussi le commentaire de retrait qui
    remplace le handler, et un gate qui se declenche sur son propre commentaire
    ne mesure rien -- meme precaution que le cliquet d'AI-125.
    """
    tree = ast.parse(ADMIN_API.read_text(encoding="utf-8"))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Route" or not node.args:
            continue
        if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            paths.add(node.args[0].value)
    return paths


def test_the_route_has_not_been_remounted():
    """`GET /api/procedures` ne revient pas sans que la cible ne change d'abord."""
    assert "/api/procedures" not in _mounted_paths(), (
        "`/api/procedures` est remontee. Elle lit `app.metric_procedures`, un "
        "magasin que la migration 145 a remplace par la famille gouvernee "
        "`metric_reconciliation`, et la surface qui montre les methodes de "
        "reconciliation est la lens `reconciliation` de "
        "`governance/controls-quality`. Si la cible a change, amender "
        "docs/product-architecture/governance.md AVANT de remonter la route."
    )


def test_its_handler_is_gone_and_its_removal_is_recorded():
    """Le handler ne survit pas a sa route -- le defaut nomme par AI-126.

    Un handler vivant sans `Route` se relit comme un oubli et se remonte ; il
    porte aussi une garde de portee que plus rien n'exerce. Les deux partent
    ensemble, et le commentaire qui reste dit pourquoi.
    """
    source = ADMIN_API.read_text(encoding="utf-8")
    assert "async def _list_procedures(" not in source, (
        "le handler `_list_procedures` est revenu sans sa route"
    )
    assert "RETIREE LE 2026-08-05 (AI-172)" in source, (
        "la trace du retrait a disparu : sans elle, le prochain lecteur remonte "
        "la route en croyant reparer un oubli"
    )


def test_no_reader_of_the_retired_store_came_back():
    """`app.metric_procedures` n'a plus de lecteur dans le code de production.

    C'est la condition que la migration 145 pose a son propre DROP. Ce test la
    rend verifiable au lieu de la laisser a la memoire de la prochaine session.
    """
    server = ADMIN_API.parents[1]
    readers = [
        f"{path.relative_to(server)}:{number}"
        for path in sorted(server.rglob("*.py"))
        if "__pycache__" not in path.parts and "tests" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "metric_procedures" in line and not line.strip().startswith("#")
    ]
    assert not readers, (
        "un lecteur de `app.metric_procedures` est reapparu, ce qui rebloque le "
        "DROP prevu par la migration 145 : " + ", ".join(readers)
    )
