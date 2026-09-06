"""AI-287 -- la marche du dispatch gouverne, eprouvee seule.

POURQUOI CE FICHIER N'EXISTAIT PAS. La regle vivait dans une cloture sur
`nonlocal dispatch`, au milieu d'une fonction de 869 lignes, avec huit points
d'appel. Rien ne pouvait l'exercer sans piloter un import entier -- ni la regle
d'ordre, ni ce qui arrive a un etat qu'elle ne connait pas.

CE QUE L'EXTRACTION A TROUVE, et c'est la raison d'etre du garde. La cloture
testait :

    if state in order and current in order and order[current] > order[state]:

Un etat ABSENT de l'ordre ne satisfaisait aucune clause : il etait ecrit sans le
moindre controle. QUATRE des huit points d'appel etaient dans ce cas --
`promoting`, `reconcile_required`, `rejected`, `failed` -- et rien ne le disait.
La garde se lisait comme si elle couvrait tous les etats.

Ils sont maintenant des SORTIES declarees, avec leur propre porte. La difference
n'est pas cosmetique : une sortie s'ecrit inconditionnellement, un pas ne peut
pas reculer, et le lecteur voit lequel des deux se produit a l'endroit ou il lit.
"""
from __future__ import annotations

import pytest
from core.import_runner import _GovernedDispatchWalk
from core.tabular_types import CsvExcelImportError


class _FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _walk(state: str | None = "pending", conn=None):
    return _GovernedDispatchWalk(
        conn or _FakeConn(),
        dispatch=None if state is None else {"id": "mfd_EXAMPLE", "state": state},
        dispatch_id="mfd_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        project_id="proj_EXAMPLE",
    )


def _writes(monkeypatch) -> list[str]:
    """Capture les etats REELLEMENT ecrits, sans base."""
    written: list[str] = []

    def _fake_advance(conn, **kwargs):
        written.append(kwargs["state"])
        return {"id": "mfd_EXAMPLE", "state": kwargs["state"]}

    monkeypatch.setattr(
        "core.managed_file_dispatch.advance_dispatch", _fake_advance, raising=False
    )
    return written


def test_a_forward_step_is_written(monkeypatch):
    written = _writes(monkeypatch)
    walk = _walk("landing")

    walk.advance("landed")

    assert written == ["landed"]
    assert walk.state == "landed"


def test_a_backward_step_is_dropped_not_raised(monkeypatch):
    """Un rejeu qui redemande `landing` sur une ligne deja `ready`.

    L'honorer ferait reculer le coordinateur durable d'une ecriture croisee : le
    candidat en entrepot serait alors decrit par un etat anterieur a sa propre
    existence. Et c'est un NO-OP, pas une erreur -- l'appelant demande un etat
    que la marche a deja passe, ce qui arrive normalement sur un rejeu.
    """
    written = _writes(monkeypatch)
    walk = _walk("ready")

    result = walk.advance("landing")

    assert written == []
    assert walk.state == "ready"
    assert result["state"] == "ready"


def test_the_same_step_twice_writes_once(monkeypatch):
    written = _writes(monkeypatch)
    walk = _walk("validating")

    walk.advance("validating")

    assert written == []


def test_an_unknown_state_is_refused_rather_than_dropped(monkeypatch):
    """L'inverse exact du recul, et pour la raison inverse.

    Un nom inconnu ne peut pas etre compare a l'ordre : il ne peut donc etre ni
    honore ni ignore sans risque. Le laisser tomber en silence bloquerait un
    dispatch sans que rien ne le dise -- c'est precisement ce que faisait la
    cloture d'origine.
    """
    written = _writes(monkeypatch)
    walk = _walk("landing")

    with pytest.raises(CsvExcelImportError) as excinfo:
        walk.advance("halfway")

    assert "dispatch_state_unknown" in str(excinfo.value.code)
    assert written == []


@pytest.mark.parametrize(
    "exit_state", ["promoting", "reconcile_required", "rejected", "failed"]
)
def test_every_exit_is_written_whatever_the_walk_had_reached(monkeypatch, exit_state):
    """Les quatre etats que l'ancienne garde ne regardait pas.

    Une sortie n'est pas un pas : un import rejete est rejete quel que soit le
    point qu'il avait atteint. Depuis `ready`, donc -- l'etat le plus avance,
    celui ou une regle d'ordre refuserait tout.
    """
    written = _writes(monkeypatch)
    walk = _walk("ready")

    walk.leave(exit_state, error_code="whatever")

    assert written == [exit_state]
    assert walk.state == exit_state


def test_a_step_may_not_be_taken_through_the_exit_door(monkeypatch):
    """Sinon `leave` serait un contournement de la regle d'ordre.

    Les deux portes doivent rester etanches dans les DEUX sens, ou la
    distinction ne veut rien dire.
    """
    written = _writes(monkeypatch)
    walk = _walk("ready")

    with pytest.raises(CsvExcelImportError):
        walk.leave("landing")

    assert written == []


