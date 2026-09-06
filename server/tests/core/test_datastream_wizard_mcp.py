"""La porte MCP du parcours gouverné — story 67.23.

CE QUE CES TESTS TIENNENT. La porte délègue ; ce qui peut mal tourner n'est donc
pas le calcul, c'est **ce qui entoure la délégation** — et c'est exactement ce
qu'aucun test du moteur ne peut voir, puisqu'il reçoit déjà l'acteur, le projet
et la révision attendue en arguments.

Deux propriétés dominent, et ce sont celles qui protègent une personne :

  * **la cérémonie reste entière.** Le secret de confirmation est émis et
    consommé dans la même transaction et ne revient JAMAIS à l'appelant ; un
    secret qu'un modèle détient n'est plus la confirmation de personne ;
  * **les deux refus de la revue ne peuvent pas être assouplis par la porte.**
    Un item bloquant refuse, chaque avertissement doit être nommé — et la porte
    nomme les mêmes clés que le moteur lira, sinon un modèle acquitterait des
    clés que la revue ne reconnaît pas et se ferait refuser sans savoir quoi
    corriger.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _conn():
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cursor = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cursor)
    cm.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cm)
    return conn


def _tools(monkeypatch, *, identity="alice@example.com"):
    from core import datastream_wizard_mcp as wiz
    from core import mcp_profiles

    # `core.main` EST IMPORTE AVANT LA DOUBLURE, et c'est necessaire : son import
    # enregistre TOUS les outils du produit, et si la doublure etait deja posee
    # ils atterriraient dans `captured`. Le test qui compte les cinq verbes de
    # cette porte se mettrait alors a en compter cent vingt-quatre.
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    captured: dict = {}
    monkeypatch.setattr(
        mcp_profiles,
        "register_profiled",
        lambda mcp, fn, **kw: captured.setdefault(fn.__name__, (fn, kw)),
    )
    monkeypatch.setattr(wiz, "_identity", lambda: identity)
    monkeypatch.setattr(wiz, "refuse_unless_project_scope", MagicMock())
    wiz.register(MagicMock())
    return captured


def _proposal(*, blocked=(), warnings=()):
    items = [{"key": k, "status": "blocked"} for k in blocked]
    items += [{"key": k, "status": "warning"} for k in warnings]
    return {"proposal_ref": "prop_1", "content_hash": "h", "sections": [{"items": items}]}


# ---------------------------------------------------------------------------
# Les cinq verbes existent, et leur rang dit ce qu'ils font
# ---------------------------------------------------------------------------


def test_the_door_has_five_verbs_and_only_one_asks_a_HUMAN(monkeypatch):
    """`human` sur la matérialisation, et c'est la moitié de la cérémonie.

    Le secret ne voyage pas, donc c'est l'hôte qui porte la confirmation devant
    la personne. Un `host` ici matérialiserait un Datastream sans que personne
    ait vu la revue.
    """
    tools = _tools(monkeypatch)
    assert set(tools) == {
        "start_datastream_draft",
        "get_datastream_draft",
        "observe_datastream_draft",
        "compile_datastream_draft",
        "materialize_datastream_draft",
    }
    modes = {name: kw["confirmation_mode"] for name, (_fn, kw) in tools.items()}
    assert modes["materialize_datastream_draft"] == "human"
    assert modes["get_datastream_draft"] == "none"
    assert [n for n, m in modes.items() if m == "human"] == [
        "materialize_datastream_draft"
    ]


def test_the_read_is_a_read_and_the_four_others_are_writes(monkeypatch):
    tools = _tools(monkeypatch)
    effects = {name: kw["effect"] for name, (_fn, kw) in tools.items()}
    assert effects["get_datastream_draft"] == "read"
    assert all(
        effects[n] == "confirmed_write"
        for n in effects
        if n != "get_datastream_draft"
    )


# ---------------------------------------------------------------------------
# Ce que la lecture nomme, et pourquoi les clés doivent être les mêmes
# ---------------------------------------------------------------------------


def test_the_read_names_the_blockers_and_the_warnings_the_ENGINE_will_read(monkeypatch):
    """Nommer autrement ferait acquitter des clés que la revue ne reconnaît pas."""
    from core import datastream_wizard_mcp as wiz

    proposal = _proposal(blocked=["plan.grain"], warnings=["mapping.currency", "schedule.tz"])
    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration.read_draft",
            return_value={"current_proposal_id": "prop_1", "current_revision": 3},
        ),
        patch("core.datastream_preconfiguration.read_proposal", return_value=proposal),
    ):
        answer = _tools(monkeypatch)["get_datastream_draft"][0](
            project_id="proj_t", draft_id="dsd_1"
        )

    assert answer["blocking_items"] == ["plan.grain"]
    assert answer["warning_items"] == ["mapping.currency", "schedule.tz"]
    assert answer["acknowledgements_required"] == answer["warning_items"]
    assert answer["can_materialize"] is False
    # ET C'EST LA MEME LECTURE QUE LE MOTEUR : la fonction est partagee.
    assert wiz._review_items(proposal) == (
        ["plan.grain"],
        ["mapping.currency", "schedule.tz"],
    )


def test_a_proposal_with_no_blocker_says_the_draft_CAN_be_materialized(monkeypatch):
    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration.read_draft",
            return_value={"current_proposal_id": "prop_1"},
        ),
        patch(
            "core.datastream_preconfiguration.read_proposal",
            return_value=_proposal(warnings=["a"]),
        ),
    ):
        answer = _tools(monkeypatch)["get_datastream_draft"][0](
            project_id="proj_t", draft_id="dsd_1"
        )
    assert answer["can_materialize"] is True
    assert answer["acknowledgements_required"] == ["a"]


def test_a_draft_with_no_proposal_yet_cannot_be_materialized(monkeypatch):
    """`can_materialize` sur un brouillon non compilé serait une invitation fausse."""
    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch("core.datastream_preconfiguration.read_draft", return_value={}),
    ):
        answer = _tools(monkeypatch)["get_datastream_draft"][0](
            project_id="proj_t", draft_id="dsd_1"
        )
    assert answer["proposal"] is None
    assert answer["can_materialize"] is False


# ---------------------------------------------------------------------------
# La cérémonie
# ---------------------------------------------------------------------------


def _materialize(monkeypatch, **overrides):
    from core.datastream_activation import prepare_confirmation  # noqa: F401

    seen: dict = {}

    def _freeze(conn, **kwargs):
        seen["freeze"] = kwargs
        return {"final_review_id": "fr_1"}

    def _issue(conn, **kwargs):
        seen["issue"] = kwargs
        return {"confirmation_id": "cnf_1", "confirmation_secret": "s3cr3t"}

    def _execute(conn, **kwargs):
        seen["execute"] = kwargs
        return MagicMock(result={"datastream_id": "ds_new", "candidate_job_id": "job_1"})

    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration_api.freeze_and_persist_final_review",
            side_effect=_freeze,
        ),
        patch(
            "core.datastream_activation.read_final_review",
            return_value={"content_hash": "h", "org_id": "org_t"},
        ),
        patch("core.datastream_activation.prepare_confirmation", side_effect=_issue),
        patch("core.datastream_activation.execute_confirmed_operation", side_effect=_execute),
        patch("core.datastream_preconfiguration_api._dispatch_activation_task"),
    ):
        answer = _tools(monkeypatch)["materialize_datastream_draft"][0](
            project_id="proj_t",
            draft_id="dsd_1",
            preview_ref="prev_1",
            acknowledged_warning_ids=overrides.get("acks", ["a", "b"]),
            idempotency_key="k1",
        )
    return answer, seen


def test_the_confirmation_secret_NEVER_reaches_the_caller(monkeypatch):
    """Un secret qu'un modèle détient n'est plus la confirmation de personne."""
    answer, seen = _materialize(monkeypatch)
    flat = json.dumps(answer)
    assert "s3cr3t" not in flat
    assert "confirmation_secret" not in answer
    assert "confirmation_id" not in answer
    # Il a bien ete emis ET consomme, dans la meme transaction.
    assert seen["issue"]["resource_id"] == "fr_1"
    assert seen["execute"]["confirmation_secret"] == "s3cr3t"


def test_the_review_is_composed_by_the_SHARED_function_and_not_here(monkeypatch):
    """Un second compositeur serait un second produit -- la faute de la journée."""
    _answer, seen = _materialize(monkeypatch, acks=["mapping.currency"])
    assert seen["freeze"]["draft_id"] == "dsd_1"
    assert seen["freeze"]["preview_id"] == "prev_1"
    assert seen["freeze"]["acknowledged_warning_ids"] == ["mapping.currency"]


def test_the_operation_is_bound_to_the_exact_frozen_review(monkeypatch):
    """`resource_id` et `content_hash` viennent de la revue, jamais de l'appelant."""
    _answer, seen = _materialize(monkeypatch)
    assert seen["execute"]["resource_id"] == "fr_1"
    assert seen["execute"]["content_hash"] == "h"
    assert seen["execute"]["org_id"] == "org_t"


