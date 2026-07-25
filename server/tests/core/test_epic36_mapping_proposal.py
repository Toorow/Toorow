"""Story 36.17 -- agentic mapping-correction proposal tests (offline, MagicMock).

Covers the invariants of E36-FR07 / E36-NFR01 / E36-NFR05:
  (a) a failing gate check rejects the candidate AND current_mapping_version_id is
      NEVER advanced (no UPDATE to the live pointer);
  (b) a saved proposal is an immutable NEW mapping version, NON-LIVE, state='ready',
      routed to the governed review;
  (c) provider samples/values NEVER appear in tool output (hostile schema fixture);
  (d) mode gate: Advisory only explains, Delegated persists, Autonomous can test;
  (e) diff_mappings is deterministic and reveals no raw values.

Live-Postgres is skipped in the established pattern: these tests drive the pure gate
functions + create_mapping_proposal against a scripted fake connection that records
every SQL statement, so the pointer-never-advances invariant is asserted structurally
(the UPDATE is simply never emitted).
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# A scripted fake connection: records every executed SQL statement and returns
# scripted fetchone values keyed by a substring match. execute_operation and
# create_mapping_proposal drive real SQL through this, so we can assert on the
# statements actually issued (e.g. the ABSENCE of a pointer UPDATE).
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, owner):
        self._owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._owner.statements.append((sql, params))
        self._owner._last_sql = sql
        return None

    def fetchone(self):
        sql = self._owner._last_sql or ""
        for pattern, value in self._owner.responses:
            if pattern in sql:
                # A callable response is invoked once (to advance a counter/state).
                return value() if callable(value) else value
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, responses):
        # responses: list of (sql_substring, value) evaluated top-to-bottom.
        self.responses = responses
        self.statements = []
        self._last_sql = None
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _valid_payload():
    """Build a schema-valid, ambiguity-free mapping payload via the 12.3 engine."""
    from core.datastream_field_mapping import profile_fields

    field_records = [
        {
            "field_id": "date",
            "physical_type": "date",
            "kind": "date",
            "canonical_target": "date",
        },
        {
            "field_id": "clicks",
            "physical_type": "integer",
            "kind": "metric",
            "canonical_target": "clicks",
            "aggregation": "sum",
        },
    ]
    payload = profile_fields(field_records=field_records)
    return payload


def _propose_responses(*, with_current=False):
    """Standard scripted responses for a create_mapping_proposal happy path."""
    responses = [
        # execute_operation: no existing operation, then the inserted op id.
        ("FROM app.operations", None),
        ("INSERT INTO app.operations", ("op-test-1",)),
        # _fetch_current_version_row: datastream row (current_mapping_version_id, plan)
        (
            "SELECT current_mapping_version_id, current_plan_version_id",
            ("dmap_PREV" if with_current else None, "dpv-1"),
        ),
        # (if with_current) the previous version row read -- 17 columns
        (
            "FROM app.datastream_mapping_versions\n        WHERE id = %s",
            (
                "dmap_PREV",
                "ds-1",
                "proj-1",
                1,
                "1",
                "a" * 64,
                "dpv-1",
                None,
                "b" * 64,
                "0.1.1",
                "1",
                True,
                0,
                "{}",
                "{}",
                "actor",
                None,
            ),
        ),
        # version_number allocation
        ("COALESCE(MAX(version_number)", (2,)),
        # the INSERT ... RETURNING the new version row (17 cols)
        (
            "INSERT INTO app.datastream_mapping_versions",
            lambda: (
                "dmap_NEW",
                "ds-1",
                "proj-1",
                2,
                "1",
                "c" * 64,
                "dpv-1",
                None,
                "d" * 64,
                "0.1.1",
                "1",
                True,
                0,
                "{}",
                "{}",
                "actor",
                None,
            ),
        ),
    ]
    return responses


# ---------------------------------------------------------------------------
# (d) Mode gate.
# ---------------------------------------------------------------------------


def test_mode_gate_advisory_delegated_autonomous():
    from core.mapping_versions import (
        MODE_ADVISORY,
        MODE_AUTONOMOUS,
        MODE_DELEGATED,
        mode_permits,
    )

    # Advisory: explain only.
    assert mode_permits(MODE_ADVISORY, effect="explain") is True
    assert mode_permits(MODE_ADVISORY, effect="persist") is False
    assert mode_permits(MODE_ADVISORY, effect="test") is False

    # Delegated: explain + persist, NOT test.
    assert mode_permits(MODE_DELEGATED, effect="explain") is True
    assert mode_permits(MODE_DELEGATED, effect="persist") is True
    assert mode_permits(MODE_DELEGATED, effect="test") is False

    # Autonomous: explain + persist + test.
    assert mode_permits(MODE_AUTONOMOUS, effect="explain") is True
    assert mode_permits(MODE_AUTONOMOUS, effect="persist") is True
    assert mode_permits(MODE_AUTONOMOUS, effect="test") is True

    # Unknown mode fails closed.
    assert mode_permits("bogus", effect="persist") is False
    assert mode_permits("bogus", effect="explain") is True


def test_advisory_mode_cannot_persist_a_proposal():
    from core.mapping_versions import (
        MODE_ADVISORY,
        MappingProposalModeDenied,
        create_mapping_proposal,
    )

    conn = _FakeConn([])
    with pytest.raises(MappingProposalModeDenied):
        create_mapping_proposal(
            conn,
            datastream_id="ds-1",
            project_id="proj-1",
            effective_org_id="org-1",
            mapping_payload=_valid_payload(),
            plan_version_id="dpv-1",
            actor="agent-1",
            mode=MODE_ADVISORY,
            idempotency_key="idem-1",
            authorized=True,
            dq_evidence={"total_unresolved": 0},
        )
    # No SQL was even attempted.
    assert conn.statements == []


# ---------------------------------------------------------------------------
# (b) A saved proposal is an immutable NEW version, NON-LIVE, state='ready'.
# ---------------------------------------------------------------------------


def test_delegated_proposal_is_immutable_new_version_ready_and_non_live():
    from core.mapping_versions import (
        MODE_DELEGATED,
        STATE_READY,
        create_mapping_proposal,
    )

    conn = _FakeConn(_propose_responses(with_current=False))
    result = create_mapping_proposal(
        conn,
        datastream_id="ds-1",
        project_id="proj-1",
        effective_org_id="org-1",
        mapping_payload=_valid_payload(),
        plan_version_id="dpv-1",
        actor="agent-1",
        mode=MODE_DELEGATED,
        idempotency_key="idem-ready-1",
        authorized=True,
        dq_evidence={"total_unresolved": 0},
        cost_estimate={"estimated_units": 10},
    )

    assert result.state == STATE_READY
    assert result.mapping_version_id.startswith("dmap_")
    assert result.proposal_id.startswith("mprop_")
    assert result.operation_id == "op-test-1"

    joined = "\n".join(sql for sql, _ in conn.statements)
    # An immutable new version row was inserted.
    assert "INSERT INTO app.datastream_mapping_versions" in joined
    # A proposal lifecycle row (state='ready') was recorded.
    assert "INSERT INTO app.mapping_proposals" in joined
    # THE INVARIANT: the live pointer is NEVER advanced in this story.
    assert not re.search(
        r"UPDATE\s+app\.datastreams\s+SET\s+current_mapping_version_id",
        joined,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# (a) A failing gate check rejects the candidate; the pointer never advances.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, expected_failed",
    [
        # authorization: strict seam denied edit.
        (dict(authorized=False, dq_evidence={"total_unresolved": 0}), "authorization"),
        # dq: open unresolved issues.
        (dict(authorized=True, dq_evidence={"total_unresolved": 3}), "dq"),
        # dq: unavailable evidence (fail-closed).
        (dict(authorized=True, dq_evidence=None), "dq"),
        # cost: over the ceiling.
        (
            dict(
                authorized=True,
                dq_evidence={"total_unresolved": 0},
                cost_estimate={"estimated_units": 10_000_000},
            ),
            "cost",
        ),
    ],
)
def test_failing_check_rejects_and_pointer_never_advances(kwargs, expected_failed):
    from core.mapping_versions import (
        MODE_DELEGATED,
        MappingProposalRejected,
        create_mapping_proposal,
    )

    conn = _FakeConn(_propose_responses())
    with pytest.raises(MappingProposalRejected) as excinfo:
        create_mapping_proposal(
            conn,
            datastream_id="ds-1",
            project_id="proj-1",
            effective_org_id="org-1",
            mapping_payload=_valid_payload(),
            plan_version_id="dpv-1",
            actor="agent-1",
            mode=MODE_DELEGATED,
            idempotency_key="idem-fail",
            **kwargs,
        )

    checks = {c["check"]: c for c in excinfo.value.checks}
    assert checks[expected_failed]["passed"] is False

    # No version insert, no proposal insert, and above all NO pointer UPDATE.
    joined = "\n".join(sql for sql, _ in conn.statements)
    assert "INSERT INTO app.datastream_mapping_versions" not in joined
    assert "INSERT INTO app.mapping_proposals" not in joined
    assert not re.search(
        r"UPDATE\s+app\.datastreams\s+SET\s+current_mapping_version_id",
        joined,
        re.IGNORECASE,
    )


def test_compile_check_fails_on_structurally_invalid_payload():
    from core.mapping_versions import (
        MODE_DELEGATED,
        MappingProposalRejected,
        create_mapping_proposal,
    )

    conn = _FakeConn(_propose_responses())
    with pytest.raises(MappingProposalRejected) as excinfo:
        create_mapping_proposal(
            conn,
            datastream_id="ds-1",
            project_id="proj-1",
            effective_org_id="org-1",
            mapping_payload={"totally": "invalid"},  # not a valid mapping
            plan_version_id="dpv-1",
            actor="agent-1",
            mode=MODE_DELEGATED,
            idempotency_key="idem-badcompile",
            authorized=True,
            dq_evidence={"total_unresolved": 0},
        )
    checks = {c["check"]: c for c in excinfo.value.checks}
    assert checks["compile"]["passed"] is False
    joined = "\n".join(sql for sql, _ in conn.statements)
    assert "INSERT INTO app.datastream_mapping_versions" not in joined


# ---------------------------------------------------------------------------
# (c) Provider samples/values NEVER appear in the sanitized output.
# ---------------------------------------------------------------------------


def test_redact_schema_evidence_drops_provider_samples_and_pii():
    from core.mapping_proposal_mcp import redact_schema_evidence

    hostile = [
        {
            "field_id": "email",
            "physical_type": "string",
            "kind": "dimension",
            # Hostile extras a provider might smuggle in:
            "sample_values": ["alice@example.com", "bob@example.com"],
            "observed_rows": [{"email": "carol@secret.com", "ssn": "123-45-6789"}],
            "access_token": "ya29.SUPERSECRET",
            "raw_payload": "PROVIDER-BLOB-DO-NOT-LEAK",
        },
        {
            "field_id": "revenue",
            "physical_type": "decimal",
            "kind": "metric",
            "sample_values": ["999999.99"],
        },
    ]
    evidence = redact_schema_evidence(hostile)

    blob = repr(evidence)
    # Only names/types/kinds/hash/counts survive.
    assert evidence["field_count"] == 2
    assert len(evidence["source_schema_hash"]) == 64
    for field in evidence["fields"]:
        assert set(field.keys()) == {"field_id", "type", "kind"}
    # NONE of the hostile provider values reach the output.
    for leak in (
        "alice@example.com",
        "bob@example.com",
        "carol@secret.com",
        "123-45-6789",
        "SUPERSECRET",
        "PROVIDER-BLOB",
        "999999.99",
        "observed_rows",
        "sample_values",
        "access_token",
    ):
        assert leak not in blob, leak


def test_safe_checks_only_expose_codes_not_free_form_detail():
    from core.mapping_proposal_mcp import _safe_checks

    raw = [
        {
            "check": "dq",
            "passed": False,
            "code": "dq_open_issues",
            "detail": "SECRET provider error text: token=abc123",
        }
    ]
    safe = _safe_checks(raw)
    assert safe == [{"check": "dq", "passed": False, "code": "dq_open_issues"}]
    assert "SECRET" not in repr(safe)
    assert "token=abc123" not in repr(safe)


# ---------------------------------------------------------------------------
# (e) diff_mappings is deterministic and reveals no raw values.
# ---------------------------------------------------------------------------


def _version_row(version_id, fields, *, content_hash, schema_hash, version_number=1):
    return {
        "id": version_id,
        "version_number": version_number,
        "content_hash": content_hash,
        "source_schema_hash": schema_hash,
        "mapping_payload": {"fields": fields},
    }


def test_diff_mappings_is_deterministic_and_reveals_field_level_changes():
    from core.mapping_diff import diff_mappings

    before = _version_row(
        "dmap_A",
        [
            {
                "field_id": "clicks",
                "physical_type": "integer",
                "suggestion": {"semantic_role": "measure", "currency": "unknown",
                               "non_additive": False},
                "binding": {"canonical_target": "clicks", "mdm_target": None,
                            "status": "confirmed"},
            },
            {
                "field_id": "old_dim",
                "physical_type": "string",
                "suggestion": {"semantic_role": "dimension", "currency": "unknown",
                               "non_additive": False},
                "binding": {"canonical_target": None, "mdm_target": None,
                            "status": "suggested"},
            },
        ],
        content_hash="a" * 64,
        schema_hash="1" * 64,
    )
    after = _version_row(
        "dmap_B",
        [
            {
                "field_id": "clicks",
                "physical_type": "bigint",  # type change
                "suggestion": {"semantic_role": "measure", "currency": "EUR",  # currency change
                               "non_additive": False},
                "binding": {"canonical_target": "clicks_total",  # target rebinding
                            "mdm_target": None, "status": "confirmed"},
            },
            {
                "field_id": "new_field",  # addition
                "physical_type": "date",
                "suggestion": {"semantic_role": "primary_date", "currency": "unknown",
                               "non_additive": False},
                "binding": {"canonical_target": "date", "mdm_target": None,
                            "status": "suggested"},
            },
        ],
        content_hash="b" * 64,
        schema_hash="2" * 64,
        version_number=2,
    )

    d1 = diff_mappings(before, after)
    d2 = diff_mappings(before, after)
    assert d1 == d2  # deterministic

    assert d1["added_fields"] == ["new_field"]
    assert d1["removed_fields"] == ["old_dim"]
    assert d1["type_changes"] == [{"field_id": "clicks", "from": "integer", "to": "bigint"}]
    assert d1["target_rebindings"] == [
        {"field_id": "clicks", "from": "clicks", "to": "clicks_total"}
    ]
    assert d1["currency_conflicts"] == [
        {"field_id": "clicks", "from": "unknown", "to": "EUR"}
    ]
    assert d1["schema_changed"] is True
    assert d1["unchanged"] is False


def test_diff_mappings_handles_null_before_and_leaks_no_values():
    from core.mapping_diff import diff_mappings

    after = _version_row(
        "dmap_B",
        [
            {
                "field_id": "amount",
                "physical_type": "decimal",
                # A hostile payload might carry sample values -- the diff must ignore them.
                "profile": {"sample_values": ["12345.67", "alice@example.com"]},
                "suggestion": {"semantic_role": "measure", "currency": "USD",
                               "non_additive": False},
                "binding": {"canonical_target": "amount", "mdm_target": None,
                            "status": "confirmed"},
            }
        ],
        content_hash="c" * 64,
        schema_hash="3" * 64,
    )
    d = diff_mappings(None, after)
    assert d["added_fields"] == ["amount"]
    assert d["before"]["version_id"] is None
    blob = repr(d)
    assert "12345.67" not in blob
    assert "alice@example.com" not in blob


def test_diff_mappings_equal_versions_report_unchanged():
    from core.mapping_diff import diff_mappings

    row = _version_row(
        "dmap_X",
        [
            {
                "field_id": "clicks",
                "physical_type": "integer",
                "suggestion": {"semantic_role": "measure", "currency": "unknown",
                               "non_additive": False},
                "binding": {"canonical_target": "clicks", "mdm_target": None,
                            "status": "confirmed"},
            }
        ],
        content_hash="e" * 64,
        schema_hash="9" * 64,
    )
    d = diff_mappings(row, row)
    assert d["unchanged"] is True
    assert d["added_fields"] == []
    assert d["removed_fields"] == []
    assert d["type_changes"] == []
