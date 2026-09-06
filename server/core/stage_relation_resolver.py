"""The pair of relations ONE Datastream is read from -- story 58.3, epic 58.

WHY THE PAIR HAS TO BE DECLARED AND CANNOT BE DERIVED. `Collected` is the raw
relation a connector lands in; `Mapped` is the `stg_` model that reads it. Neither
name follows from anything the platform already knows:

  * `module_name` does not give the relation. Measured 2026-08-06: 36 connectors
    declare 52 raw relations, among them `raw_ga4_standard_daily`,
    `raw_gbp_search_keyword_monthly`, `raw_shopify_orders`, `raw_square_payments`
    and `raw_monday_board_snapshot`. There is no `raw_<connector>_daily` rule --
    28 of the 38 follow that shape and 10 do not.
  * The report profile does not give it either. `google-analytics` has 8 profiles
    and 8 relations, and only 4 of the 8 correspondences are derivable from the
    two names (`pages_daily_landing` -> `raw_ga4_landing_daily`).
  * And a wrong address is not a wrong number, it is ANOTHER CONNECTOR'S ROWS
    served under this flux's name. `core/verification.py` says it of its own
    per-module registry: a key it does not know falls through to "ANOTHER
    connector's table, counted and served as this pull's verdict". As a counter
    that was a false number; as a READ ADDRESS it would be somebody else's data
    on the screen.

So `report_profiles[]` of every one of the 39 manifests carries `raw_relation`
and `staging_relation`, and `tests/conformance/test_profile_relations_declared.py`
is what keeps them true: it refuses a profile arriving without the keys, and it
refuses a value naming a relation that connector does not declare.

WHY `staging_relation` IS DECLARED TOO, and this is the point the story's
arbitrage 1 did not have to decide because it had not measured it: the raw
relation does not determine the staging model either. `raw_gsc_daily` is read by
FOUR staging models (`stg_gsc_daily`, `stg_gsc_query_page_daily`,
`stg_gsc_surface_daily`, `stg_gsc_search_appearance_daily`), each filtering the
rows of a different profile; `raw_klaviyo_daily` and `raw_linkedin_ads_daily` are
read by two each. Picking one of four would serve the `query x page` rows of a
flux whose profile is `page_daily` -- the same defect as a wrong raw address, one
layer up. The dbt graph is still read here, but only to CHECK a declaration, never
to invent one.

FOUR REASONS AN ADDRESS IS ABSENT, AND EACH IS ITS OWN WORD (arbitrage 7's rule:
two causes, two words):

  * `report_profile_not_set`          -- the Datastream names no profile. Measured
    2026-08-06: 773 of the 842 live Datastreams. Giving them one is real work, and
    it is not this story's.
  * `raw_relation_not_declared`       -- the profile declares no raw relation. The
    event profiles are the honest case: `campaign_launch`, `social_post`,
    `product_launch`, `video_upload`, `board_events` and Brevo's
    `transactional_events` land in `app.context_events`, in Postgres, and never in
    the warehouse. So do the three connectors that declare no raw relation at all
    (`bigquery` reads an external table, `generic` has no fixed shape, `github`
    does not land in this warehouse).
  * `staging_model_absent`            -- the raw relation is declared and NO `stg_`
    model reads it. Measured: exactly 6 relations, the `*_catalog_daily` of ga4,
    hubspot, shopify, square, stripe and woocommerce.
  * `staging_relation_not_declared`   -- staging models DO read the relation but
    the profile declares none of them. The catalog-driven profiles of `gsc`,
    `klaviyo` and `linkedin-ads` are exactly this: which staging model their rows
    reach depends on the fields the operator selected, so no declaration can be
    true for all of them. Saying `staging_model_absent` there would be a false
    sentence about the repository.

AD-2: no connector name appears in this file. The manifests are walked, the dbt
staging directories are read, and both are declarations of the modules themselves.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

#: The manifest keys this story adds to every `report_profiles[]` entry.
RAW_RELATION_KEY = "raw_relation"
STAGING_RELATION_KEY = "staging_relation"

#: Why the pair has no address. One word per cause; see the module docstring.
REPORT_PROFILE_NOT_SET = "report_profile_not_set"
RAW_RELATION_NOT_DECLARED = "raw_relation_not_declared"
STAGING_MODEL_ABSENT = "staging_model_absent"
STAGING_RELATION_NOT_DECLARED = "staging_relation_not_declared"

#: The sentence each absence is READ as, written once and server-side.
#:
#: The screen renders these verbatim and holds none of its own: a reason written
#: in a component is a second answer that can disagree with the first, and the
#: refusal on the forced call (`422`) has to quote the same words as the greyed
#: control that preceded it. One wording, two doors.
_MESSAGES = {
    REPORT_PROFILE_NOT_SET: (
        "This Datastream names no report profile, so no relation can be resolved "
        "for it. The profile is what says which relation of the connector its rows "
        "land in."
    ),
    RAW_RELATION_NOT_DECLARED: (
        "This report profile declares no raw relation, so there is no collected "
        "relation to read. A profile that lands its rows outside the warehouse has "
        "nothing to show here."
    ),
    STAGING_MODEL_ABSENT: (
        "No staging model reads this raw relation, so the collected rows have no "
        "mapped counterpart to compare them with."
    ),
    STAGING_RELATION_NOT_DECLARED: (
        "This report profile declares no staging model, and several read its raw "
        "relation. Naming one of them would show the rows of another profile under "
        "this flux."
    ),
}


def message_for(reason: str | None) -> str | None:
    """The sentence of a reason code, or `None` when there is no reason."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