def test_no_intent_recorded_means_no_dispatch_at_all():
    """Le chemin sans publication : `current` est None, et le reste.

    Une charge utile qui dit `dispatch: None` est honnete ; une qui en invente un
    ferait croire a un dispatch gouverne la ou il n'y en a jamais eu.
    """
    walk = _GovernedDispatchWalk()

    assert walk.current is None
    assert walk.state == "pending"


def test_adopt_takes_the_database_at_its_word(monkeypatch):
    """La reconciliation relit la verite durable, et elle gagne.

    `advance` refuse de reculer parce qu'une demande de recul est un bug. Une
    ligne relue en base est autre chose : Postgres est l'autorite sur ce que
    l'etat EST, et une marche qui la refuserait continuerait de croire un etat
    que la base a deja dementi.
    """
    written = _writes(monkeypatch)
    walk = _walk("ready")

    walk.adopt({"id": "mfd_EXAMPLE", "state": "landing"})

    assert walk.state == "landing"
    # Rien n'est ecrit : `adopt` enregistre une lecture, il ne mute pas.
    assert written == []


def test_each_write_commits_so_a_reconciliation_can_see_it(monkeypatch):
    """Postgres coordonne l'ecriture croisee : chaque pas doit etre VISIBLE.

    Sans le commit, une reconciliation concurrente lirait un etat anterieur au
    travail deja engage cote entrepot -- exactement l'incertitude que le
    dispatch existe pour lever.
    """
    _writes(monkeypatch)
    conn = _FakeConn()
    walk = _walk("landing", conn=conn)

    walk.advance("landed")
    walk.leave("failed", error_code="dq_gate_failed")

    assert conn.commits == 2


class _FakeLedger:
    """Le ledger, reduit a ce que l'etape lui demande."""

    def __init__(self):
        self.marked = []
        self.OUTCOME_FAILED = "failed"

    def mark_outcome(self, **kwargs):
        self.marked.append(kwargs)


def test_a_refused_import_discards_its_candidate(monkeypatch):
    """AI-287 -- LE CANDIDAT EST JETE, et c'est la partie porteuse.

    `_refuse_on_dq_gates` fait quatre choses qui doivent arriver ENSEMBLE, ce qui
    est precisement pourquoi elles etaient emmelees dans une fonction de
    856 lignes : l'execution passe a FAILED, le ledger enregistre le meme
    resultat, le CANDIDAT en entrepot est jete, et le dispatch QUITTE la marche
    gouvernee.

    La troisieme est celle qui mord. Un candidat laisse derriere est de la donnee
    non confirmee posee dans l'entrepot sous un id d'execution -- la publication
    suivante la trouverait. Avant cette extraction, rien ne pouvait l'eprouver
    sans piloter un import entier.
    """
    from core import import_runner

    discarded, advanced, left = [], [], []
    ledger = _FakeLedger()

    monkeypatch.setattr("core.managed_feed_ledger.mark_outcome",
                        lambda **kw: ledger.marked.append(kw), raising=False)
    monkeypatch.setattr("core.managed_feed_ledger.OUTCOME_FAILED", "failed", raising=False)
    monkeypatch.setattr("core.raw_landing.discard_candidate",
                        lambda rel, ex, **kw: discarded.append((rel, ex)), raising=False)
    monkeypatch.setattr("core.datastream_publication.advance_state",
                        lambda *a, **kw: advanced.append(a[1:3]) or {"state": "failed"},
                        raising=False)
    monkeypatch.setattr("core.datastream_publication.STATE_FAILED", "failed", raising=False)

    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: left.append(s) or {})

    out = import_runner._refuse_on_dq_gates(
        conn=None,
        dq_issues=[{"code": "row_count_below_floor"}],
        walk=walk,
        current_state="validating",
        execution_id="ex_EXAMPLE",
        ledger_id="mfl_EXAMPLE",
        ledger_after={"outcome": "written"},
        observed_execution={"state": "validating"},
        project_id="proj_EXAMPLE",
        actor="person_TEST",
        landing_relation="raw.managed_feed_ds_EXAMPLE",
        import_contract_id="ic_EXAMPLE",
        replay=False,
        accepted=10,
        rejected=2,
    )

    # 3 -- le candidat est jete, sous le NOM DE TABLE seul, jamais la relation
    # qualifiee : `discard_candidate` attend un nom, pas `schema.table`.
    assert discarded == [("managed_feed_ds_EXAMPLE", "ex_EXAMPLE")]
    # 1 -- l'execution transitionne depuis l'etat OBSERVE, pas un relu
    assert advanced == [("validating", "failed")]
    # 2 -- le ledger dit la meme chose
    assert ledger.marked and ledger.marked[0]["error_code"] == "dq_gate_failed"
    # 4 -- le dispatch SORT de la marche, il n'y avance pas
    assert left == ["failed"]

    assert out["blocked"] is True
    assert out["published"] is False
    assert out["reason"] == "dq_gate_failed"


