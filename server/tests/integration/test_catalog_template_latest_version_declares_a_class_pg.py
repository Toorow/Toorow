"""Every catalog Template's LATEST version declares a `class` -- except the generic one.

WHY THIS EXISTS (file-source-ingestion, amendment of 2026-08-29). The placement
gate refuses an import whose Template declares no `class`
(`file_source_producer.stamp_placement`, `no_placement_class`), and the five
OFFLINE catalog Templates were seeded without one: every catalog-bound
Datastream was refused by construction. Migration 319 released a version 2 of
each, carrying `class: actual`; readers resolve a code to its latest version.

What nothing guarded before this file: THE NEXT SEED. A migration adding a
Template (or a new version of one) without a class would put the catalog back
where G9 found it, and no test would go red. This one reads the catalog as the
migrations leave it on a disposable Postgres and refuses:

  * a Template whose latest version carries no `class` -- `GENERIC_TABULAR_V1`
    excepted, and that exception is asserted too, because the amendment says it
    keeps no class ON PURPOSE (a generic tabular file has no place in the
    reconciliation matrix until a Template says which);
  * a `class` outside the vocabulary the placement gate accepts;
  * a catalog with fewer Templates than the five OFFLINE ones plus the generic
    one -- the instrument would otherwise pass on an empty table.

Version 1 rows are NOT judged: `app.protect_import_template` refuses UPDATE,
so a version kept below a classed version 2 is immutable history, exactly as
the amendment says ("a Datastream that pinned version 1 explicitly keeps a
contract the gate refuses, exactly as before").

SKIPS when TEST_POSTGRES_DSN is unset. Never run against production.
"""

from __future__ import annotations

import os

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres catalog test skipped",
)

#: The one Template the amendment leaves classless on purpose.
GENERIC_CODE = "GENERIC_TABULAR_V1"

#: The catalog as migration 319 leaves it. A seed that REMOVES one of these is a
#: catalog change this test must be told about, not a silent pass.
EXPECTED_CATALOG_CODES = frozenset(
    {
        GENERIC_CODE,
        "OFFLINE_DOOH_V1",
        "OFFLINE_OOH_V1",
        "OFFLINE_PRESS_V1",
        "OFFLINE_RADIO_V1",
        "OFFLINE_TV_V1",
    }
)


@pytest.fixture
def latest_versions() -> dict[str, str | None]:
    """`{template_code: class of its latest version}` read from the catalog."""
    dsn = os.environ["TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (template_code)
                   template_code, contract->>'class'
            FROM app.import_templates
            ORDER BY template_code, version DESC
            """
        )
        return {str(code): klass for code, klass in cur.fetchall()}


def _placement_classes() -> frozenset[str]:
    """The vocabulary the placement gate accepts -- read from the gate, not retyped."""
    from core.file_source_producer import PLACEMENT_CLASSES  # noqa: PLC0415

    return frozenset(str(v) for v in PLACEMENT_CLASSES)


def test_the_catalog_holds_the_templates_the_amendment_named(latest_versions):
    missing = EXPECTED_CATALOG_CODES - set(latest_versions)
    assert not missing, (
        f"catalog Templates missing on this base: {sorted(missing)} -- either the "
        "migrations were not applied or a seed removed one; this test would "
        "otherwise pass on an empty table"
    )


def test_every_latest_version_declares_a_class_except_the_generic_one(latest_versions):
    classless = sorted(
        code
        for code, klass in latest_versions.items()
        if code != GENERIC_CODE and not klass
    )
    assert not classless, (
        f"catalog Templates whose LATEST version declares no `class`: {classless}. "
        "The placement gate refuses every import bound to them (no_placement_class). "
        "Release a new version carrying the class -- never edit the seeded row."
    )


def test_the_generic_template_keeps_no_class_on_purpose(latest_versions):
    assert latest_versions.get(GENERIC_CODE) is None, (
        "GENERIC_TABULAR_V1 now declares a class; the amendment keeps it classless "
        "on purpose -- a generic tabular file has no place in the reconciliation "
        "matrix until a Template says which. Amend file-source-ingestion.md first."
    )


def test_every_declared_class_is_one_the_gate_accepts(latest_versions):
    accepted = _placement_classes()
    foreign = sorted(
        (code, klass)
        for code, klass in latest_versions.items()
        if klass is not None and klass not in accepted
    )
    assert not foreign, (
        f"catalog Templates declaring a class the placement gate does not accept: "
        f"{foreign}; accepted: {sorted(accepted)}"
    )
