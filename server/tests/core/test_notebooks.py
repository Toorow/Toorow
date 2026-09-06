"""Tests for save_notebook and run_notebook MCP tools (Story 6.5, AC3, AC4, AC8).

Covers (from AC8):
  - test_save_notebook_inserts_row: mock Postgres; assert app.notebooks row inserted.
  - test_save_notebook_unknown_report_ref: ToolError not_found for unknown report.
  - test_save_notebook_unsupported_window_rule: ToolError for unsupported rule.
  - test_run_notebook_resolves_window_rule: last_30d -> correct date window.
  - test_run_notebook_summary_has_citations: run result summary contains citation tokens.
  - test_run_notebook_envelope_meta_has_notebook_id: envelope has meta.notebook_id.
  - test_run_notebook_stores_pull_ids: pull_ids in notebook_runs match warehouse result.
  - test_run_notebook_not_found: unknown notebook_id -> ToolError not_found.
  - test_run_notebook_as_of: as_of='2026-06-01' -> summary contains as_of hint.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Prevent background workers from starting during import.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(cursor_mock=None, *, fetchone_return=None, fetchall_return=None):
    """Build a mock psycopg connection usable as a context manager."""
    if cursor_mock is None:
        cursor_mock = MagicMock()
    if fetchone_return is not None:
        cursor_mock.fetchone.return_value = fetchone_return
    if fetchall_return is not None:
        cursor_mock.fetchall.return_value = fetchall_return
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


def _make_cursor_with_description(row, columns):
    """Cursor mock whose .fetchone returns row and .description returns column specs."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = row
    cursor_mock.description = [(col,) for col in columns]
    return cursor_mock


# ---------------------------------------------------------------------------
# save_notebook tests
# ---------------------------------------------------------------------------