def test_an_already_failed_execution_is_not_transitioned_again(monkeypatch):
    """La moitie qu'on oublie : ne pas re-transitionner ce qui a deja echoue.

    `advance_state` refuse une transition depuis un etat que la ligne n'occupe
    plus. Rejouer un import deja FAILED doit donc jeter le candidat et rendre sa
    charge utile SANS toucher a l'etat -- et l'execution deja observee est
    conservee, pas remplacee par None.
    """
    from core import import_runner

    advanced = []
    monkeypatch.setattr("core.managed_feed_ledger.mark_outcome", lambda **kw: None, raising=False)
    monkeypatch.setattr("core.managed_feed_ledger.OUTCOME_FAILED", "failed", raising=False)
    monkeypatch.setattr("core.raw_landing.discard_candidate", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("core.datastream_publication.advance_state",
                        lambda *a, **kw: advanced.append(a) or {}, raising=False)
    monkeypatch.setattr("core.datastream_publication.STATE_FAILED", "failed", raising=False)

    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: {})
    observed = {"state": "failed", "id": "ex_EXAMPLE"}

    out = import_runner._refuse_on_dq_gates(
        conn=None, dq_issues=[{"code": "x"}], walk=walk, current_state="failed",
        execution_id="ex_EXAMPLE", ledger_id="mfl_EXAMPLE", ledger_after={},
        observed_execution=observed, project_id="proj_EXAMPLE", actor="person_TEST",
        landing_relation="raw.t", import_contract_id=None, replay=True,
        accepted=0, rejected=0,
    )

    assert advanced == []
    assert out["execution"] is observed


# ---------------------------------------------------------------------------
# AI-287, steps 3-8 -- the seams lifted from run_import by 332b9e54, each one
# provable without driving a whole import. Same pattern as above: no database,
# the collaborators are captured at the module attribute the helper resolves.
# ---------------------------------------------------------------------------


def _capture_advances(monkeypatch) -> list[dict]:
    """Capture every `advance_dispatch` call with its FULL kwargs.

    `_writes` above keeps only the state; the promotion/publication seams also
    carry an error code and reconciliation evidence, and those are the part a
    settling read depends on.
    """
    recorded: list[dict] = []

    def _fake_advance(conn, **kwargs):
        recorded.append(kwargs)
        return {"id": "mfd_EXAMPLE", "state": kwargs["state"]}

    monkeypatch.setattr(
        "core.managed_file_dispatch.advance_dispatch", _fake_advance, raising=False
    )
    return recorded


def test_no_publication_opens_no_dispatch(monkeypatch):
    """`_open_governed_dispatch` on the non-publish path records NO intent.

    The walk exists on both paths so the caller has one variable, but it must
    stay empty: an import that publishes no candidate has no governed dispatch,
    and the payload says `dispatch: None` rather than inventing one.
    """
    from core import import_runner

    intents = []
    monkeypatch.setattr(
        "core.managed_file_dispatch.record_intent",
        lambda *a, **kw: intents.append(kw) or {"id": "mfd_EXAMPLE", "state": "pending"},
        raising=False,
    )

    walk, dispatch_id, settled = import_runner._open_governed_dispatch(
        None,
        publish_candidate=False,
        source_metadata={},
        bundle={},
        ledger={},
        ledger_id="mfl_EXAMPLE",
        execution={},
        execution_id="ex_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        project_id="proj_EXAMPLE",
        import_contract_id=None,
    )

    assert intents == []
    assert dispatch_id is None
    assert settled is None
    assert walk.current is None


def test_a_publication_without_raw_evidence_is_refused_before_any_intent(monkeypatch):
    """No immutable raw-import evidence, no governed publication -- and nothing written."""
    from core import import_runner

    intents = []
    monkeypatch.setattr(
        "core.managed_file_dispatch.record_intent",
        lambda *a, **kw: intents.append(kw) or {},
        raising=False,
    )

    with pytest.raises(CsvExcelImportError) as excinfo:
        import_runner._open_governed_dispatch(
            _FakeConn(),
            publish_candidate=True,
            source_metadata={"raw_import_id": "   "},
            bundle={},
            ledger={},
            ledger_id="mfl_EXAMPLE",
            execution={},
            execution_id="ex_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            project_id="proj_EXAMPLE",
            import_contract_id=None,
        )

    assert excinfo.value.code == "dispatch_raw_import_missing"
    assert intents == []


