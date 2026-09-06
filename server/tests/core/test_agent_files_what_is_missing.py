"""Une machine depose ce qu'elle a trouve manquant -- Story 45.8.

POURQUOI CE FICHIER. Mesure du 2026-08-05 :

    grep 'origin="agent"' server --include=*.py (hors tests)
      -> 1 resultat, et c'etait UN COMMENTAIRE

Le schema acceptait `agent`, `request_review` prenait le parametre, la
resolution humaine ecrivait le lien `derived`, la console lisait les rejets
recurrents. Il manquait le PRODUCTEUR : la file se remplissait par relecture et
jamais par l'usage -- exactement la moitie que le cas LangGraph decrivait.

CE QUE CES TESTS TIENNENT :

  * une proposition PORTE sa charge quand les DEUX bouts sont nommes, et ne la
    porte PAS quand un seul l'est -- deviner l'autre bout serait une devinette
    avec de meilleures manieres ;
  * la note est DERIVEE du fait, jamais datee ni comptee : le meme fait produit
    la meme note, donc UNE ligne ouverte et pas une par execution ;
  * le seuil se franchit UNE fois ;
  * un retrait declare (anti-declencheur) ne descend meme pas dans l'agregat,
    donc ne peut pas etre propose.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core import candidate_fate, context_review


class _Conn:
    """Une base qui refuse tout : ces chemins doivent s'arreter avant elle."""

    def cursor(self):
        raise AssertionError("no statement should reach the database")


def _capture(monkeypatch) -> list[dict]:
    """Remplace l'ecriture par sa capture -- on teste CE QUI EST DEPOSE."""
    filed: list[dict] = []

    def fake_request_review(conn, **kwargs):
        filed.append(kwargs)
        return {"id": f"crr_{len(filed)}", **kwargs}

    monkeypatch.setattr(context_review, "request_review", fake_request_review)
    return filed


# --- ce qu'une proposition porte, et ce qu'elle refuse de porter ------------


def test_a_proven_gap_carries_the_link_and_the_evidence_that_produced_it(monkeypatch) -> None:
    filed = _capture(monkeypatch)

    context_review.propose_missing_link(
        _Conn(),
        org_id="org_1",
        project_id="proj_1",
        node_type="procedure",
        node_id="proc_1",
        node_version=3,
        taxonomy_type="business_domain",
        taxonomy_id="bdm_marketing",
        relation_type="applies_to",
        evidence="missing_path on route rte_1 (evaluation run evr_9)",
    )

    assert len(filed) == 1
    request = filed[0]
    assert request["origin"] == "agent"
    assert request["requested_by"] == context_review.AGENT_AUTHOR
    assert request["proposed_change"] == {
        "kind": "business_link",
        "taxonomy_type": "business_domain",
        "taxonomy_id": "bdm_marketing",
        "relation_type": "applies_to",
    }
    # Une proposition qui ne peut pas dire ce qui l'a produite est une devinette.
    assert "missing_path on route rte_1" in request["note"]


def test_a_recurring_rejection_names_no_target_because_it_knows_none(monkeypatch) -> None:
    """Le signal ne nomme qu'un bout : le candidat. La requete est du texte libre,
    pas une taxonomie -- en deduire un metier serait choisir a la place de
    quelqu'un."""
    filed = _capture(monkeypatch)

    context_review.flag_recurring_rejection(
        _Conn(),
        org_id="org_1",
        project_id="proj_1",
        node_type="procedure",
        node_id="proc_1",
        node_version=3,
        reason=candidate_fate.REASON_OUT_OF_SCOPE,
        times=3,
        last_query="attribution window",
    )

    assert len(filed) == 1
    request = filed[0]
    assert request["origin"] == "agent"
    assert "proposed_change" not in request or request.get("proposed_change") is None
    # ⚠️ LA REQUETE N'EST PLUS DANS LA NOTE, et c'est une sortie de relecture :
    # `md5(note)` fait partie de l'index unique, donc un texte de recherche dans
    # la note faisait DEUX lignes ouvertes pour le meme noeud des que la requete
    # changeait. Elle se lit ou elle vit, dans les rejets recurrents.
    assert "attribution window" not in request["note"]
    assert candidate_fate.REASON_OUT_OF_SCOPE in request["note"]


def test_the_note_does_not_carry_the_count_so_the_same_fact_is_one_row(monkeypatch) -> None:
    """`times` monte a chaque recherche. L'inclure ferait une note differente a
    chaque passage -- donc une ligne de plus a chaque passage, et une file qu'on
    apprend a ignorer. L'idempotence est CONSTRUITE, pas rattrapee."""
    filed = _capture(monkeypatch)

    for count in (3, 4, 91):
        context_review.flag_recurring_rejection(
            _Conn(),
            org_id="org_1",
            project_id="proj_1",
            node_type="procedure",
            node_id="proc_1",
            node_version=3,
            reason=candidate_fate.REASON_OUT_OF_SCOPE,
            times=count,
            last_query="attribution window",
        )

    notes = {request["note"] for request in filed}
    assert len(notes) == 1

    # Et le meme noeud ecarte AILLEURS, sur d'AUTRES mots : toujours une note.
    for query in ("attribution window", "pacing budget", None):
        context_review.flag_recurring_rejection(
            _Conn(),
            org_id="org_1",
            project_id=None,
            node_type="procedure",
            node_id="proc_1",
            node_version=3,
            reason=candidate_fate.REASON_OUT_OF_SCOPE,
            times=7,
            last_query=query,
        )
    assert len({request["note"] for request in filed}) == 1


