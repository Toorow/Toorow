"""Story 57.7 -- what a saved configuration carries, and what it deliberately drops.

Offline by construction: `derive_template_payload` is pure, and the refusal at
the limit is exercised against a fake cursor rather than a database. No network,
no provider, no fixture account -- a contract proved offline is a contract that
can be proved at all ([[prove-contract-offline-connectors-last]]).

The five cases, and each one is a sentence of the story:

  1. the three draft-only keys leave, `configure` stays WHOLE -- a template that
     dropped a metric list would be a configuration nobody validated;
  2. the source account leaves AND is declared open -- the open variable is
     declared, never guessed by the screen;
  3. in `managed_feed`, `template_ref` leaves too: it is the one reference of
     the payload whose scope is a PROJECT (`file_source_templates.project_id
     NOT NULL`), so it cannot travel with an organization-scoped template;
  4. what comes out, with the account re-injected, still passes
     `_validate_operator_union` -- the check the applying route runs;
  5. the eleventh is refused by name, and the message says which ones to retire.
"""

from __future__ import annotations

import pytest
from core.datastream_preconfiguration import _validate_operator_union
from core.datastream_setup_templates import (
    MAX_ORG_SETUP_TEMPLATES,
    NoRecordedOperatorInput,
    TemplateConflictLabel,
    TemplateLimitReached,
    TemplateValidationError,
    derive_template_payload,
    save_template,
)

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)


def _connector_input() -> dict:
    """A finished `connector_pull` input, shaped like the wizard persists one."""
    return {
        "mode": "connector_pull",
        "name": "Daily search performance",
        "data_role": "Performance",
        "domain_ids": ["mbd_acquisition"],
        "schedule": {"mode": "daily"},
        "observation_ref": "dsob_01ABCDEFGHJKMNPQRSTVWXYZ00",
        "wizard_state": {"active_section": 3, "active_section_ref": "preview_validate",
                         "first_incomplete": "preview_validate"},
        "source": {
            "source_account_ref": "sacct_search_property",
            "connector_ref": "gsc",
            "connector_contract_version_ref": "ccv_gsc_7",
            "report_ref": "country_daily",
            "observation_ref": "dsob_01ABCDEFGHJKMNPQRSTVWXYZ00",
        },
        "configure": {
            "date_field": "date",
            "metrics": "clicks,impressions",
            "dimensions": "date,country",
            "date_window": "30",
            "filters": "",
            "history_intent": "recent",
            "cadence_intent": "daily",
            "grain": "date,country",
        },
    }


def _feed_input() -> dict:
    return {
        "mode": "managed_feed",
        "name": "Media plan of the quarter",
        "data_role": "Forecast & plan",
        "schedule": {"mode": "manual"},
        "source": {
            "channel": "file_upload",
            "source_account_ref": "",
            "template_ref": "fst_media_plan_v3",
            "staged_asset_ref": "dsa_01ABCDEFGHJKMNPQRSTVWXYZ00",
            "sheet_ref": "",
        },
        "configure": {
            "input_ref": "media_plan.csv",
            "parsing_contract": "header_row=1",
            "logical_dataset_name": "media_plan",
            "date_semantics": "period_start",
            "grain": "date,campaign",
            "write_mode": "replace",
        },
    }


def test_derivation_drops_draft_only_keys_and_keeps_configure_whole():
    payload, _ = derive_template_payload(_connector_input())

    assert "observation_ref" not in payload
    assert "wizard_state" not in payload
    assert "observation_ref" not in payload["source"]
    # `configure` is the half a person actually typed. It travels untouched.
    assert payload["configure"] == _connector_input()["configure"]
    assert payload["name"] == "Daily search performance"
    assert payload["data_role"] == "Performance"
    assert payload["domain_ids"] == ["mbd_acquisition"]


def test_derivation_reopens_the_source_account_and_declares_it():
    payload, open_variables = derive_template_payload(_connector_input())

    assert "source_account_ref" not in payload["source"]
    # DECLARED, not inferred by the screen from an absent key: the card names
    # the open variable before the click.
    assert open_variables == ["source_account_ref"]
    # And what the template IS keeping is the whole point -- the connector, its
    # contract and the report family survive the account being reopened.
    assert payload["source"]["connector_ref"] == "gsc"
    assert payload["source"]["connector_contract_version_ref"] == "ccv_gsc_7"
    assert payload["source"]["report_ref"] == "country_daily"


