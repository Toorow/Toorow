"""AI-113 -- the delivered file becomes a candidate, so "validate" is possible.

The journey was: address, draft, first file, SEE THE SHAPE, validate. The shape
arrived in `inbound_discovery`; this is the last step. Before it,
`managed_feed_candidate` honoured only `file_upload` and routed the delivered
channels to a boundary whose own docstring says "NOT CONFIGURED ANYWHERE TODAY"
-- so an operator could see the shape and had nothing to approve.

The deferral had a real reason, quoted in that driver: "each needs its input
resolved differently, and guessing one would materialize bytes nobody reviewed".
These tests pin that the reason is ANSWERED rather than waived:

  (a) the input is resolved by the CONTENT HASH the plan pinned;
  (b) "the latest delivery" is never consulted -- a file that arrives while
      someone is reading the review cannot become the candidate they approve;
  (c) a retained object that no longer matches its hash is REFUSED;
  (d) an expired or unreadable object is refused with a named reason, not a
      silent fallback;
  (e) the channel travels into provenance, so a delivered file and an uploaded
      one do not become indistinguishable once landed;
  (f) Google Sheets still routes to the remote boundary -- this did not quietly
      claim a channel it cannot serve.
"""

from __future__ import annotations

import hashlib

import pytest

_CSV = b"date,clicks\n2026-01-15,10\n2026-01-16,12\n"
_HASH = hashlib.sha256(_CSV).hexdigest()
_OTHER = b"date,clicks\n2026-02-01,99\n"
_OTHER_HASH = hashlib.sha256(_OTHER).hexdigest()


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(list(rows))

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def commit(self):
        return None


class _Store:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, uri):
        if uri not in self.mapping:
            raise RuntimeError("object not found")
        return self.mapping[uri]


def _patch_resolution(monkeypatch, *, rows, store):
    """Wire the two seams `_received_file` reaches: the DB and the store."""
    import core.db as core_db
    import core.inbound_quarantine as quarantine

    monkeypatch.setattr(core_db, "get_connection", lambda *a, **kw: _Conn(rows))
    monkeypatch.setattr(quarantine, "open_quarantine_store", lambda *a, **kw: store)