# --- le declenchement, au moment ou l'ecart est prouve ----------------------


def _served_conn(rows, versions=()):
    """Une base qui repond aux lectures du producteur.

    `fetchone` sert la lecture de l'organisation ; `fetchall` sert la lecture
    GROUPEE des versions -- une instruction pour tous les candidats, pas une par
    candidat (le cout que la relecture du 2026-08-05 a nomme).

    `transaction()` est fourni parce que le producteur le PREND : sans lui, le
    faux ferait lire << rien depose >> la ou le code depose -- le piege que trois
    autres faux de ce depot ont deja tendu.
    """
    import contextlib

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.transaction.side_effect = lambda: contextlib.nullcontext()
    cur.fetchone.side_effect = rows
    cur.fetchall.return_value = list(versions)
    return conn, cur


def test_the_crossing_files_one_remark_on_the_node_it_names(monkeypatch) -> None:
    filed = _capture(monkeypatch)
    conn, _cur = _served_conn([("org_1",)], versions=[("proc_1", None, 4)])

    ids = context_review.flag_recurring_rejections(
        conn,
        project_id="proj_1",
        crossed=[{
            "candidate_id": "proc_1",
            "candidate_kind": "procedure",
            "reason": candidate_fate.REASON_BELOW_CUTOFF,
            "times": 3,
            "last_query": "attribution window",
        }],
    )

    assert len(ids) == 1
    assert filed[0]["node_type"] == "procedure"
    assert filed[0]["node_id"] == "proc_1"
    assert filed[0]["node_version"] == 4
    # Un noeud de PLATEFORME concerne tout le monde : sa remarque aussi. La
    # rattacher au projet qui l'a revelee la cacherait aux autres.
    assert filed[0]["project_id"] is None


def test_a_candidate_that_is_not_a_hub_node_is_not_flagged(monkeypatch) -> None:
    """`schema_doc` et `target_field` sont ecartes comme les autres et n'ont pas
    de remarque possible : la table n'accepte que topic et procedure."""
    filed = _capture(monkeypatch)
    conn, _cur = _served_conn([("org_1",)])

    ids = context_review.flag_recurring_rejections(
        conn,
        project_id="proj_1",
        crossed=[{
            "candidate_id": "sch_1",
            "candidate_kind": "schema_doc",
            "reason": candidate_fate.REASON_BELOW_CUTOFF,
            "times": 3,
            "last_query": "q",
        }],
    )

    assert ids == []
    assert filed == []


def test_a_node_that_no_longer_exists_is_not_flagged(monkeypatch) -> None:
    filed = _capture(monkeypatch)
    conn, _cur = _served_conn([("org_1",)], versions=[])

    ids = context_review.flag_recurring_rejections(
        conn,
        project_id="proj_1",
        crossed=[{
            "candidate_id": "proc_gone",
            "candidate_kind": "procedure",
            "reason": candidate_fate.REASON_BELOW_CUTOFF,
            "times": 3,
            "last_query": "q",
        }],
    )

    assert ids == []
    assert filed == []


def test_a_failure_to_file_never_breaks_the_search_that_revealed_it(monkeypatch) -> None:
    """Observer ne casse jamais l'observe : la recherche a deja rendu sa reponse."""
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("database is on fire")

    crossed = [{"candidate_id": "proc_1", "candidate_kind": "procedure"}]
    assert context_review.flag_recurring_rejections(
        conn, project_id="proj_1", crossed=crossed
    ) == []


def test_nothing_crossed_files_nothing_and_touches_no_database() -> None:
    assert context_review.flag_recurring_rejections(
        _Conn(), project_id="proj_1", crossed=[]
    ) == []


# --- le lien avec l'anti-declencheur ---------------------------------------


def test_a_declared_drop_cannot_reach_this_loop() -> None:
    """45.5 le tient deja depuis l'autre bout : un retrait que l'auteur a ecrit
    ne descend pas dans l'agregat, donc ne franchit aucun seuil, donc n'est
    jamais propose. Le prouver ici est ce qui empeche une future story de le
    defaire par megarde."""
    assert candidate_fate.REASON_ANTI_TRIGGER in candidate_fate.UNRECORDED_REASONS


@pytest.mark.parametrize("kind", ["topic", "procedure"])
def test_both_hub_node_kinds_are_flaggable(kind: str) -> None:
    assert context_review._FLAGGABLE_KINDS[kind] == kind