# ---------------------------------------------------------------------------
# What the modules declare. Pure reads of the repository; no database.
# ---------------------------------------------------------------------------

#: A `CREATE TABLE` statement and the name it creates -- the same shape
#: `core.raw_table_provisioning` harvests, reused rather than re-expressed.
_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)", re.IGNORECASE
)

#: `{{ source('<schema>', '<relation>') }}` as every staging model writes it.
_SOURCE = re.compile(r"source\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")


def _relation_name(statement: str) -> str | None:
    match = _CREATE.search(statement)
    if match is None:
        return None
    name = match.group(1).strip('"')
    return name.rsplit(".", 1)[-1]


def declared_raw_relations(modules_dir: str | Path | None = None) -> dict[str, list[str]]:
    """`{connector: [raw relation, ...]}` -- what each module's DDL creates."""
    from core.raw_table_provisioning import declared_raw_tables  # noqa: PLC0415

    declared = declared_raw_tables(modules_dir)
    found: dict[str, list[str]] = {}
    for connector, statements in declared.items():
        names = sorted({name for name in map(_relation_name, statements) if name})
        if names:
            found[connector] = names
    return found


def declared_staging_models(
    modules_dir: str | Path | None = None,
) -> dict[str, dict[str, list[str]]]:
    """`{connector: {raw relation: [staging model, ...]}}` -- read from the dbt SQL.

    The staging models are the dbt project's own declaration of what reads what;
    this walks them so a `staging_relation` in a manifest can be CHECKED against
    the model that exists, and so `staging_model_absent` is a measurement rather
    than an opinion.
    """
    root = Path(modules_dir) if modules_dir else _MODULES_DIR
    found: dict[str, dict[str, list[str]]] = {}
    for model in sorted(root.glob("*/dbt/staging/*.sql")):
        connector = model.parents[2].name
        name = model.stem
        try:
            text = model.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 -- an unreadable model is not a crash
            logger.warning("stage_relation_resolver: %s unreadable (%s)", model, exc)
            continue
        for _schema, relation in set(_SOURCE.findall(text)):
            found.setdefault(connector, {}).setdefault(relation, []).append(name)
    for relations in found.values():
        for models in relations.values():
            models.sort()
    return found


