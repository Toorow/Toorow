"""Stories 38.16 / 38.17: the entry into mapping repair, from a failed file.

The engine is not tested here -- it is `datastream_change`, it exists, and it is
governed. What is tested is the ENTRY that did not exist: from "this file
failed", can an operator see what the file contained and what it is mapped
against, without any write happening on the way?

Covers:
  (a) Samples are MASKED. One policy, both channels, so the safe one is not the
      one somebody forgets.
  (b) The context names the pinned versions and points at the governed path
      instead of exposing a write.
  (c) Reading a context WRITES NOTHING -- it is a screen opened because
      something already went wrong.
  (d) Every unavailability is a stable reason, never an exception.
  (e) A .sav's variable labels arrive as evidence, not as bindings.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

_CSV = b"date,clicks,respondent_email\n2026-01-15,10,alice@example.com\n"


def _store(tmp_path):
    from core.inbound_quarantine import LocalFsQuarantineStore

    return LocalFsQuarantineStore(root=str(tmp_path))


def _stored(store, data=_CSV, filename="report.csv"):
    return store.put(
        partition="p",
        message_id="m",
        filename=filename,
        data=data,
        content_type="text/csv",
    ).uri


def _conn(*, lifecycle="active", project_id="proj-1"):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("plan_v1", "map_v1", lifecycle, project_id)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _raw(**overrides):
    row = {
        "raw_import_id": "inbraw_1",
        "datastream_id": "ds-1",
        "state": "REJECTED",
        "error_code": "declared_type_mismatch",
        "filename": "report.csv",
        "media_type_detected": "text/csv",
        "content_hash": "a" * 64,
        "scan_verdict": {"accepted": False},
        "quarantine_uri": "file:///nowhere",
    }
    row.update(overrides)
    return row


def _patch_raw(monkeypatch, row):
    import core.inbound_raw_imports as iri

    monkeypatch.setattr(iri, "get_raw_import", lambda conn, **kw: row)


# ---------------------------------------------------------------------------
# (a) Masking.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_value",
    ["alice@example.com", "2026-01-15", "a very long free text answer indeed"],
)
def test_a_sample_never_survives_readable(raw_value):
    from core.inbound_mapping_entry import mask_sample

    masked = mask_sample(raw_value)
    assert raw_value not in masked
    # The shape survives: enough to recognise a column, not to read it.
    assert masked.startswith(raw_value[:2])
    assert masked.endswith(f"({len(raw_value)})")


def test_masking_keeps_none_distinguishable_from_empty():
    """"This column is empty here" is itself the diagnosis half the time."""
    from core.inbound_mapping_entry import mask_sample

    assert mask_sample(None) is None
    assert mask_sample("") == ""


def test_no_raw_cell_reaches_the_context(monkeypatch, tmp_path):
    """The assertion that matters: a real email address in a real file.

    38.10 AC5 forbids raw rows over MCP and 38.16 AC5 allows only minimal
    disclosure to a human. Two masking policies would mean the safe one is the
    one somebody forgets, so there is one -- and this proves it holds on the
    path an operator actually opens.
    """
    from core.inbound_mapping_entry import get_mapping_repair_context

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri))

    context = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    blob = str(context)
    assert "alice@example.com" not in blob
    assert "2026-01-15" not in blob
    # ... and the columns ARE there, by name.
    names = [c["name"] for c in context["source_columns"]]
    assert names == ["date", "clicks", "respondent_email"]


# ---------------------------------------------------------------------------
# (b) (c) What the context is for.
# ---------------------------------------------------------------------------


def test_the_context_names_the_pinned_versions_and_the_governed_path(
    monkeypatch, tmp_path
):
    from core.inbound_mapping_entry import get_mapping_repair_context

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri))

    context = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    assert context["available"] is True
    assert context["pinned_versions"] == {
        "plan_version_id": "plan_v1",
        "mapping_version_id": "map_v1",
    }
    # It POINTS at the engine; it does not become one.
    path = context["governed_path"]
    assert path["engine"] == "datastream_change"
    # CONTRE LE VRAI ROUTEUR, PAS CONTRE UNE SOUS-CHAINE. L'assertion precedente
    # cherchait "workbench/mapping/changes" dans l'URL emise : elle restait verte
    # alors que les DEUX adresses rendaient 404, parce qu'il leur manquait le
    # prefixe `/api/projects/{project_id}` et que le confirm ne vit pas sous
    # `mapping/`. Un renommage de route laissait la reference verte et morte.
    from core.datastream_workbench_api import datastream_workbench_routes

    templates = {route.path for route in datastream_workbench_routes}
    # C-1 : l'etape de PUBLICATION est nommee, et elle vise l'autre moteur.
    # Appendre une version ne l'active pas -- le depot l'ecrit dans son propre
    # test de gouvernance -- donc pointer `datastream_change` seul laissait l'AC
    # << publier change la version courante >> sans aucun endroit ou se produire.
    assert path["publish_engine"] == "governed_publication"
    assert "/api/governance/publication-reviews" in path["publish"]

    for key in ("prepare", "confirm"):
        emitted = path[key].split(" ", 1)[1]
        # Reconstruit depuis les identifiants REELS du contexte, jamais depuis
        # des valeurs devinees : une doublure qui change ses ids ferait passer
        # la garde a cote sans rien dire.
        rebuilt = emitted.replace(
            str(context["project_id"]), "{project_id}"
        ).replace(str(context["datastream_id"]), "{datastream_id}")
        assert rebuilt in templates, f"{key}: {rebuilt!r} n'est aucune route montee"
    # And it restates the failure it exists to repair.
    assert context["raw_import"]["error_code"] == "declared_type_mismatch"


def test_reading_a_repair_context_writes_nothing(monkeypatch, tmp_path):
    """A screen opened because something failed must not be a trap."""
    from core.inbound_mapping_entry import get_mapping_repair_context

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri))
    conn = _conn()

    get_mapping_repair_context(
        conn, raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    conn.commit.assert_not_called()
    executed = [
        str(call) for call in conn.cursor.return_value.execute.call_args_list
    ]
    for statement in executed:
        upper = statement.upper()
        assert "INSERT" not in upper and "UPDATE" not in upper
        assert "DELETE" not in upper


# ---------------------------------------------------------------------------
# (d) Unavailability is a reason, never an exception.
# ---------------------------------------------------------------------------


def test_an_absent_raw_import_is_a_reason(monkeypatch):
    from core.inbound_mapping_entry import (
        CONTEXT_NOT_FOUND,
        get_mapping_repair_context,
    )

    _patch_raw(monkeypatch, None)
    out = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_x", datastream_id="ds-1"
    )
    assert out["available"] is False
    assert out["reason"] == CONTEXT_NOT_FOUND


def test_a_non_active_datastream_is_refused_before_the_operator_composes(
    monkeypatch, tmp_path
):
    """`prepare_change` requires an Active Datastream with exact pointers.

    Saying so here beats letting someone assemble a repair the next call will
    refuse for a reason they cannot see from this screen.
    """
    from core.inbound_mapping_entry import (
        CONTEXT_DATASTREAM_NOT_ACTIVE,
        get_mapping_repair_context,
    )

    store = _store(tmp_path)
    _patch_raw(monkeypatch, _raw(quarantine_uri=_stored(store)))

    out = get_mapping_repair_context(
        _conn(lifecycle="draft"),
        raw_import_id="inbraw_1",
        datastream_id="ds-1",
        store=store,
    )
    assert out["available"] is False
    assert out["reason"] == CONTEXT_DATASTREAM_NOT_ACTIVE
    # The pinned versions are still reported: the screen can explain itself.
    assert out["pinned_versions"]["mapping_version_id"] == "map_v1"


def test_unreadable_bytes_are_a_reason_not_a_raise(monkeypatch, tmp_path):
    from core.inbound_mapping_entry import (
        CONTEXT_NO_BYTES,
        get_mapping_repair_context,
    )

    store = _store(tmp_path)
    _patch_raw(monkeypatch, _raw(quarantine_uri="file:///gone/missing.csv"))

    out = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out["reason"] == CONTEXT_NO_BYTES


def test_an_unparseable_file_is_the_diagnosis_not_an_error(monkeypatch, tmp_path):
    """The file already failed once; this read must not fail a second time."""
    from core.inbound_mapping_entry import (
        CONTEXT_UNPARSEABLE,
        get_mapping_repair_context,
    )

    store = _store(tmp_path)
    uri = _stored(store, data=b"\x00\x01\x02 binary junk", filename="junk.bin")
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri, filename="junk.bin"))

    out = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )
    assert out["available"] is False
    # UN REFUS NOMME VAUT MIEUX QUE LE GENERIQUE, et le code en rend un depuis que
    # le parseur propage `exc.code` (`inbound_mapping_entry.py:232`). Un `.bin`
    # n'est pas << illisible >>, il est d'un type non pris en charge -- et c'est
    # ce que la personne doit lire pour savoir quoi renvoyer. Le generique reste
    # accepte : il couvre l'echec de parsing qui ne porte aucun code.
    assert out["reason"] in {CONTEXT_UNPARSEABLE, "unsupported_file_type"}


# ---------------------------------------------------------------------------
# (e) Survey labels arrive as evidence.
# ---------------------------------------------------------------------------


def test_a_sav_variable_label_arrives_as_evidence_not_a_binding(
    monkeypatch, tmp_path
):
    """Same rule as 38.12 decision 1, seen from the repair screen.

    The label is offered to the human choosing a canonical field; it never
    becomes the column name, or a reworded question would silently re-key a
    published dataset.
    """
    pyreadstat = pytest.importorskip("pyreadstat")
    pd = pytest.importorskip("pandas")

    from core.inbound_mapping_entry import get_mapping_repair_context

    path = tmp_path / "survey.sav"
    pyreadstat.write_sav(
        pd.DataFrame({"q1": [1.0, 2.0]}),
        str(path),
        column_labels=["Q1. Overall satisfaction"],
    )
    data = path.read_bytes()

    store = _store(tmp_path / "q")
    uri = _stored(store, data=data, filename="survey.sav")
    _patch_raw(
        monkeypatch, _raw(quarantine_uri=uri, filename="survey.sav", state="FAILED")
    )

    context = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    column = context["source_columns"][0]
    assert column["name"] == "q1"
    assert column["source_label"] == "Q1. Overall satisfaction"


# ---------------------------------------------------------------------------
# AC3 -- the seven readings, ON THE INBOUND PATH.
#
# THE RATIFIED LINE. `docs/product-architecture/file-source-ingestion.md:203`
# lists as a condition leaving this surface incomplete:
#
#     "the same file yields different results by upload and by email"
#
# That was the state on 2026-08-10. The seven readings AC3 asks for were
# delivered on `build_file_source_preview`, whose only two callers are the
# UPLOAD path (`server/core/file_import_api.py#_preview_csv_excel_import`,
# `file_source_template_api.py:490`) -- and
# the second REQUIRES `file_base64`, so an operator repairing an emailed file
# was asked to re-upload bytes the platform already retained, while 38.18 exists
# to "recover without provider resend".
#
#     grep -c '"coercions"\|"row_validation"\|"units"\|"classification"\|"gate"' \
#         server/core/inbound_mapping_entry.py
#     -> 0
# ---------------------------------------------------------------------------


_FULL_PREVIEW = {
    "fields": [{"source": "date", "target": "day", "status": "recognized"}],
    "placement": {"class": "daily_metrics"},
    "gate": {"passed": False, "missing_required": ["spend"]},
    "drift": None,
    "ambiguities": [],
    "vocabularies": [],
    "coercions": [{"column": "date", "from": "text", "to": "date"}],
    "row_validation": {"rows": 10, "rejected": 2},
    "units": [{"column": "spend", "currency": "EUR"}],
    "classification": {"sensitive_columns": ["respondent_email"]},
    "blocked": False,
    "reason": None,
}


def test_the_inbound_path_reports_all_seven_readings_ac3_asks_for(
    monkeypatch, tmp_path
):
    """The parity closure, measured reading by reading.

    Every one of the seven must arrive from the RETAINED bytes -- no re-upload,
    no `file_base64`, no second door.
    """
    import core.csv_excel_import as cei
    from core.inbound_mapping_entry import PREVIEW_READINGS, get_mapping_repair_context

    seen: dict = {}

    def _composer(conn, data, **kwargs):
        seen["data"] = data
        seen["kwargs"] = kwargs
        return dict(_FULL_PREVIEW)

    monkeypatch.setattr(cei, "build_file_source_preview", _composer)

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri))

    context = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    preview = context["preview"]
    assert preview["available"] is True, preview
    for reading in PREVIEW_READINGS:
        assert reading in preview, (
            f"AC3 names {reading!r} and the inbound path does not report it"
        )
        assert preview[reading] == _FULL_PREVIEW[reading]

    # THE RETAINED BYTES, not a re-upload. This is the whole point: the operator
    # never resends the file.
    assert seen["data"] == _CSV
    assert seen["kwargs"]["datastream_id"] == "ds-1"
    assert seen["kwargs"]["mapping_version_id"] == "map_v1"


def test_the_inbound_preview_calls_the_same_composer_as_the_upload_path():
    """One composer, or the ratified line reopens the moment someone edits one.

    A second implementation on this side would produce exactly what
    `file-source-ingestion.md:203` forbids -- the same file yielding different
    results by upload and by email -- while looking like the repair.
    """
    import ast
    import pathlib

    core = pathlib.Path(__file__).resolve().parents[2] / "core"
    callers = {
        path.name
        for path in core.glob("*.py")
        if "build_file_source_preview(" in path.read_text(encoding="utf-8")
    }
    assert "inbound_mapping_entry.py" in callers, (
        "the inbound path no longer calls the shared preview composer"
    )
    # It is DEFINED once, and the definition is not in this module.
    definitions = [
        path.name
        for path in core.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "build_file_source_preview"
    ]
    assert definitions == ["import_preview.py"], (
        f"`build_file_source_preview` is defined in {definitions} -- a second "
        "copy is the defect `file-source-ingestion.md:203` names"
    )


def test_an_absent_template_is_a_reason_and_never_an_empty_preview(
    monkeypatch, tmp_path
):
    """A Datastream with no `fst_` binding has no verdict to report.

    Reporting `{}` or `gate: {"passed": true}` here would invent a verdict for a
    contract nobody declared -- and an operator would read "nothing wrong".
    """
    import core.csv_excel_import as cei
    from core.inbound_mapping_entry import (
        PREVIEW_NO_TEMPLATE,
        get_mapping_repair_context,
    )

    monkeypatch.setattr(cei, "build_file_source_preview", lambda conn, data, **kw: None)

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri))

    context = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    preview = context["preview"]
    assert preview["available"] is False
    assert preview["reason"] == PREVIEW_NO_TEMPLATE
    assert "gate" not in preview, (
        "an absent template must not carry a landing verdict -- there is none"
    )


def test_a_failing_preview_never_breaks_the_repair_screen(monkeypatch, tmp_path):
    """This screen is opened BECAUSE something failed. It must not fail again."""
    import core.csv_excel_import as cei
    from core.inbound_mapping_entry import PREVIEW_UNAVAILABLE, get_mapping_repair_context

    def _explode(conn, data, **kwargs):
        raise RuntimeError("the template registry is unreachable")

    monkeypatch.setattr(cei, "build_file_source_preview", _explode)

    store = _store(tmp_path)
    uri = _stored(store)
    _patch_raw(monkeypatch, _raw(quarantine_uri=uri))

    context = get_mapping_repair_context(
        _conn(), raw_import_id="inbraw_1", datastream_id="ds-1", store=store
    )

    # The context is still SERVED: the columns and the pinned versions are what
    # the operator came for, and losing them to a preview outage would be the
    # screen failing for the reason it exists.
    assert context["available"] is True
    assert context["source_columns"]
    assert context["preview"] == {"available": False, "reason": PREVIEW_UNAVAILABLE}