def test_managed_feed_reopens_the_project_scoped_template_reference():
    payload, open_variables = derive_template_payload(_feed_input())

    # `staged_asset_ref` is DRAFT-scoped evidence: removed, and NOT asked again
    # -- a new upload is what discovery gathers, not a question on a card.
    assert "staged_asset_ref" not in payload["source"]
    assert "staged_asset_ref" not in open_variables
    # `template_ref` is PROJECT-scoped, so it cannot travel with an
    # organization-scoped template. Removed AND declared open.
    assert "template_ref" not in payload["source"]
    assert open_variables == ["source_account_ref", "template_ref"]
    assert payload["source"]["channel"] == "file_upload"


def test_derived_payload_is_reapplicable_once_the_account_is_chosen():
    payload, _ = derive_template_payload(_connector_input())

    # A template that could not be re-applied would be a dead row. This is the
    # exact check `update_draft` runs on the PATCH that applies it.
    _validate_operator_union(payload)
    payload["source"]["source_account_ref"] = "sacct_another_property"
    _validate_operator_union(payload)


def test_no_operator_input_key_names_a_country_in_any_mode():
    """THE SENTENCE THE RATIFIED DOCUMENT NOW CARRIES, pinned.

    The plan asked for two open variables, "the country or the account". There
    is no country to reopen: not one key of the operator union names a country,
    a geography, a market or a region, in any of the three modes. The geographic
    posture is read on the PROJECT (`compile_geographic_intent`), and a Datastream
    has no country field to change. This test fails the day someone adds one --
    which is exactly when this section of the document must be rewritten rather
    than quietly contradicted.
    """
    from core.datastream_preconfiguration import (
        _OPERATOR_COMMON,
        _OPERATOR_CONFIGURE_KEYS,
        _OPERATOR_SOURCE_KEYS,
    )

    every_key = set(_OPERATOR_COMMON)
    for mode_keys in (*_OPERATOR_SOURCE_KEYS.values(), *_OPERATOR_CONFIGURE_KEYS.values()):
        every_key |= set(mode_keys)
    geographic = sorted(
        key
        for key in every_key
        if any(word in key.lower() for word in ("country", "geo", "market", "region"))
    )

    assert geographic == []


def test_derivation_refuses_an_unsupported_mode():
    with pytest.raises(TemplateValidationError):
        derive_template_payload({"mode": "carrier_pigeon", "source": {}, "configure": {}})


# EVERY STATEMENT `save_template` ISSUES ON A TESTED PATH, named. Before this,
# an unrecognised statement fell into an `else` that answered no rows -- and no
# rows is how `fetchone()` says NO SUCH ROW, which is precisely the decision
# `save_template` reads three times (no replay under this key, no active label,
# no materialized origin). The fake was giving the right answers for no reason,
# and a rewritten query would have kept giving them (AI-317).
#
# Two fragments are NARROW on purpose rather than wide:
#   * `idempotency_key_hash = %s` and not `idempotency_key_hash`, because the
#     INSERT names that column too;
#   * `is_active order by created_at` and not the relation alone, because the
#     duplicate-configuration probe reads `SELECT label` from the same table.
#     That probe is on no tested path, so it is NOT in the inventory: it must
#     raise the day a test reaches it, never be answered as the label list.
_SAVE_TEMPLATE = StatementInventory(
    "_FakeCursor (save_template)",
    org_of_project="select org_id from app.projects",
    idempotent_replay="idempotency_key_hash = %s",
    active_labels=(
        "select label from app.datastream_setup_templates",
        "is_active order by created_at",
    ),
    origin_revision="from app.datastream_setup_materializations",
)