def test_it_answers_with_the_datastream_it_created(monkeypatch):
    answer, _seen = _materialize(monkeypatch)
    assert answer["datastream_id"] == "ds_new"
    assert answer["final_review_ref"] == "fr_1"
    assert answer["candidate_job_id"] == "job_1"


# ---------------------------------------------------------------------------
# Les refus que la porte ne peut pas assouplir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "Blocking review items cannot be acknowledged away",
        "Every warning requires an explicit acknowledgement",
        "A current passing preview is required",
    ],
)
def test_the_engine_refusal_travels_with_ITS_OWN_words(monkeypatch, sentence):
    """Deux portes qui renomment le même refus donnent deux vocabulaires."""
    from core.datastream_activation import ActivationValidationError
    from fastmcp.exceptions import ToolError

    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration_api.freeze_and_persist_final_review",
            side_effect=ActivationValidationError(sentence),
        ),
        pytest.raises(ToolError) as exc,
    ):
        _tools(monkeypatch)["materialize_datastream_draft"][0](
            project_id="proj_t",
            draft_id="dsd_1",
            preview_ref="prev_1",
            acknowledged_warning_ids=[],
            idempotency_key="k1",
        )

    body = json.loads(exc.value.args[0])
    assert body["code"] == "review_refused"
    assert body["message"] == sentence