def test_an_already_published_dispatch_settles_the_import_as_a_replay(monkeypatch):
    """The durable coordinator may already hold the answer, and then it IS the payload."""
    from core import import_runner

    monkeypatch.setattr(
        "core.managed_file_dispatch.record_intent",
        lambda *a, **kw: {"id": "mfd_EXAMPLE", "state": "published"},
        raising=False,
    )
    monkeypatch.setattr(
        "core.managed_feed_ledger.OUTCOME_PUBLISHED", "published", raising=False
    )

    walk, dispatch_id, settled = import_runner._open_governed_dispatch(
        _FakeConn(),
        publish_candidate=True,
        source_metadata={"raw_import_id": "ri_EXAMPLE"},
        bundle={},
        ledger={"row_count": 7, "rejected_row_count": 1},
        ledger_id="mfl_EXAMPLE",
        execution={"id": "ex_EXAMPLE"},
        execution_id="ex_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        project_id="proj_EXAMPLE",
        import_contract_id="cic_EXAMPLE",
    )

    assert dispatch_id == "mfd_EXAMPLE"
    assert settled is not None
    assert settled["replay"] is True
    assert settled["published"] is True
    assert settled["outcome"] == "published"
    assert settled["row_count"] == 7
    assert settled["dispatch"] is walk.current


def test_a_failed_reconciliation_rolls_back_and_adopts_a_fresh_intent(monkeypatch):
    """The one exception flow that RECOMPOSES state, kept whole on purpose.

    A reconciliation that itself fails leaves the connection unusable; the
    rollback and the fresh intent that follows are the second half of the same
    decision. The walk must end up believing the FRESH row, not the broken one.
    """
    from core import import_runner

    intents = []

    def _record_intent(conn, **kw):
        intents.append(kw)
        if len(intents) == 1:
            return {"id": "mfd_EXAMPLE", "state": "promoting"}
        return {"id": "mfd_EXAMPLE2", "state": "pending"}

    def _reconcile_boom(conn, **kw):
        raise TimeoutError("reconciliation could not read the warehouse")

    monkeypatch.setattr(
        "core.managed_file_dispatch.record_intent", _record_intent, raising=False
    )
    monkeypatch.setattr(
        "core.managed_file_dispatch.reconcile_dispatch", _reconcile_boom, raising=False
    )

    conn = _FakeConn()
    walk, dispatch_id, settled = import_runner._open_governed_dispatch(
        conn,
        publish_candidate=True,
        source_metadata={"raw_import_id": "ri_EXAMPLE"},
        bundle={},
        ledger={},
        ledger_id="mfl_EXAMPLE",
        execution={},
        execution_id="ex_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        project_id="proj_EXAMPLE",
        import_contract_id=None,
    )

    assert conn.rollbacks == 1
    assert len(intents) == 2
    assert walk.current == {"id": "mfd_EXAMPLE2", "state": "pending"}
    assert settled is None


def test_a_diverged_candidate_leaves_the_walk_before_the_exception_escapes(monkeypatch):
    """`_observe_landed_candidate`: the exit is WRITTEN, then the raise.

    An exception that escaped first would leave a candidate no reader could
    account for -- the whole reason the state write precedes the raise.
    """
    from core import import_runner

    left = []
    walk = _walk("landing")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: left.append((s, kw)) or {})
    monkeypatch.setattr(
        "core.raw_landing.inspect_candidate",
        lambda *a, **kw: {
            "content_fingerprint": "something-else",
            "schema_fingerprint": "sf",
            "row_count": 3,
        },
        raising=False,
    )

    with pytest.raises(CsvExcelImportError) as excinfo:
        import_runner._observe_landed_candidate(
            walk=walk,
            execution_id="ex_EXAMPLE",
            project_id="proj_EXAMPLE",
            landing_relation="raw.managed_feed_ds_EXAMPLE",
            landed={"backend": None},
            warehouse_columns=[],
            accepted_row_count=3,
            expected_content_fingerprint="cf",
            expected_schema_fingerprint="sf",
        )

    assert excinfo.value.code == "candidate_evidence_diverged"
    assert left and left[0][0] == "reconcile_required"
    assert left[0][1]["error_code"] == "candidate_evidence_diverged"


def test_a_matching_candidate_advances_and_returns_the_observed_pair(monkeypatch):
    """The expected fingerprints are a claim; only the observed pair is evidence."""
    from core import import_runner

    written = _writes(monkeypatch)
    monkeypatch.setattr(
        "core.raw_landing.inspect_candidate",
        lambda *a, **kw: {
            "content_fingerprint": "cf-observed",
            "schema_fingerprint": "sf-observed",
            "row_count": 3,
        },
        raising=False,
    )

    walk = _walk("landing")
    pair = import_runner._observe_landed_candidate(
        walk=walk,
        execution_id="ex_EXAMPLE",
        project_id="proj_EXAMPLE",
        landing_relation="raw.managed_feed_ds_EXAMPLE",
        landed={"backend": None},
        warehouse_columns=[],
        accepted_row_count=3,
        expected_content_fingerprint="cf-observed",
        expected_schema_fingerprint="sf-observed",
    )

    assert written == ["landed"]
    assert pair == ("cf-observed", "sf-observed")


