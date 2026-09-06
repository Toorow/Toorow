"""The missing caller: turn the shipped tax seed into governed preset versions.

Story 48.4 shipped everything a preset needs -- ``validate_preset``,
``seed_rows_as_preset_payloads`` (which carries the seed's caveat verbatim),
``record_preset_version`` and ``list_preset_versions`` -- and nothing that calls
them outside a test. Measured on 2026-08-01:

    grep -rn "seed_rows_as_preset_payloads\\|record_preset_version\\|
              list_preset_versions\\|propose_from_presets" server --include=*.py \\
      | grep -v "core/tax_fee_presets.py"
    # -> only server/tests/core/test_tax_fee_presets_and_evidence.py

So ``app.tax_fee_preset_versions`` is empty in every database, and
``propose_from_presets`` -- which only considers ``status == "published"`` -- has
exactly one possible answer everywhere. A proposal tool built on that would be
born with nothing to propose. This module is the door that fills it.

Three properties, each with a failure it prevents:

* **the source version is a content digest of the seed file.** A corrected seed
  produces a NEW ``source_reference_version``, therefore a new content hash,
  therefore a new preset version -- rather than silently changing what an already
  adopted rule cites;
* **it invents nothing.** Every field comes from the seed row or from
  ``seed_rows_as_preset_payloads``. A row with an empty ``source_note`` aborts the
  whole import (both the loader and the payload builder refuse), because the
  caveat is the one honest thing the platform ships with a rate;
* **it never commits.** The caller owns the transaction, like every other
  governed store here: a mutation and its audit evidence commit together or not
  at all.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Prefix of the version string every imported preset cites as its source. Named
#: after the seed rather than after a date: a date says when someone ran a script,
#: a digest says which bytes the rate came from.
SEED_REFERENCE_PREFIX = "country_tax_defaults"

#: How much of the sha256 travels into the version string. Sixteen hex characters
#: is 64 bits of the digest -- far past any accidental collision between revisions
#: of one 19-row file, and short enough to read in a rule's evidence block.
_DIGEST_CHARS = 16


def seed_source_reference_version(path: Path | None = None) -> str:
    """``country_tax_defaults@<sha256[:16]>`` for the seed file at *path*.

    Pure, and deliberately over the RAW BYTES rather than over the parsed rows: a
    change to a ``source_note`` -- a caveat being weakened, say -- must produce a
    new version even though no rate moved.
    """
    from core.fee_tax_country_defaults import default_tax_seed_path  # noqa: PLC0415

    seed_path = Path(path) if path is not None else default_tax_seed_path()
    digest = hashlib.sha256(seed_path.read_bytes()).hexdigest()
    return f"{SEED_REFERENCE_PREFIX}@{digest[:_DIGEST_CHARS]}"


def seed_preset_payloads(path: Path | None = None) -> list[dict[str, Any]]:
    """Every seed row as a validated preset payload. No database, no writes.

    Built in full BEFORE anything is persisted, so a single unusable row aborts
    the whole import rather than leaving a partial one behind.
    """
    from core.fee_tax_country_defaults import (  # noqa: PLC0415
        default_tax_seed_path,
        load_country_tax_defaults,
    )
    from core.tax_fee_presets import seed_rows_as_preset_payloads  # noqa: PLC0415

    seed_path = Path(path) if path is not None else default_tax_seed_path()
    rows = load_country_tax_defaults(seed_path)
    return seed_rows_as_preset_payloads(
        rows, source_reference_version=seed_source_reference_version(seed_path)
    )


def _preset_count(conn, org_id: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.tax_fee_preset_versions "
            "WHERE COALESCE(org_id, '') = COALESCE(%s, '')",
            (org_id,),
        )
        return int(cur.fetchone()[0])


def import_seed_presets(
    conn,
    *,
    actor: str,
    publish: bool = False,
    path: Path | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Import the shipped seed as preset versions. Returns what actually happened.

    ``org_id=None`` is the default and means PLATFORM-SHARED: the seed is the same
    reference for every organization, and copying it per organization would create
    as many divergent copies of one statement about the world.

    Idempotent by content, because ``record_preset_version`` is: a second run over
    an unchanged seed returns the stored ids and reports ``created=0``. The counts
    are measured by counting rows before and after rather than by re-deriving the
    content hash here -- a second implementation of that digest is a second thing
    to keep in step.

    ``publish`` only takes effect for rows this run CREATES. An existing draft is
    returned as it stands: promoting a draft to published is a decision about that
    version, and silently flipping it from an importer would be an unreviewed
    publication.
    """
    payloads = seed_preset_payloads(path)
    before = _preset_count(conn, org_id)

    from core.tax_fee_presets import record_preset_version  # noqa: PLC0415

    ids: list[str] = []
    for payload in payloads:
        ids.append(
            record_preset_version(
                conn, payload=payload, org_id=org_id, actor=actor, publish=publish
            )
        )
    after = _preset_count(conn, org_id)
    created = after - before
    result = {
        "source_reference_version": seed_source_reference_version(path),
        "rows": len(payloads),
        "created": created,
        "unchanged": len(payloads) - created,
        "published": bool(publish),
        "org_id": org_id,
        "preset_version_ids": ids,
    }
    logger.info(
        "tax_fee_preset_seeding: %d row(s), %d created, %d unchanged (%s)",
        result["rows"],
        result["created"],
        result["unchanged"],
        result["source_reference_version"],
    )
    return result