class TestSaveNotebook:
    """`save_notebook` ecrit le magasin GOUVERNE depuis le 2026-08-22 (story 67.23).

    CE QUE CES TESTS TENAIENT, et pourquoi le contrat a change. Ils prouvaient
    qu'un INSERT partait vers `app.notebooks` avec `(title, report_ref,
    window_rule, narrative_prompt)`. Cette table est le magasin HERITE : les
    ecrans canoniques d'Analyze lisent `app.analysis_notebooks` et ses versions,
    et le seul lecteur de l'ancien est `list_legacy_notebooks`, qui existe pour
    tenir AC12 << remain readable >>. **Un modele creait donc un objet reel,
    audite, et visible par aucun ecran.**

    Le contrat est declare dans `analyze-and-test.md` (amendement 67.23) et
    derive de `_validate_blocks` : `(label, blocks, description?)`. `report_ref`
    devient un `block_type`, `window_rule` une propriete de la version de Query
    Spec que le bloc epingle -- les deux cles plates disaient la meme chose
    moins precisement, et pour un notebook entier au lieu d'un bloc.
    """

    def _blocks(self):
        return [
            {
                "block_key": "spend",
                "block_type": "query",
                "as_of_rule": "current",
                "query_spec_version_id": "qsv_EXAMPLE",
            }
        ]

    def test_it_writes_the_GOVERNED_store_and_not_the_legacy_one(self):
        """La mesure qui compte : quelle fonction est appelee, et laquelle ne l'est plus."""
        from core.main import save_notebook

        conn_mock = _make_mock_conn(_make_cursor_with_description(("org_test",), ["org_id"]))
        created = {"id": "nbkv_1", "notebook_id": "nbk_TEST", "version_number": 1}

        with (
            patch("core.main._resolve_project", side_effect=lambda pid, identity=None: pid),
            patch("core.notebook_mcp.refuse_unless_project_scope"),
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.analyze_artifacts.create_notebook", return_value=created) as governed,
            patch("core.audit.write_audit_row") as mock_audit,
        ):
            result = save_notebook(
                project_id="proj_test",
                label="Mon notebook GSC",
                blocks=self._blocks(),
            )

        assert result.is_error is not True
        governed.assert_called_once()
        assert governed.call_args.kwargs["label"] == "Mon notebook GSC"
        assert governed.call_args.kwargs["org_id"] == "org_test"
        assert governed.call_args.kwargs["blocks"] == self._blocks()

        # L'accusé nomme CE QUI a ete ecrit et OU le retrouver -- AI-103 : ce
        # n'est pas la langue qui est tenue ici, c'est le titre et l'identifiant.
        text = result.content[0].text
        assert "Mon notebook GSC" in text
        assert "nbk_TEST" in text

        mock_audit.assert_called_once()
        assert mock_audit.call_args[1]["action"] == "notebook_created"
        assert mock_audit.call_args[1]["metadata"]["notebook_id"] == "nbk_TEST"

    def test_the_legacy_table_is_never_named_by_this_module_again(self):
        """La garde de la bascule : une seule mesure, sur la source entiere."""
        import inspect

        from core import notebook_mcp

        source = inspect.getsource(notebook_mcp)
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        for relation in ("app.notebooks", "app.notebook_runs"):
            assert f"FROM {relation}" not in code and f"INTO {relation}" not in code, (
                f"le module touche encore {relation}"
            )

    def test_every_refusal_of_a_block_travels_and_not_just_the_first(self):
        """`_validate_blocks` accumule ses refus ; les jeter en garderait un sur N.

        Un modele qui compose cent blocs doit apprendre ses cent erreurs en un
        tour. C'est la propriete la plus utile du contrat gouverne pour cette
        porte-ci, et rien d'autre ne la tient.
        """
        from core.analyze_artifacts import ArtifactRefused, Refusal
        from core.main import save_notebook
        from fastmcp.exceptions import ToolError

        conn_mock = _make_mock_conn(_make_cursor_with_description(("org_test",), ["org_id"]))
        refused = ArtifactRefused(
            "invalid_blocks",
            "three blocks are wrong",
            [
                Refusal("invalid_block_type", "`chart` is not a block type", "blocks[0]"),
                Refusal("missing_field", "a query block pins a Query Spec version", "blocks[2]"),
                Refusal("duplicate_block_key", "`spend` appears twice", "blocks[3]"),
            ],
        )

        with (
            patch("core.main._resolve_project", side_effect=lambda pid, identity=None: pid),
            patch("core.notebook_mcp.refuse_unless_project_scope"),
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.analyze_artifacts.create_notebook", side_effect=refused),
            pytest.raises(ToolError) as exc,
        ):
            save_notebook(project_id="proj_test", label="x", blocks=self._blocks())

        body = json.loads(exc.value.args[0])
        assert body["code"] == "invalid_blocks"
        assert [r["subject"] for r in body["refusals"]] == [
            "blocks[0]",
            "blocks[2]",
            "blocks[3]",
        ]

    def test_blocks_that_are_not_a_list_are_refused_before_any_connection(self):
        """La portee d'abord, la forme ensuite, et AUCUNE connexion.

        L'ordre n'est pas un detail : prouver la portee avant de regarder la
        charge est ce qui empeche un appelant d'apprendre quoi que ce soit sur
        le projet du voisin en variant sa charge. Aucune doublure de connexion
        n'est posee -- si la porte en ouvrait une, ce test tomberait.
        """
        from core.main import save_notebook
        from fastmcp.exceptions import ToolError

        with (
            patch("core.main._resolve_project", side_effect=lambda pid, identity=None: pid),
            patch("core.notebook_mcp.refuse_unless_project_scope"),
            pytest.raises(ToolError) as exc,
        ):
            save_notebook(project_id="proj_test", label="x", blocks="not a list")
        assert json.loads(exc.value.args[0])["code"] == "invalid_input"