def test_the_merge_is_announced_before_it_runs(monkeypatch):
    """`_promote_candidate_or_reconcile`: intent in Postgres FIRST, merge second.

    An announced promotion whose merge fails is recoverable; a merge nobody
    announced is data that arrived under no record.
    """
    from core import import_runner

    calls = []
    monkeypatch.setattr(
        "core.datastream_publication.begin_managed_file_promotion",
        lambda *a, **kw: calls.append("announce"),
        raising=False,
    )
    monkeypatch.setattr(
        "core.raw_landing.promote_candidate",
        lambda *a, **kw: calls.append("merge") or {"merged_rows": 3},
        raising=False,
    )

    promotion = import_runner._promote_candidate_or_reconcile(
        _FakeConn(),
        walk=_walk("ready"),
        dispatch_id="mfd_EXAMPLE",
        project_id="proj_EXAMPLE",
        execution_id="ex_EXAMPLE",
        actor="person_TEST",
        landing_relation="raw.managed_feed_ds_EXAMPLE",
        landed={"backend": None},
        warehouse_columns=[],
        accepted_row_count=3,
        candidate_evidence={},
        candidate_content_fingerprint="cf",
        candidate_schema_fingerprint="sf",
    )

    assert calls == ["announce", "merge"]
    assert promotion == {"merged_rows": 3}


def test_an_uncertain_merge_is_translated_and_leaves_for_reconciliation(monkeypatch):
    """Every failure in the merge half means one thing: the warehouse is UNCERTAIN.

    The provider's exception must not escape -- a timeout and a schema refusal
    leave exactly the same question for a reconciliation to answer -- and the
    dispatch exit must be durable before anything propagates.
    """
    from core import import_runner
    from core.managed_file_dispatch import ManagedFileDispatchError

    recorded = _capture_advances(monkeypatch)
    monkeypatch.setattr(
        "core.datastream_publication.begin_managed_file_promotion",
        lambda *a, **kw: None,
        raising=False,
    )

    def _boom(*a, **kw):
        raise TimeoutError("socket dropped mid-merge")

    monkeypatch.setattr("core.raw_landing.promote_candidate", _boom, raising=False)

    conn = _FakeConn()
    walk = _walk("ready", conn=conn)
    with pytest.raises(ManagedFileDispatchError) as excinfo:
        import_runner._promote_candidate_or_reconcile(
            conn,
            walk=walk,
            dispatch_id="mfd_EXAMPLE",
            project_id="proj_EXAMPLE",
            execution_id="ex_EXAMPLE",
            actor="person_TEST",
            landing_relation="raw.managed_feed_ds_EXAMPLE",
            landed={"backend": None},
            warehouse_columns=[],
            accepted_row_count=3,
            candidate_evidence={},
            candidate_content_fingerprint="cf",
            candidate_schema_fingerprint="sf",
        )

    assert excinfo.value.code == "dispatch_promotion_reconciliation_required"
    assert [kw["state"] for kw in recorded] == ["reconcile_required"]
    assert recorded[0]["error_code"] == "dispatch_promotion_reconciliation_required"
    # The exit is committed before the exception propagates, so a concurrent
    # reconciliation sees it.
    assert conn.commits == 1
    assert walk.state == "reconcile_required"


def test_a_publication_that_returns_its_dispatch_is_adopted_not_rewritten(monkeypatch):
    """`_commit_publication_or_reconcile` on success: no second dispatch write."""
    from core import import_runner

    recorded = _capture_advances(monkeypatch)
    publication = {
        "dispatch": {"id": "mfd_EXAMPLE", "state": "published"},
        "execution": {"id": "ex_EXAMPLE", "state": "published"},
        "publication_log_id": "pl_EXAMPLE",
        "prior_execution_id": None,
    }
    monkeypatch.setattr(
        "core.datastream_publication.commit_publication",
        lambda *a, **kw: publication,
        raising=False,
    )

    walk = _walk("ready")
    out = import_runner._commit_publication_or_reconcile(
        _FakeConn(),
        walk=walk,
        dispatch_id="mfd_EXAMPLE",
        project_id="proj_EXAMPLE",
        execution_id="ex_EXAMPLE",
        ledger_id="mfl_EXAMPLE",
        actor="person_TEST",
        candidate_evidence={},
        promotion={"merged_rows": 3},
    )

    assert out is publication
    assert recorded == []
    assert walk.state == "published"


def test_a_failed_pointer_swap_reraises_the_original_and_carries_the_promotion(monkeypatch):
    """Unlike its sibling, this half re-raises the ORIGINAL exception.

    A publication failure has a code the caller acts on; flattening it would
    turn a governed refusal into an opaque reconciliation. And the evidence
    carries the promotion, so a settling read knows the warehouse half already
    happened.
    """
    from core import import_runner

    recorded = _capture_advances(monkeypatch)

    class _GovernedRefusal(Exception):
        code = "publication_gate_refused"

    def _boom(*a, **kw):
        raise _GovernedRefusal()

    monkeypatch.setattr(
        "core.datastream_publication.commit_publication", _boom, raising=False
    )

    conn = _FakeConn()
    walk = _walk("ready", conn=conn)
    with pytest.raises(_GovernedRefusal):
        import_runner._commit_publication_or_reconcile(
            conn,
            walk=walk,
            dispatch_id="mfd_EXAMPLE",
            project_id="proj_EXAMPLE",
            execution_id="ex_EXAMPLE",
            ledger_id="mfl_EXAMPLE",
            actor="person_TEST",
            candidate_evidence={},
            promotion={"merged_rows": 3},
        )

    assert [kw["state"] for kw in recorded] == ["reconcile_required"]
    assert recorded[0]["error_code"] == "publication_gate_refused"
    assert recorded[0]["reconciliation_evidence"]["promotion"] == {"merged_rows": 3}
    assert conn.commits == 1