def test_acknowledgements_that_are_not_a_list_are_refused_before_any_connection(monkeypatch):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        _tools(monkeypatch)["materialize_datastream_draft"][0](
            project_id="proj_t",
            draft_id="dsd_1",
            preview_ref="prev_1",
            acknowledged_warning_ids="a,b",
            idempotency_key="k1",
        )
    assert json.loads(exc.value.args[0])["code"] == "invalid_input"


def test_an_absent_draft_refuses_as_not_found_and_not_as_a_crash(monkeypatch):
    from core.datastream_preconfiguration import PreconfigurationNotFound
    from fastmcp.exceptions import ToolError

    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration.read_draft",
            side_effect=PreconfigurationNotFound("Draft not found"),
        ),
        pytest.raises(ToolError) as exc,
    ):
        _tools(monkeypatch)["get_datastream_draft"][0](project_id="proj_t", draft_id="nope")
    assert json.loads(exc.value.args[0])["code"] == "not_found"


# ---------------------------------------------------------------------------
# Ce que les verbes transmettent au moteur
# ---------------------------------------------------------------------------


def test_the_expected_revision_travels_so_a_moved_draft_is_refused(monkeypatch):
    """Observer contre une révision périmée observerait des choix qui ont changé."""
    seen: dict = {}

    def _observe(conn, **kwargs):
        seen.update(kwargs)
        return {"observation_ref": "obs_1"}

    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_setup_observations.create_observation", side_effect=_observe
        ),
    ):
        _tools(monkeypatch)["observe_datastream_draft"][0](
            project_id="proj_t",
            draft_id="dsd_1",
            expected_revision=7,
            idempotency_key="k",
            request={"kind": "connector_pull"},
        )
    assert seen["expected_revision"] == 7
    assert seen["actor"] == "alice@example.com"
    assert seen["request"] == {"kind": "connector_pull"}


def test_the_compile_returns_what_the_final_review_will_read(monkeypatch):
    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration.compile_draft",
            return_value=_proposal(blocked=["x"], warnings=["y"]),
        ),
    ):
        answer = _tools(monkeypatch)["compile_datastream_draft"][0](
            project_id="proj_t", draft_id="dsd_1", expected_revision=2, idempotency_key="k"
        )
    assert answer["blocking_items"] == ["x"]
    assert answer["acknowledgements_required"] == ["y"]


def test_the_draft_is_resumable_and_the_key_travels(monkeypatch):
    """Un seul brouillon par Projet : rappeler l'outil rend celui qui existe."""
    seen: dict = {}

    def _create(conn, **kwargs):
        seen.update(kwargs)
        return {"draft_id": "dsd_1", "idempotent_replay": True}

    with (
        patch("core.db.request_connection", return_value=_conn()),
        patch(
            "core.datastream_preconfiguration.create_or_resume_draft", side_effect=_create
        ),
    ):
        answer = _tools(monkeypatch)["start_datastream_draft"][0](
            project_id="proj_t", idempotency_key="k9"
        )
    assert seen["idempotency_key"] == "k9"
    assert answer["idempotent_replay"] is True