class _FakeCursor:
    """The four statements `save_template` runs before it would insert.

    It answers those four and raises on anything else, so the day the product
    adds a fifth read -- or moves one of these -- the failure names the query
    instead of quietly handing the product a "no such row" it never asked for.
    """

    def __init__(self, labels: list[str]):
        self._labels = labels
        self._rows: list[tuple] = []
        self.description: list[tuple[str]] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params=None):
        statement = _SAVE_TEMPLATE.match(sql)
        # DERIVED from the statement: all four are flat SELECTs, so restating a
        # column list here would only be a second copy of the query, and the
        # copy nothing reads is the one that goes stale first.
        self.description = describe(sql)
        match statement:
            case "org_of_project":
                self._rows = [("org_EXAMPLE",)]
            case "idempotent_replay":
                # No template was ever saved under this idempotency key.
                self._rows = []
            case "active_labels":
                self._rows = [(label,) for label in self._labels]
            case "origin_revision":
                # NOTHING was materialized, and that is DECLARED rather than
                # falling out of a silent tail: it is the only answer this
                # deployment can give today -- `count(*)` on
                # `app.datastream_setup_materializations` is 0 against preprod
                # (measured 2026-08-05, 45 Datastreams).
                self._rows = []
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _SAVE_TEMPLATE.unknown(sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, labels: list[str]):
        self._labels = labels

    def cursor(self):
        return _FakeCursor(self._labels)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: the inventory is a claim about the product, so it is asserted.

    The statement below is REAL -- it is the duplicate-configuration probe
    `save_template` runs once the origin resolves -- and no test in this file
    reaches it. The old tail answered it with no rows, which the product reads
    as "no template holds this configuration": a decision, fabricated by the
    silence. It now names itself, and names the inventory it collided with.
    """
    cursor = _FakeCursor([])
    with pytest.raises(UnknownStatement) as refused:
        cursor.execute(
            "SELECT label FROM app.datastream_setup_templates "
            "WHERE org_id = %s AND content_hash = %s AND is_active"
        )

    message = str(refused.value)
    assert "content_hash = %s" in message
    assert "active_labels" in message


def test_the_eleventh_template_is_refused_and_the_message_names_what_to_retire():
    labels = [f"Saved configuration {index}" for index in range(MAX_ORG_SETUP_TEMPLATES)]

    with pytest.raises(TemplateLimitReached) as refused:
        save_template(
            _FakeConnection(labels),
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            label="One too many",
            actor="owner@example.com",
            idempotency_key="request-1",
        )

    assert refused.value.code == "setup_template_limit_reached"
    message = str(refused.value)
    assert f"limit of {MAX_ORG_SETUP_TEMPLATES}" in message
    # WHICH ONES TO RETIRE. A refusal that does not say what to remove leaves the
    # operator at a wall with no door.
    assert "Saved configuration 0" in message
    assert "Saved configuration 9" in message


def test_a_name_already_taken_in_this_organization_is_refused_by_name():
    """`409 duplicate_label`, and it is checked BEFORE the origin is read.

    `UNIQUE (org_id, label) WHERE is_active` would raise anyway, but a unique
    violation surfaces as an opaque database error; the refusal has to name what
    collided. The index is PARTIAL on purpose: a retired template must not hold
    its name forever, otherwise "retire it and save a corrected one under the
    same name" -- the ordinary repair -- is impossible.
    """
    with pytest.raises(TemplateConflictLabel) as refused:
        save_template(
            _FakeConnection(["Daily spend by market", "Weekly reach"]),
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            label="  Daily spend by market  ",  # trimmed before it is compared
            actor="owner@example.com",
            idempotency_key="request-3",
        )

    assert refused.value.code == "duplicate_label"
    assert "Daily spend by market" in str(refused.value)


def test_a_datastream_with_no_recorded_operator_input_is_refused_by_name():
    """The ONLY answer this deployment can give today: `count(*)` on
    `app.datastream_setup_materializations` is 0 against preprod (measured
    2026-08-05, 45 Datastreams), so every Datastream that exists was created
    outside the wizard and carries no reusable input.

    The cursor is the ordinary one. `_NoMaterialization` used to override
    `execute` to answer the evidence chain with no rows -- and to answer
    everything else with no rows too, which is the silence AI-317 removes. What
    it declared is now declared once, on the shared fake.
    """
    with pytest.raises(NoRecordedOperatorInput) as refused:
        save_template(
            _FakeConnection([]),
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            label="From an imported Datastream",
            actor="owner@example.com",
            idempotency_key="request-2",
        )

    assert refused.value.code == "no_recorded_operator_input"
    assert "not created by the setup wizard" in str(refused.value)