def test_a_rejection_without_publication_touches_only_the_ledger(monkeypatch):
    """`_refuse_on_rejection_gate` is conditional on `publish_candidate` throughout.

    A rejection can refuse an import that never opened a governed dispatch at
    all: no candidate to discard, no execution to fail, no walk to leave -- only
    the ledger outcome, and a payload whose `dispatch` is honestly None.
    """
    from core import import_runner

    discarded, advanced, left, marked = [], [], [], []
    monkeypatch.setattr(
        "core.managed_feed_ledger.mark_outcome",
        lambda **kw: marked.append(kw),
        raising=False,
    )
    monkeypatch.setattr(
        "core.managed_feed_ledger.OUTCOME_REJECTED", "rejected", raising=False
    )
    monkeypatch.setattr(
        "core.raw_landing.discard_candidate",
        lambda *a, **kw: discarded.append(a),
        raising=False,
    )
    monkeypatch.setattr(
        "core.datastream_publication.advance_state",
        lambda *a, **kw: advanced.append(a),
        raising=False,
    )

    walk = _GovernedDispatchWalk()
    monkeypatch.setattr(walk, "leave", lambda s, **kw: left.append(s) or {})

    observed = {"state": "created", "id": "ex_EXAMPLE"}
    out = import_runner._refuse_on_rejection_gate(
        None,
        gate_issue={"code": "rejection_rate_exceeded", "detail": "d"},
        walk=walk,
        publish_candidate=False,
        execution=observed,
        execution_id="ex_EXAMPLE",
        ledger_id="mfl_EXAMPLE",
        ledger_after={"outcome": "rejected"},
        project_id="proj_EXAMPLE",
        actor="person_TEST",
        landing_relation="raw.managed_feed_ds_EXAMPLE",
        import_contract_id=None,
        replay=False,
        accepted=10,
        rejected=9,
    )

    assert discarded == [] and advanced == [] and left == []
    assert marked and marked[0]["outcome"] == "rejected"
    assert out["reason"] == "rejection_threshold_exceeded"
    assert out["dispatch"] is None
    # The observed execution is returned untouched, never replaced by None.
    assert out["execution"] is observed


def test_a_rejected_publication_does_the_same_four_things_as_its_sibling(monkeypatch):
    """The DQ refusal's SIBLING, on the rejection gate: same four effects.

    Execution to FAILED from the OBSERVED state, ledger marked rejected,
    candidate discarded under its bare table name, and the dispatch LEAVES the
    walk through `rejected` -- an exit, not a step.
    """
    from core import import_runner

    discarded, advanced, left, marked = [], [], [], []
    monkeypatch.setattr(
        "core.managed_feed_ledger.mark_outcome",
        lambda **kw: marked.append(kw),
        raising=False,
    )
    monkeypatch.setattr(
        "core.managed_feed_ledger.OUTCOME_REJECTED", "rejected", raising=False
    )
    monkeypatch.setattr(
        "core.raw_landing.discard_candidate",
        lambda rel, ex, **kw: discarded.append((rel, ex)),
        raising=False,
    )
    monkeypatch.setattr(
        "core.datastream_publication.advance_state",
        lambda *a, **kw: advanced.append(a[1:3]) or {"state": "failed"},
        raising=False,
    )
    monkeypatch.setattr(
        "core.datastream_publication.STATE_FAILED", "failed", raising=False
    )

    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: left.append(s) or {})

    out = import_runner._refuse_on_rejection_gate(
        None,
        gate_issue={"code": "rejection_rate_exceeded", "detail": "d"},
        walk=walk,
        publish_candidate=True,
        execution={"state": "validating", "id": "ex_EXAMPLE"},
        execution_id="ex_EXAMPLE",
        ledger_id="mfl_EXAMPLE",
        ledger_after={"outcome": "rejected"},
        project_id="proj_EXAMPLE",
        actor="person_TEST",
        landing_relation="raw.managed_feed_ds_EXAMPLE",
        import_contract_id=None,
        replay=False,
        accepted=10,
        rejected=9,
    )

    assert discarded == [("managed_feed_ds_EXAMPLE", "ex_EXAMPLE")]
    assert advanced == [("validating", "failed")]
    assert marked and marked[0]["outcome"] == "rejected"
    assert left == ["rejected"]
    assert out["blocked"] is True
    assert out["published"] is False