def declared_profile_relations(
    modules_dir: str | Path | None = None,
) -> dict[str, dict[str, dict[str, str | None]]]:
    """`{connector: {profile id: {raw_relation, staging_relation}}}` from the manifests.

    A profile that carries neither key is reported with both `None` AND is what
    the conformance guard fails on: an absent key and a declared `null` are two
    different statements, and only the second one is a decision somebody made.
    """
    root = Path(modules_dir) if modules_dir else _MODULES_DIR
    found: dict[str, dict[str, dict[str, str | None]]] = {}
    for manifest in sorted(root.glob("*/manifest.json")):
        connector = manifest.parent.name
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # noqa: BLE001
            logger.warning("stage_relation_resolver: %s unreadable (%s)", manifest, exc)
            continue
        profiles = payload.get("report_profiles")
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict) or not profile.get("id"):
                continue
            found.setdefault(connector, {})[str(profile["id"])] = {
                RAW_RELATION_KEY: _text_or_none(profile.get(RAW_RELATION_KEY)),
                STAGING_RELATION_KEY: _text_or_none(profile.get(STAGING_RELATION_KEY)),
            }
    return found


def declared_profile_keys(
    modules_dir: str | Path | None = None,
) -> dict[str, dict[str, set[str]]]:
    """`{connector: {profile id: {keys the entry actually carries}}}`.

    Separate from `declared_profile_relations` because that one cannot tell a
    missing key from a declared `null`, and the guard has to.
    """
    root = Path(modules_dir) if modules_dir else _MODULES_DIR
    found: dict[str, dict[str, set[str]]] = {}
    for manifest in sorted(root.glob("*/manifest.json")):
        connector = manifest.parent.name
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # noqa: BLE001
            continue
        profiles = payload.get("report_profiles")
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if isinstance(profile, dict) and profile.get("id"):
                found.setdefault(connector, {})[str(profile["id"])] = set(profile)
    return found


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict, dict]:
    """The two declarations, read once per process.

    They come from files that only a deploy changes, and the read is a few dozen
    module imports; paying it on every opened day would make the cheapest half of
    this surface the slowest.
    """
    return declared_profile_relations(), declared_staging_models()


def reset_cache() -> None:
    """Test seam: forget the parsed declarations."""
    _catalog.cache_clear()


# ---------------------------------------------------------------------------
# The resolution itself.
# ---------------------------------------------------------------------------


def resolve_stage_relations(
    *,
    connector: str | None,
    report_profile_id: str | None,
    modules_dir: str | Path | None = None,
) -> dict[str, Any]:
    """The `(collected, mapped)` pair of one Datastream, or the named absence.

    Never a composed name: every value returned here was written in a manifest by
    the module that lands the rows, and a profile this resolver does not know
    answers an absence rather than the relation of the profile beside it.
    """
    if modules_dir is None:
        profiles, staging = _catalog()
    else:
        profiles = declared_profile_relations(modules_dir)
        staging = declared_staging_models(modules_dir)

    module = (connector or "").strip()
    profile_id = (report_profile_id or "").strip()

    if not profile_id:
        return _absent(module, None, REPORT_PROFILE_NOT_SET)

    declared = (profiles.get(module) or {}).get(profile_id)
    raw_relation = (declared or {}).get(RAW_RELATION_KEY)
    if not raw_relation:
        # A profile the manifest does not carry lands here too, and it must: the
        # only alternative is to answer with the relation of some OTHER profile.
        return _absent(module, profile_id, RAW_RELATION_NOT_DECLARED)

    staging_relation = (declared or {}).get(STAGING_RELATION_KEY)
    readers = (staging.get(module) or {}).get(raw_relation) or []
    if not staging_relation:
        reason = STAGING_MODEL_ABSENT if not readers else STAGING_RELATION_NOT_DECLARED
        return {
            "connector": module or None,
            "report_profile_id": profile_id,
            "collected_relation": raw_relation,
            "mapped_relation": None,
            "reason": reason,
            "message": _MESSAGES[reason],
        }

    return {
        "connector": module or None,
        "report_profile_id": profile_id,
        "collected_relation": raw_relation,
        "mapped_relation": staging_relation,
        "reason": None,
        "message": None,
    }


def _absent(connector: str, profile_id: str | None, reason: str) -> dict[str, Any]:
    return {
        "connector": connector or None,
        "report_profile_id": profile_id,
        "collected_relation": None,
        "mapped_relation": None,
        "reason": reason,
        "message": _MESSAGES[reason],
    }
