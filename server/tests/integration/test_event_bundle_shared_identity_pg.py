"""An event bundle's entity identifier becomes a shared identity, and a key (AI-373).

WHAT THIS PROVES, AND WHY IT NEEDS A REAL DATABASE. The shared-identity proposal
is DERIVED at read time from the `mapping_payload` JSONB of every non-archived
Datastream of a Project (`governance.md`, amendment 2026-09-04), and a common key
is a row whose components are resolved against the canonical registry. A mock on
either side would have counted whatever it was told.

THE CHAIN IS BUILT THROUGH THE MECHANISM, NOT AROUND IT. The two mapping payloads
are not typed by hand: they come out of the wizard's own field universe
(`_normalized_field_universe`) and its profiler (`profile_fields`), fed by the
governed catalogue (`normalize_capabilities`). That is the whole point -- remove
the seam of the 2026-09-06 amendment and the event bundle's universe falls back to
`date` alone, the identity drops to ONE carrier, and this file goes red at the
line that counts them. Measured 2026-09-04 on the reference project: *video
publications* mapped `date` and nothing else while *views by video* carried
`video`, so `video x date` could not be declared.

No connector, no provider and no real identifier appears here: the manifest is the
source-agnostic fixture of `tests/core/test_event_bundle_entity.py`, read from
there so the two files cannot drift into two different declarations.

It runs as the ordinary `connector` role; `live_postgres` rolls back on teardown.
"""

from __future__ import annotations

import json

import pytest

psycopg = pytest.importorskip("psycopg")

from core import mdm_common_keys as keys  # noqa: E402
from core.datastream_field_mapping import profile_fields  # noqa: E402
from core.datastream_preconfiguration import _normalized_field_universe  # noqa: E402

from tests.core.test_event_bundle_entity import (  # noqa: E402
    ENTITY_FIELD,
    catalogue,
)
from tests.integration.epic66_fixtures import (  # noqa: E402
    hash64,
    make_canonical_field,
    make_project,
    uid,
)

#: The two flows of the reference project's shape: markers about a thing, and
#: measures about the same thing.
EVENT_BUNDLE = "marker_bundle"
FACT_REPORT = "asset_daily"


@pytest.fixture()
def project(live_postgres):
    return make_project(live_postgres, "Event bundle identity")


@pytest.fixture()
def vocabulary(live_postgres, project):
    """`date` and the entity identifier, as active canonical dimensions.

    Minted rather than assumed: the registry holds nothing by default, and a
    proposal that could not name a canonical field would say so instead of
    proposing one.
    """
    _org_id, project_id = project
    return {
        "date": make_canonical_field(live_postgres, project_id, "date", value_type="date"),
        ENTITY_FIELD: make_canonical_field(live_postgres, project_id, ENTITY_FIELD),
    }


def _mapping_payload(report_id: str, pins: dict[str, str]) -> dict:
    """The mapping a Datastream on this report would publish, built by the product.

    `pins` maps a canonical target to the canonical field id it is pinned to --
    the governed mapping change a person makes on the Mapping tab, and the only
    thing typed by this fixture.
    """
    universe = _normalized_field_universe(
        {"connector_contract": {"contract": catalogue()}}, report_id
    )
    payload = profile_fields(field_records=universe, sample_data=None)
    for field in payload["fields"]:
        target = pins.get(str(field["binding"].get("canonical_target") or ""))
        if target:
            field["binding"]["mdm_target"] = target
            field["binding"]["status"] = "confirmed"
    return payload