# ---------------------------------------------------------------------------
# AI-287, the three contradictions 332b9e54 found and left. Each is settled by
# the rule the repository already carries -- one machine, one door, the
# principle AI-223 applied to `advance_state` -- and each gets the guard that
# would have caught it.
# ---------------------------------------------------------------------------


def test_the_walk_is_the_only_door_to_advance_dispatch_in_this_module():
    """CONTRADICTION 1: two spellings of the same exit, one of them ungoverned.

    `reconcile_required` was written four times: twice through `walk.leave` (the
    two landing seams) and twice through a raw `advance_dispatch` (the promotion
    and publication seams). The raw pair bypassed the walk, so `_EXITS` never
    validated them -- the door that refuses `walk.leave("landing")` was simply
    not in the way. `published` had the same hole on the success side.

    Proved from the SOURCE, not from behaviour, because that is what the defect
    was: a call site is only governed when there is no other way out of the
    module. A behavioural test cannot see the difference -- both spellings end
    in the same `advance_dispatch`.
    """
    import ast
    import inspect

    from core import import_runner

    tree = ast.parse(inspect.getsource(import_runner))
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
            if name == "advance_dispatch":
                callers.add(node.name)

    assert callers == {"_write"}, (
        "advance_dispatch is reachable outside _GovernedDispatchWalk._write: "
        f"{sorted(callers)}"
    )


def test_the_publication_seam_leaves_through_the_walk(monkeypatch):
    """The half of contradiction 1 that no behavioural guard covered.

    `test_a_failed_pointer_swap_...` pins the state and the evidence; it cannot
    tell a raw write from a governed exit. This pins the door itself.
    """
    from core import import_runner

    left = []

    class _GovernedRefusal(Exception):
        code = "publication_gate_refused"

    def _boom(*a, **kw):
        raise _GovernedRefusal()

    monkeypatch.setattr(
        "core.datastream_publication.commit_publication", _boom, raising=False
    )

    walk = _walk("ready")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: left.append((s, kw)) or {})

    with pytest.raises(_GovernedRefusal):
        import_runner._commit_publication_or_reconcile(
            _FakeConn(),
            walk=walk,
            dispatch_id="mfd_EXAMPLE",
            project_id="proj_EXAMPLE",
            execution_id="ex_EXAMPLE",
            ledger_id="mfl_EXAMPLE",
            actor="person_TEST",
            candidate_evidence={},
            promotion={"merged_rows": 3},
        )

    assert [state for state, _ in left] == ["reconcile_required"]


def test_the_promotion_seam_leaves_through_the_walk(monkeypatch):
    """Its sibling, on the merge half. Same door, same reason."""
    from core import import_runner
    from core.managed_file_dispatch import ManagedFileDispatchError

    left = []

    def _boom(*a, **kw):
        raise TimeoutError("socket dropped mid-merge")

    monkeypatch.setattr(
        "core.datastream_publication.begin_managed_file_promotion",
        lambda *a, **kw: None,
        raising=False,
    )
    monkeypatch.setattr("core.raw_landing.promote_candidate", _boom, raising=False)

    walk = _walk("ready")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: left.append((s, kw)) or {})

    with pytest.raises(ManagedFileDispatchError):
        import_runner._promote_candidate_or_reconcile(
            _FakeConn(),
            walk=walk,
            dispatch_id="mfd_EXAMPLE",
            project_id="proj_EXAMPLE",
            execution_id="ex_EXAMPLE",
            actor="person_TEST",
            landing_relation="raw.managed_feed_ds_EXAMPLE",
            landed={"backend": None},
            warehouse_columns=[],
            accepted_row_count=3,
            candidate_evidence={},
            candidate_content_fingerprint="cf",
            candidate_schema_fingerprint="sf",
        )

    assert [state for state, _ in left] == ["reconcile_required"]
    assert left[0][1]["error_code"] == "dispatch_promotion_reconciliation_required"


def _gate_collaborators(monkeypatch, *, on_mark_outcome):
    """The collaborators both gates reach, captured in CALL ORDER.

    ONE list, not one per collaborator: the defect being guarded is an order, and
    separate lists cannot express which happened first.
    """
    order: list[str] = []

    def _mark(**kw):
        order.append("ledger")
        on_mark_outcome()

    monkeypatch.setattr("core.managed_feed_ledger.mark_outcome", _mark, raising=False)
    monkeypatch.setattr(
        "core.managed_feed_ledger.OUTCOME_FAILED", "failed", raising=False
    )
    monkeypatch.setattr(
        "core.managed_feed_ledger.OUTCOME_REJECTED", "rejected", raising=False
    )
    monkeypatch.setattr(
        "core.raw_landing.discard_candidate",
        lambda *a, **kw: order.append("discard"),
        raising=False,
    )
    monkeypatch.setattr(
        "core.datastream_publication.advance_state",
        lambda *a, **kw: order.append("failed") or {"state": "failed"},
        raising=False,
    )
    monkeypatch.setattr(
        "core.datastream_publication.STATE_FAILED", "failed", raising=False
    )
    return order


