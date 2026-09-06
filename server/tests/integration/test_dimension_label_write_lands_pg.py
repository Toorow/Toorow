"""Nommer une dimension par la porte MCP POSE-T-IL UNE LIGNE ? — story 67.24.

CE QUE CE FICHIER EXISTE POUR ATTRAPER, ET IL L'A ATTRAPÉ. `set_dimension_label`
est gardé : la portée est refusée, la plateforme est refusée, l'identifiant de
portée est exigé, l'écriture passe par une connexion armée. Tout cela est vrai
et tout cela était vert — pendant que **l'écriture n'avait jamais posé une seule
ligne**.

Mesuré le 2026-08-22 contre une vraie base : `validate_scope("org", ...)` lève
`InvalidScope: unknown scope_level: 'org'`. Le magasin ne connaît que MAJUSCULES
(`app.dimension_labels`, migration 106:69, et ses trois sœurs des migrations 049
et 052) ; l'outil MCP documente `org` / `project` en minuscules ; rien entre les
deux ne convertissait. Et l'échec ne se disait même pas : `InvalidScope` dérive
de `MetricSemanticsError`, pas de `ValueError`, donc le `except ValueError` de
l'outil ne l'attrapait pas et l'exception remontait nue.

**Aucune des gardes existantes ne pouvait le voir**, et c'est la leçon : elles
mesurent toutes des REFUS. Un outil dont tous les refus fonctionnent et dont
l'acceptation ne pose rien est vert partout. La seule mesure qui l'attrape est
`SELECT COUNT(*)` après un appel qui devait réussir — c'est ce fichier.

La porte REST jumelle normalisait déjà (`dimension_lineage_api:250`,
`.strip().upper()`), donc la casse n'était pas une décision à prendre : c'était
la même réparation, jamais appliquée à la seconde porte.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- une ecriture ne se prouve pas sans base",
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def org_with_owner(live_postgres):
    """Une organisation et un PROPRIETAIRE : le rang que la porte exige.

    `set_dimension_label` demande `manage` sur l'organisation
    (`refuse_unless_org_scope(..., minimum_capability="manage")`), et l'ecriture
    traverse ensuite une connexion armee dont la politique RLS de
    `app.dimension_labels` est le second plancher. Un membre simple serait refuse
    par le premier et ne dirait rien du second.
    """
    conn = live_postgres
    org_id, project_id = _uid("org"), _uid("proj")
    subject = f"owner_{uuid.uuid4().hex[:10]}@example.com"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, 'Story 67.24 fixture', %s, 'active', %s)",
            (org_id, org_id.replace("_", "-"), subject),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, 'Story 67.24 fixture', %s, %s)",
            (project_id, org_id, project_id.replace("_", "-"), subject),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status) "
            "VALUES (%s, %s, %s, 'owner', 'active')",
            (_uid("om"), org_id, subject),
        )
    conn.commit()
    yield {"org_id": org_id, "project_id": project_id, "subject": subject}


def _tool_bodies(monkeypatch, subject: str) -> dict:
    """Les CORPS des deux outils, attrapes a l'enregistrement.

    `register_profiled` est importe DANS `register()` (le seam anti-cycle de tout
    `server/core`), donc c'est `core.mcp_profiles` qu'il faut remplacer, jamais
    un attribut du module appelant -- il n'en a pas. Une doublure posee au mauvais
    endroit rendrait `AttributeError`, ce qui est au moins bruyant ; posee sur un
    module qui l'expose par hasard, elle rendrait un test vert qui ne joue rien.
    """
    from core import dimension_labels_mcp, mcp_profiles

    captured: dict = {}
    monkeypatch.setattr(
        mcp_profiles,
        "register_profiled",
        lambda mcp, fn, **kw: captured.setdefault(fn.__name__, fn),
    )
    monkeypatch.setattr(dimension_labels_mcp, "_identity", lambda: subject)

    class _Registry:
        def tool(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            def _wrap(fn):
                captured.setdefault(fn.__name__, fn)
                return fn

            return _wrap

    dimension_labels_mcp.register(_Registry())
    assert "set_dimension_label" in captured, "l'outil ne s'est pas enregistre"
    return captured


def _labels(conn, org_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope_level, canonical_dimension, display_label "
            "FROM app.dimension_labels WHERE org_id = %s ORDER BY canonical_dimension",
            (org_id,),
        )
        return cur.fetchall()


@requires_postgres
def test_the_word_the_model_writes_reaches_the_table(org_with_owner, live_postgres):
    """LA MESURE QUI MANQUAIT : la ligne existe-t-elle apres l'appel ?"""
    from core.dimension_conformance import set_dimension_label

    org_id = org_with_owner["org_id"]
    assert _labels(live_postgres, org_id) == []

    # Le mot du MAGASIN, celui que la frontiere produit.
    set_dimension_label(
        canonical_dimension="country",
        display_label="Pays",
        scope_level="ORG",
        org_id=org_id,
        project_id=None,
        identity=org_with_owner["subject"],
    )
    assert _labels(live_postgres, org_id) == [("ORG", "country", "Pays")]