def _publish(conn, org_id: str, project_id: str, name: str, payload: dict) -> str:
    """One Datastream whose CURRENT mapping version carries `payload`."""
    datastream_id, plan_id, mapping_id = uid("ds"), uid("dpv"), uid("dmv")
    with conn.cursor() as cur:
        cur.execute(
            # `ck_datastreams_source_kind` demands a module beside a
            # `connector_pull`: the kind and its module travel together or the row
            # is refused. The module is the fixture's own, never a real one.
            "INSERT INTO app.datastreams "
            "(id, org_id, project_id, name, created_by, source_kind, module_name) "
            "VALUES (%s,%s,%s,%s,'tester','connector_pull','test-event-module')",
            (datastream_id, org_id, project_id, name),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                    '{}'::jsonb,%s,%s,'tester')
            """,
            (plan_id, datastream_id, project_id, hash64(), hash64()),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'mapping.v1',%s,%s,%s,'0.1.1','2',%s::jsonb,'{}'::jsonb,%s,'tester')
            """,
            (
                mapping_id,
                datastream_id,
                project_id,
                hash64(),
                plan_id,
                hash64(),
                json.dumps(payload),
                hash64(),
            ),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (mapping_id, datastream_id),
        )
    return datastream_id


@pytest.fixture()
def two_flows(live_postgres, project, vocabulary):
    """The event bundle and the fact report, both published, both pinned."""
    org_id, project_id = project
    pins = {"date": vocabulary["date"], ENTITY_FIELD: vocabulary[ENTITY_FIELD]}
    markers = _publish(
        live_postgres, org_id, project_id, "Publication markers",
        _mapping_payload(EVENT_BUNDLE, pins),
    )
    measures = _publish(
        live_postgres, org_id, project_id, "Views by asset",
        _mapping_payload(FACT_REPORT, pins),
    )
    return markers, measures


def test_the_event_bundle_publishes_its_entity_identifier_in_its_mapping(two_flows, live_postgres):
    """The measured defect, at the layer where it was seen: the mapping itself."""
    markers, _measures = two_flows
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT m.mapping_payload FROM app.datastreams d "
            "JOIN app.datastream_mapping_versions m ON m.id = d.current_mapping_version_id "
            "WHERE d.id = %s",
            (markers,),
        )
        payload = cur.fetchone()[0]

    columns = {str(field["field_id"]) for field in payload["fields"]}
    assert ENTITY_FIELD in columns, (
        "the event bundle's mapping must carry the identity its markers are about"
    )


def test_the_entity_identifier_is_proposed_as_a_shared_identity_with_two_carriers(
    two_flows, live_postgres, project, vocabulary
):
    """`video` had ONE carrier where the project has two (measured 2026-09-04)."""
    markers, measures = two_flows
    _org_id, project_id = project

    proposed = keys.propose_shared_identities(live_postgres, project_id=project_id)

    assert proposed["state"] == "available"
    by_identity = {item["identity"]: item for item in proposed["proposals"]}
    assert ENTITY_FIELD in by_identity, sorted(by_identity)
    identity = by_identity[ENTITY_FIELD]
    assert identity["role"] == "dimension"
    assert identity["carrier_count"] == 2
    assert sorted(carrier["datastream_id"] for carrier in identity["carriers"]) == sorted(
        [markers, measures]
    )
    assert identity["canonical_field_id"] == vocabulary[ENTITY_FIELD]
    assert sorted(identity["already_pinned"]) == sorted([markers, measures])
    assert "declare a common key over it" in identity["gesture"]


def test_a_common_key_over_the_entity_identifier_and_the_day_is_declarable(
    two_flows, live_postgres, project, vocabulary
):
    """The whole point of the pin: the crossing becomes expressible."""
    markers, measures = two_flows
    _org_id, project_id = project

    declared = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Asset by day",
        canonical_field_ids=[vocabulary[ENTITY_FIELD], vocabulary["date"]],
        actor="owner@example.com",
    )

    components = declared["current_version"]["components"]
    assert [component["canonical_name"] for component in components] == [
        ENTITY_FIELD,
        "date",
    ]
    # DERIVED, never stored: both flows implement BOTH components, which is what
    # makes the crossing expressible rather than merely named.
    coverage = keys.mapping_coverage(
        live_postgres, project_id=project_id, components=components
    )
    assert coverage["state"] == "available"
    implementing = {
        component["canonical_name"]: {
            item["datastream_id"] for item in component["implemented_by"]
        }
        for component in coverage["components"]
    }
    assert implementing == {
        ENTITY_FIELD: {markers, measures},
        "date": {markers, measures},
    }
