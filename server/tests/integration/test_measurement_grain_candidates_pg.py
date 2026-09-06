"""Story 71.3 against a real database — the workbench gesture reads real bindings.

The unit tests of `test_measurement_grain_candidates.py` prove the derivation and
the refusal with a fake cursor. What they cannot prove is that
`_mdm_targets_of_current_mapping`'s SQL reads a real published mapping version,
and that `confirm_measurement_grain` composes with the real MDM
`create_measurement_grain` end to end. This file does, against the disposable
Postgres.

pg-gated: skipped without ``TEST_POSTGRES_DSN``. No real identifier appears —
`proj_EXAMPLE`, minted ULIDs, the actor is `tester`.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import datastream_workbench as workbench  # noqa: E402
from core import metric_dimensions as grains  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
)


@pytest.fixture()
def project(live_postgres):
    return make_project(live_postgres, label="Epic 71.3")


@pytest.fixture()
def vocabulary(live_postgres, project):
    _org_id, project_id = project
    return {
        "spend": make_canonical_field(
            live_postgres, project_id, "spend", kind="metric", value_type="money"
        ),
        "day": make_canonical_field(live_postgres, project_id, "day", value_type="date"),
        "campaign": make_canonical_field(live_postgres, project_id, "campaign_id"),
    }


def _datastream(live_postgres, project, vocabulary, *, bind_campaign: bool = True):
    org_id, project_id = project
    bindings = {
        vocabulary["spend"]: ("col_spend", "confirmed"),
        vocabulary["day"]: ("col_day", "confirmed"),
    }
    if bind_campaign:
        bindings[vocabulary["campaign"]] = ("col_campaign", "confirmed")
    return make_datastream(
        live_postgres, org_id, project_id, "Media daily", bindings=bindings
    )


def test_confirm_reads_the_real_bindings_and_creates_a_governed_grain(
    live_postgres, project, vocabulary
):
    """The whole path over real rows: the SQL read of the current mapping, the
    workbench refusal, and the MDM creation."""
    _org_id, project_id = project
    datastream_id = _datastream(live_postgres, project, vocabulary)

    result = workbench.confirm_measurement_grain(
        live_postgres,
        project_id=project_id,
        datastream_id=datastream_id,
        name="Media spend grain",
        head_field_id=vocabulary["spend"],
        member_field_ids=[vocabulary["day"], vocabulary["campaign"]],
        actor="tester",
    )

    # A governed grain now exists, head = spend, members = day + campaign.
    read = grains.read_measurement_grain(
        live_postgres, project_id=project_id, measurement_grain_id=result["id"]
    )
    assert read["current_version"]["head"]["canonical_field_id"] == vocabulary["spend"]
    assert {m["canonical_field_id"] for m in read["current_version"]["members"]} == {
        vocabulary["day"],
        vocabulary["campaign"],
    }


def test_confirm_refuses_a_head_the_current_mapping_does_not_bind(
    live_postgres, project, vocabulary
):
    """The workbench's own refusal, over real rows and BEFORE the MDM runs: a head
    no binding of the current mapping backs is refused."""
    _org_id, project_id = project
    datastream_id = _datastream(live_postgres, project, vocabulary)
    unbound_metric = make_canonical_field(
        live_postgres, project_id, "revenue", kind="metric", value_type="money"
    )

    with pytest.raises(Exception) as caught:
        workbench.confirm_measurement_grain(
            live_postgres,
            project_id=project_id,
            datastream_id=datastream_id,
            name="Unbacked grain",
            head_field_id=unbound_metric,
            member_field_ids=[vocabulary["day"]],
            actor="tester",
        )
    assert getattr(caught.value, "code", "") == "grain_head_not_bound_to_mdm"


def test_confirm_refuses_a_member_the_current_mapping_does_not_bind(
    live_postgres, project, vocabulary
):
    """A dimension not bound by the current mapping cannot enter a grain — the
    `mdm_target` binding is the wire, proven against a mapping that omits it."""
    _org_id, project_id = project
    datastream_id = _datastream(live_postgres, project, vocabulary, bind_campaign=False)

    with pytest.raises(Exception) as caught:
        workbench.confirm_measurement_grain(
            live_postgres,
            project_id=project_id,
            datastream_id=datastream_id,
            name="Grain naming an unbound dimension",
            head_field_id=vocabulary["spend"],
            member_field_ids=[vocabulary["day"], vocabulary["campaign"]],
            actor="tester",
        )
    assert getattr(caught.value, "code", "") == "grain_member_not_bound_to_mdm"