def _refuse_through(gate, walk, *, current_state="validating"):
    """Drive either gate, each with the shape its own signature asks for."""
    from core import import_runner

    if gate == "dq":
        return import_runner._refuse_on_dq_gates(
            conn=None,
            dq_issues=[{"code": "row_count_below_floor"}],
            walk=walk,
            current_state=current_state,
            execution_id="ex_EXAMPLE",
            ledger_id="mfl_EXAMPLE",
            ledger_after={},
            observed_execution={"state": current_state, "id": "ex_EXAMPLE"},
            project_id="proj_EXAMPLE",
            actor="person_TEST",
            landing_relation="raw.managed_feed_ds_EXAMPLE",
            import_contract_id=None,
            replay=False,
            accepted=10,
            rejected=2,
        )
    return import_runner._refuse_on_rejection_gate(
        None,
        gate_issue={"code": "rejection_rate_exceeded", "detail": "d"},
        walk=walk,
        publish_candidate=True,
        execution={"state": current_state, "id": "ex_EXAMPLE"},
        execution_id="ex_EXAMPLE",
        ledger_id="mfl_EXAMPLE",
        ledger_after={},
        project_id="proj_EXAMPLE",
        actor="person_TEST",
        landing_relation="raw.managed_feed_ds_EXAMPLE",
        import_contract_id=None,
        replay=False,
        accepted=10,
        rejected=9,
    )


@pytest.mark.parametrize("gate", ["rejection", "dq"])
def test_both_gates_discard_the_candidate_before_the_ledger_records_it(
    monkeypatch, gate
):
    """CONTRADICTION 2: the two gates disagreed on the ORDER. They no longer do.

    The rejection gate discarded the candidate before marking the ledger; the DQ
    gates marked the ledger first. Only one of those two writes is durable on its
    own -- `discard_candidate` reaches the OTHER store and nothing rolls it back,
    while `mark_outcome` waits for the commit `walk.leave` performs -- so the
    order decides which store may be ahead of the other when the process dies.
    """
    order = _gate_collaborators(monkeypatch, on_mark_outcome=lambda: None)
    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: order.append("exit") or {})

    _refuse_through(gate, walk)

    assert order == ["failed", "discard", "ledger", "exit"]


@pytest.mark.parametrize("gate", ["rejection", "dq"])
def test_a_ledger_write_that_dies_leaves_no_candidate_behind(monkeypatch, gate):
    """The same order, proved where it is the only thing that matters.

    The SECOND of the two writes is made to fail. What must hold afterwards is
    that the warehouse is already clean: a ledger row that never closed is
    re-runnable and no reader mistakes it for confirmed data, while unconfirmed
    rows left under a terminal execution id are found by the next publication and
    nothing ever comes back to remove them.
    """

    def _boom():
        raise RuntimeError("the ledger connection died")

    order = _gate_collaborators(monkeypatch, on_mark_outcome=_boom)
    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: order.append("exit") or {})

    with pytest.raises(RuntimeError):
        _refuse_through(gate, walk)

    assert "discard" in order
    # And nothing was committed: the exit -- which owns the commit -- never ran.
    assert "exit" not in order


@pytest.mark.parametrize("gate", ["rejection", "dq"])
def test_an_unreadable_execution_state_is_handed_to_the_machine_not_skipped(
    monkeypatch, gate
):
    """CONTRADICTION 3: two answers to "and if the execution has no state".

    The rejection gate skipped the transition (`if current_state and ...`); the
    DQ gates attempted it (`if current_state not in {STATE_FAILED}`). Skipping is
    the one that cannot be right: `advance_state` is the single writer of
    `app.datastream_executions.state` (AI-223) and it re-reads the live row when
    given no expectation, refusing an invalid transition itself. A caller that
    skips decides, on no evidence, that the execution stays where it is -- and
    the payload then carries a refusal the execution row does not corroborate,
    which is the disagreement between the two stores these gates exist to
    prevent.
    """
    order = _gate_collaborators(monkeypatch, on_mark_outcome=lambda: None)
    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: order.append("exit") or {})

    _refuse_through(gate, walk, current_state=None)

    assert order == ["failed", "discard", "ledger", "exit"]


@pytest.mark.parametrize("gate", ["rejection", "dq"])
def test_an_execution_already_failed_is_the_only_state_that_skips(monkeypatch, gate):
    """The other half of the single answer, and why it is not "always call".

    FAILED is the destination. Asking to move there from there is not a governed
    refusal to observe, it is the caller asking for what already holds.
    """
    order = _gate_collaborators(monkeypatch, on_mark_outcome=lambda: None)
    walk = _walk("validating")
    monkeypatch.setattr(walk, "leave", lambda s, **kw: order.append("exit") or {})

    _refuse_through(gate, walk, current_state="failed")

    assert order == ["discard", "ledger", "exit"]