def _context(**overrides):
    base = {
        "channel": "inbound_email",
        "datastream_id": "ds-1",
        "project_id": "proj-1",
        "draft_id": "draft-1",
        "execution_id": "exec-1",
        "plan_version_id": "plan-1",
        "mapping_version_id": "map-1",
        "plan_intent": {"source": {"managed_feed": {"source_ref": _HASH}}},
        "observation": {"safe_metadata": {"content_hash": _HASH}},
        "projection_plan": {},
        "import_contract": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# (a) (b) Resolution is by pinned hash, never by recency.
# ---------------------------------------------------------------------------


def test_the_candidate_resolves_the_file_its_plan_pinned(monkeypatch):
    from inbound.adapters import datastream_activation_drivers as drivers

    _patch_resolution(
        monkeypatch,
        rows=[("inbraw_1", "report.csv", "file:///q/a")],
        store=_Store({"file:///q/a": _CSV}),
    )
    raw_import_id, filename, payload = drivers._received_file("ds-1", _HASH)

    assert filename == "report.csv"
    assert payload == _CSV
    # The delivery's own id travels with its bytes. Dropping it is why a
    # materialized delivery stayed ACCEPTED with no `import_ledger_id`: nothing
    # downstream could name the row to close.
    assert raw_import_id == "inbraw_1"


def test_the_lookup_is_keyed_on_the_hash_and_ordered_oldest_first(monkeypatch):
    """Never "the latest".

    A second file delivered while someone reads the review must not become the
    candidate they thought they were approving. The query says so: it filters on
    the content hash and orders ASC, so it cannot drift to a newer arrival.
    """
    from inbound.adapters import datastream_activation_drivers as drivers

    conn = _Conn([("inbraw_1", "report.csv", "file:///q/a")])
    import core.db as core_db
    import core.inbound_quarantine as quarantine

    monkeypatch.setattr(core_db, "get_connection", lambda *a, **kw: conn)
    monkeypatch.setattr(
        quarantine, "open_quarantine_store", lambda *a, **kw: _Store({"file:///q/a": _CSV})
    )

    drivers._received_file("ds-1", _HASH)

    sql, params = conn.cursor_obj.queries[0]
    assert "content_hash = %s" in sql
    assert "ORDER BY created_at ASC" in sql
    assert "DESC" not in sql
    assert params == ("ds-1", _HASH)


def test_a_missing_pin_is_refused_rather_than_defaulted(monkeypatch):
    from core.datastream_activation import ActivationValidationError
    from inbound.adapters import datastream_activation_drivers as drivers

    with pytest.raises(ActivationValidationError, match="content hash"):
        drivers._received_file("ds-1", "")


# ---------------------------------------------------------------------------
# (c) (d) Refusals are named.
# ---------------------------------------------------------------------------


def test_an_altered_retained_object_is_refused(monkeypatch):
    """The bytes read do not hash to the record.

    Materializing them would put a candidate under an audit trail naming a
    different file -- the one refusal here where proceeding is worse than
    failing.
    """
    from core.datastream_activation import ActivationValidationError
    from inbound.adapters import datastream_activation_drivers as drivers

    _patch_resolution(
        monkeypatch,
        rows=[("inbraw_1", "report.csv", "file:///q/a")],
        # The store returns DIFFERENT bytes than the hash describes.
        store=_Store({"file:///q/a": _OTHER}),
    )
    with pytest.raises(ActivationValidationError, match="content hash"):
        drivers._received_file("ds-1", _HASH)


def test_a_no_longer_retained_file_names_the_only_honest_recovery(monkeypatch):
    from core.datastream_activation import ActivationValidationError
    from inbound.adapters import datastream_activation_drivers as drivers

    _patch_resolution(monkeypatch, rows=[None], store=_Store({}))
    with pytest.raises(ActivationValidationError, match="no longer retained"):
        drivers._received_file("ds-1", _HASH)


def test_an_unreadable_object_is_a_storage_fault_not_a_missing_pin(monkeypatch):
    from core.datastream_activation import ActivationValidationError
    from inbound.adapters import datastream_activation_drivers as drivers

    _patch_resolution(
        monkeypatch,
        rows=[("inbraw_1", "report.csv", "file:///q/gone")],
        store=_Store({}),
    )
    with pytest.raises(ActivationValidationError, match="could not be read"):
        drivers._received_file("ds-1", _HASH)


# ---------------------------------------------------------------------------
# The pin, and where it comes from.
# ---------------------------------------------------------------------------


def test_the_plan_outranks_the_observation():
    """Once a plan version exists, it IS the pin -- that is what pinned means."""
    from inbound.adapters import datastream_activation_drivers as drivers

    context = _context(
        plan_intent={"source": {"managed_feed": {"source_ref": _HASH}}},
        observation={"safe_metadata": {"content_hash": _OTHER_HASH}},
    )
    assert drivers._received_content_hash(context) == _HASH


def test_the_observation_is_the_fallback_before_any_plan_exists():
    """The preview runs before a plan version: the observed file is the only one."""
    from inbound.adapters import datastream_activation_drivers as drivers

    context = _context(plan_intent={}, observation={"safe_metadata": {"content_hash": _HASH}})
    assert drivers._received_content_hash(context) == _HASH


# ---------------------------------------------------------------------------
# (e) (f) Dispatch, and what was NOT claimed.
# ---------------------------------------------------------------------------


def test_the_preview_serves_a_delivered_file(monkeypatch):
    from inbound.adapters import datastream_activation_drivers as drivers

    _patch_resolution(
        monkeypatch,
        rows=[("inbraw_1", "report.csv", "file:///q/a")],
        store=_Store({"file:///q/a": _CSV}),
    )
    result = drivers.managed_feed_preview_driver(_context())

    assert result["adapter_verified"] is True
    assert result["placeholder"] is False
    assert [field["name"] for field in result["schema"]] == ["date", "clicks"]
    # Bounded evidence, not the dataset -- and in the SAME bucket vocabulary
    # the staged-upload path answers, because the whole claim is that a review
    # screen cannot tell which channel produced a shape.
    assert result["row_count_bucket"] == "1-99"


def test_google_sheets_still_routes_to_the_remote_boundary(monkeypatch):
    """This change did not quietly claim a channel it cannot serve.

    A sheet's input is a live range read through a provider, not bytes this
    deployment holds -- there is nothing local to preview, and pretending
    otherwise is how a driver returns a shape nobody delivered.
    """
    from inbound.adapters import datastream_activation_drivers as drivers

    called: dict = {}
    monkeypatch.setattr(
        drivers,
        "_remote_activation",
        lambda kind, mode, context: called.setdefault("kind", kind) or {"remote": True},
    )
    drivers.managed_feed_preview_driver(_context(channel="google_sheets"))
    assert called["kind"] == "setup_preview"


def test_an_unknown_channel_still_routes_remote(monkeypatch):
    from inbound.adapters import datastream_activation_drivers as drivers

    called: dict = {}
    monkeypatch.setattr(
        drivers,
        "_remote_activation",
        lambda kind, mode, context: called.setdefault("kind", kind) or {"remote": True},
    )
    drivers.managed_feed_candidate(_context(channel="sftp"))
    assert called["kind"] == "candidate_materialization"


def test_the_candidate_carries_the_delivery_channel_into_provenance(monkeypatch):
    """A delivered file and an uploaded one must not become indistinguishable."""
    from inbound.adapters import datastream_activation_drivers as drivers

    captured: dict = {}

    import core.csv_excel_import as cei
    import core.db as core_db
    import core.inbound_quarantine as quarantine
    import core.raw_landing as raw_landing

    monkeypatch.setattr(
        core_db,
        "get_connection",
        lambda *a, **kw: _Conn([("inbraw_1", "report.csv", "file:///q/a")]),
    )
    monkeypatch.setattr(
        quarantine, "open_quarantine_store", lambda *a, **kw: _Store({"file:///q/a": _CSV})
    )
    monkeypatch.setattr(cei, "resolve_file_source_producer", lambda *a, **kw: None)

    class _NoIsolation:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(raw_landing, "candidate_execution", lambda *a, **kw: _NoIsolation())

    def _run_import(payload, **kwargs):
        captured.update(kwargs)
        captured["payload"] = payload
        return {"landed_row_count": 2, "rejected_count": 0}

    monkeypatch.setattr(cei, "run_import", _run_import)

    result = drivers.managed_feed_candidate(_context())

    assert captured["source_metadata"]["channel"] == "inbound_email"
    assert captured["payload"] == _CSV
    # The adapter identity says WHERE the bytes came from; the payload is the
    # same shape as an upload's, and that is the point.
    assert result["adapter_ref"] == "managed_feed.received_file.candidate.isolated.v1"
    assert result["row_count"] == 2
    assert result["isolation"] == {"kind": "relation_per_execution", "published": False}


def test_a_materialized_delivery_is_closed_with_the_ledger_row_it_produced(monkeypatch):
    """The other half of the arrival, which nothing used to write.

    A delivery whose Datastream still needs setup is left ACCEPTED by the ingest
    path -- correctly. The review then happens, the candidate materializes THOSE
    bytes and mints an import-ledger row, and nothing went back to say so: the
    delivery stayed ACCEPTED for ever with a NULL `import_ledger_id`, while the
    rows it produced were already in the warehouse.

    Measured on the STATE and the ID written, not on a call having happened: a
    transition that named the wrong ledger, or none, would pass a call check.
    """
    import core.csv_excel_import as cei
    import core.db as core_db
    import core.inbound_quarantine as quarantine
    import core.inbound_raw_imports as raw_imports
    import core.raw_landing as raw_landing
    from inbound.adapters import datastream_activation_drivers as drivers

    monkeypatch.setattr(
        core_db, "get_connection",
        lambda *a, **kw: _Conn([("inbraw_1", "report.csv", "file:///q/a")]),
    )
    monkeypatch.setattr(
        quarantine, "open_quarantine_store", lambda *a, **kw: _Store({"file:///q/a": _CSV})
    )
    monkeypatch.setattr(cei, "resolve_file_source_producer", lambda *a, **kw: None)

    class _NoIsolation:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(raw_landing, "candidate_execution", lambda *a, **kw: _NoIsolation())
    monkeypatch.setattr(
        cei, "run_import",
        lambda payload, **kw: {
            "landed_row_count": 2,
            "rejected_count": 0,
            "ledger": {"id": "mfl_1"},
        },
    )
    marked: list[dict] = []
    monkeypatch.setattr(
        raw_imports, "mark_raw_import_state", lambda conn, **kw: marked.append(kw)
    )

    drivers.managed_feed_candidate(_context())

    assert len(marked) == 1
    assert marked[0]["raw_import_id"] == "inbraw_1"
    assert marked[0]["state"] == "LANDED"
    assert marked[0]["import_ledger_id"] == "mfl_1"


def test_an_import_that_produced_no_ledger_row_lands_no_delivery(monkeypatch):
    """A no-op replay landed nothing, so nothing may be recorded as landed.

    LANDED is terminal and carries an `import_ledger_id`; writing it without one
    would both violate the raw-import contract and close a delivery that a later,
    real materialization still has to close.
    """
    import core.csv_excel_import as cei
    import core.db as core_db
    import core.inbound_quarantine as quarantine
    import core.inbound_raw_imports as raw_imports
    import core.raw_landing as raw_landing
    from inbound.adapters import datastream_activation_drivers as drivers

    monkeypatch.setattr(
        core_db, "get_connection",
        lambda *a, **kw: _Conn([("inbraw_1", "report.csv", "file:///q/a")]),
    )
    monkeypatch.setattr(
        quarantine, "open_quarantine_store", lambda *a, **kw: _Store({"file:///q/a": _CSV})
    )
    monkeypatch.setattr(cei, "resolve_file_source_producer", lambda *a, **kw: None)

    class _NoIsolation:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(raw_landing, "candidate_execution", lambda *a, **kw: _NoIsolation())
    monkeypatch.setattr(
        cei, "run_import",
        lambda payload, **kw: {"landed_row_count": 0, "rejected_count": 0, "no_op": True},
    )
    marked: list[dict] = []
    monkeypatch.setattr(
        raw_imports, "mark_raw_import_state", lambda conn, **kw: marked.append(kw)
    )

    drivers.managed_feed_candidate(_context())

    assert marked == []


# ---------------------------------------------------------------------------
# The creation path: there is no Datastream yet, and nothing has arrived.
# ---------------------------------------------------------------------------


def test_preview_before_any_datastream_exists_is_evidence_not_a_crash():
    """The wizard previews an inbound channel BEFORE the Datastream exists.

    An inbound address cannot be issued until a Datastream exists, so on the
    creation path there is nothing to have received anything -- the observations
    endpoint already says exactly that, with `no_inbound_datastream_bound`
    (datastream_preconfiguration_api.py:563). The preview driver instead indexed
    `context["datastream_id"]` and raised a bare KeyError, which the worker
    reported as `activation_work_failed`: a state that names nothing and sends
    the reader looking for a broken adapter.

    MEASURED live 2026-08-07: job dsaj_01KZEN77HGX3Y5AZBAMF99Z4Q9 failed with
    `KeyError: 'datastream_id'` at datastream_activation_drivers.py:75, on the
    first preview the wizard ever managed to dispatch.

    Honest emptiness, in the shape this driver already uses for an unparseable
    file: verified, no schema, coverage unavailable, and a NAMED exception.
    """
    from inbound.adapters import datastream_activation_drivers as drivers

    context = _context()
    context.pop("datastream_id")
    context["plan_intent"] = {}
    context["observation"] = {"safe_metadata": {}}

    result = drivers.managed_feed_preview(context)

    assert result["adapter_verified"] is True
    assert result["schema"] == []
    assert result["coverage"] == {"schema": "unavailable", "values": "unavailable"}
    assert [item["code"] for item in result["exceptions"]] == ["no_inbound_datastream_bound"]


def test_preview_with_a_datastream_but_no_delivery_says_which_absence_it_is():
    """Two absences, two sentences: the operator's next step differs.

    No Datastream -> create it and issue an address. A Datastream but nothing
    delivered -> wait for, or resend, the file. Collapsing them into one code
    would send half the readers to the wrong screen. `no_delivery_received_yet`
    is the vocabulary `core.inbound_discovery.NO_DELIVERY_YET` already uses.
    """
    from inbound.adapters import datastream_activation_drivers as drivers

    context = _context(plan_intent={}, observation={"safe_metadata": {}})

    result = drivers.managed_feed_preview(context)

    assert [item["code"] for item in result["exceptions"]] == ["no_delivery_received_yet"]
    assert result["schema"] == []