class TestRunNotebook:
    """`run_notebook` execute un Notebook GOUVERNE, sur sa version exacte.

    Il lisait `app.notebooks`, resolvait une `window_rule` plate, rendait un
    report et inserait son propre run dans `app.notebook_runs` -- un SECOND
    moteur d'execution, sur le magasin que rien ne montre. Le service gouverne
    epingle la version AVANT qu'un bloc s'execute et rend un Run idempotent :
    aucune de ces deux garanties n'existait ici.
    """

    def test_an_unknown_notebook_refuses_exactly_like_one_that_is_not_yours(self):
        """Le refus ne dit pas qu'un objet existe -- c'est la seule facon honnete."""
        from core.main import run_notebook
        from fastmcp.exceptions import ToolError

        conn_mock = _make_mock_conn(_make_cursor_with_description(None, []))
        with (
            patch("core.db.get_connection", return_value=conn_mock),
            pytest.raises(ToolError) as exc,
        ):
            run_notebook(notebook_id="nbk_DOESNOTEXIST")

        assert json.loads(exc.value.args[0])["code"] == "not_found"

    def test_the_scope_is_READ_from_the_notebook_and_never_received(self):
        """Recevoir le projet en argument laisserait resoudre l'acces ailleurs.

        L'outil ne prend que `notebook_id` ; l'organisation et le projet viennent
        de la ligne. Ce test tient les deux moities : le scope resolu est celui de
        la LIGNE, et c'est lui qui est passe a la garde de portee.
        """
        from core.main import run_notebook

        conn_mock = _make_mock_conn(
            _make_cursor_with_description(("org_beta", "proj_beta"), ["org_id", "project_id"])
        )
        answer = {
            "run_id": "nbkrun_1",
            "notebook_version_id": "nbkv_1",
            "state": "accepted",
            "blocks": [{"block_key": "spend"}],
        }
        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.notebook_mcp.refuse_unless_project_scope") as guard,
            patch("core.analyze_artifacts.run_notebook", return_value=answer) as governed,
            patch("core.audit.write_audit_row"),
        ):
            result = run_notebook(notebook_id="nbk_TEST")

        guard.assert_called_once()
        assert guard.call_args[0][0] == "proj_beta"
        assert guard.call_args.kwargs["minimum_capability"] == "edit"
        assert governed.call_args.kwargs["org_id"] == "org_beta"
        assert governed.call_args.kwargs["project_id"] == "proj_beta"
        assert "nbkrun_1" in result.content[0].text

    def test_the_run_carries_an_idempotency_key_because_the_service_demands_one(self):
        """Sans clef, `analyze_artifacts.run_notebook` refuse -- et il a raison :
        un rejeu sans clef duplique un Run."""
        from core.main import run_notebook

        conn_mock = _make_mock_conn(
            _make_cursor_with_description(("org_t", "proj_t"), ["org_id", "project_id"])
        )
        answer = {"run_id": "r", "notebook_version_id": "v", "state": "accepted", "blocks": []}
        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.notebook_mcp.refuse_unless_project_scope"),
            patch("core.analyze_artifacts.run_notebook", return_value=answer) as governed,
            patch("core.audit.write_audit_row"),
        ):
            run_notebook(notebook_id="nbk_TEST", as_of="2026-08-01")

        key = governed.call_args.kwargs["idempotency_key"]
        assert key, "aucune clef d'idempotence n'est composee"
        assert "nbk_TEST" in key and "2026-08-01" in key
        assert governed.call_args.kwargs["requested_as_of"] == "2026-08-01"
        assert governed.call_args.kwargs["dispatch_source"] == "manual"

    def test_an_as_of_that_is_blank_asks_for_CURRENT_and_not_for_an_empty_date(self):
        from core.main import run_notebook

        conn_mock = _make_mock_conn(
            _make_cursor_with_description(("org_t", "proj_t"), ["org_id", "project_id"])
        )
        answer = {"run_id": "r", "notebook_version_id": "v", "state": "accepted", "blocks": []}
        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.notebook_mcp.refuse_unless_project_scope"),
            patch("core.analyze_artifacts.run_notebook", return_value=answer) as governed,
            patch("core.audit.write_audit_row"),
        ):
            run_notebook(notebook_id="nbk_TEST", as_of="   ")

        assert governed.call_args.kwargs["requested_as_of"] is None

    def test_the_scheduler_entry_point_uses_the_same_service(self):
        """`run_notebook_direct` portait le second moteur ; il n'appelle plus que celui-ci."""
        from core.main import run_notebook_direct

        conn_mock = _make_mock_conn(
            _make_cursor_with_description(("org_t", "proj_t"), ["org_id", "project_id"])
        )
        answer = {"run_id": "r", "notebook_version_id": "v", "state": "accepted", "blocks": []}
        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.analyze_artifacts.run_notebook", return_value=answer) as governed,
        ):
            out = run_notebook_direct(notebook_id="nbk_TEST")

        assert out["run_id"] == "r"
        assert governed.call_args.kwargs["dispatch_source"] == "scheduled"
        assert governed.call_args.kwargs["actor"] == "scheduler"