@requires_postgres
def test_the_store_refuses_the_lowercase_word_the_tool_documents(org_with_owner):
    """Le defaut lui-meme, epingle -- pour qu'on ne le repare pas deux fois.

    Ce test ne demande pas au magasin de changer d'avis : il epingle qu'il refuse
    la minuscule, ce qui est la RAISON pour laquelle la frontiere doit traduire.
    Le jour ou quelqu'un enleve la traduction de `dimension_labels_mcp`, le test
    suivant rougit et celui-ci explique pourquoi.
    """
    from core.dimension_conformance import InvalidScope, validate_scope

    with pytest.raises(InvalidScope):
        validate_scope("org", org_with_owner["org_id"], None)
    # Et il ne derive PAS de ValueError, ce qui est la seconde moitie du defaut :
    # l'outil n'attrapait que `ValueError`.
    assert not issubclass(InvalidScope, ValueError)


@requires_postgres
def test_the_mcp_door_translates_and_the_row_lands(org_with_owner, live_postgres, monkeypatch):
    """La porte MCP, appelee avec SON vocabulaire, pose la ligne.

    C'est le parcours entier : le mot minuscule que l'outil documente entre, la
    frontiere le traduit, le magasin l'accepte, la table le porte. Avant la
    reparation du 2026-08-22, cet appel levait `InvalidScope` nu.
    """
    org_id = org_with_owner["org_id"]
    tool = _tool_bodies(monkeypatch, org_with_owner["subject"])["set_dimension_label"]
    answer = tool(
        canonical_dimension="channel",
        display_label="Canal",
        # LE MOT MINUSCULE, celui que le docstring de l'outil promet.
        scope_level="org",
        org_id=org_id,
    )
    assert "error" not in answer, answer
    assert _labels(live_postgres, org_id) == [("ORG", "channel", "Canal")]


@requires_postgres
def test_a_store_refusal_comes_back_NAMED_and_not_as_a_bare_exception(
    org_with_owner, monkeypatch
):
    """La seconde moitie du defaut : `InvalidScope` remontait NUE.

    Le magasin leve `InvalidScope` sur un triplet incoherent ; l'outil
    n'attrapait que `ValueError`, dont `InvalidScope` ne derive pas. Le modele
    recevait donc une panne la ou il y avait un refus nommable -- et un modele
    qui recoit une panne reessaie.

    LE REFUS EST FORCE PLUTOT QUE PROVOQUE, et c'est delibere : les gardes de
    portee de l'outil tirent AVANT le magasin (elles rendent `project_not_found`
    sur un projet que l'appelant ne voit pas), donc un triplet reellement
    incoherent n'atteint jamais la clause qu'on veut mesurer. Ce que ce test
    tient est exactement cette clause : quoi que le magasin leve, la porte rend
    un dict nomme.
    """
    from core import dimension_conformance

    def _refuse(*_args, **_kwargs):
        raise dimension_conformance.InvalidScope("unknown scope_level: 'ORG'")

    monkeypatch.setattr(dimension_conformance, "set_dimension_label", _refuse)
    tool = _tool_bodies(monkeypatch, org_with_owner["subject"])["set_dimension_label"]

    answer = tool(
        canonical_dimension="country",
        display_label="Pays",
        scope_level="org",
        org_id=org_with_owner["org_id"],
    )
    assert answer == {
        "error": "invalid_scope",
        "message": "unknown scope_level: 'ORG'",
    }


@requires_postgres
def test_a_blank_label_still_comes_back_as_the_other_named_refusal(
    org_with_owner, monkeypatch
):
    """La clause `ValueError` n'a pas ete perdue en ajoutant la sienne."""
    tool = _tool_bodies(monkeypatch, org_with_owner["subject"])["set_dimension_label"]
    answer = tool(
        canonical_dimension="country",
        display_label="   ",
        scope_level="org",
        org_id=org_with_owner["org_id"],
    )
    assert answer["error"] == "invalid_label"
