"""Project Settings control-plane lifecycle (Story 46.3).

Settings owns Project intent and immutable configuration references. Data and
Governance remain the owners of detailed proposals and evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from core import business_identity_catalogue as catalogue
from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_PROJECT_SETTINGS_ACTIVATE = declare_action("project.settings.activate")


PROJECT_SETTINGS_COMMAND = ACTION_PROJECT_SETTINGS_ACTIVATE
CAPABILITY_SPECS: OrderedDict[str, dict[str, Any]] = OrderedDict(
    (
        ("country", {"availability": "optional", "dependencies": []}),
        ("currency_fx", {"availability": "always_present", "dependencies": []}),
        ("reporting_timezone", {"availability": "always_present", "dependencies": []}),
        ("tax_fees", {"availability": "optional", "dependencies": ["currency_fx"]}),
        ("competitors", {"availability": "optional", "dependencies": []}),
        # The sixth, story 61.5. LAST, and that position is load-bearing: the
        # reader of the Settings envelope orders its rows with a CASE whose ranks
        # are written below, and `validate_capability_ledger` compares the result
        # to this order. `optional` and `currency_fx` are both ratified in
        # `docs/product-architecture/project-settings.md`: "Placement Mapping |
        # Optional and Disabled by default […] Currency & FX is required for any
        # planned-versus-actual figure".
        ("placement_mapping", {"availability": "optional", "dependencies": ["currency_fx"]}),
        # The seventh, story 70.3. LAST for the same load-bearing reason as the
        # sixth, and `optional` because the capability is off by default:
        # `docs/product-architecture/capabilities/analytics-alignment.md`
        # ("off by default", decision 3). `currency_fx` is the only dependency
        # expressible HERE -- this list holds capability-to-capability edges. The
        # other two, an MDM common key and a published Semantic View
        # relationship, are not capabilities and are resolved by
        # `core.analytics_alignment.resolve_dependencies`, which names each
        # missing one with the gesture that meets it.
        ("analytics_alignment", {"availability": "optional", "dependencies": ["currency_fx"]}),
    )
)
_COVERAGE_KEYS = ("applicable", "complete", "partial", "unavailable", "excluded", "pending")


class ProjectSettingsValidationError(ValueError):
    code = "invalid_project_settings"


class ProjectSettingsConflict(RuntimeError):
    code = "change_set_conflict"


class ProjectSettingsStale(RuntimeError):
    code = "stale_change_set"


class ProjectSettingsBlocked(RuntimeError):
    code = "change_set_blocked"


@dataclass(frozen=True, slots=True)
class ProjectSettingsConfirmation:
    confirmation_id: str
    confirmation_secret: str
    expires_at: Any
    prepared_payload_hash: str


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ProjectSettingsValidationError("value must be JSON serializable") from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


#: The posture every Project starts from. `confirm_change_set` computes exactly
#: this when a Project has no prior version, so version 1 is not an invention:
#: it is the state the first change set would have applied its intent to.
_BASELINE_POSTURE: dict[str, Any] = {"defaults": {}, "capabilities": {}}


def ensure_active_configuration_version(
    conn, project_id: str, actor_person_id: str = "system"
) -> str:
    """Give a Project its version 1, and point the Project at it. Idempotent.

    A PROJECT WITHOUT ONE CANNOT FINISH THE ADD DATASTREAM FUNNEL. Measured
    2026-08-11 by walking the whole funnel against production: Source, discovery
    and the compiled proposal all succeed, then `POST .../previews` answers 422
    `An active Project configuration version is required` (`_preview_pins`), and
    the final review refuses for want of the preview. Nothing on the way says so
    and no screen offers the gesture, because there is no gesture: a version was
    only ever written by `confirm_change_set`, in Project Settings, as the side
    effect of applying an unrelated change.

    So the Projects created through the console -- every one of them -- carried
    `active_configuration_version_id = NULL` and could never activate a
    Datastream. Only the QA harness's Projects had one, which is precisely why
    the suite stayed green over a funnel that was broken for a person.

    Version 1 is derived, never decided: the baseline posture, hashed the same
    way `confirm_change_set` hashes it, so the next change set chains onto it
    instead of re-minting a first version with the same content.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT active_configuration_version_id FROM app.projects WHERE id = %s FOR UPDATE",
            (project_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ProjectSettingsValidationError("Project not found")
        if row[0]:
            return str(row[0])

        content_hash = canonical_hash(_BASELINE_POSTURE)
        cur.execute(
            "SELECT id FROM app.project_configuration_versions "
            "WHERE project_id = %s AND content_hash = %s",
            (project_id, content_hash),
        )
        existing = cur.fetchone()
        if existing:
            version_id = str(existing[0])
        else:
            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 "
                "FROM app.project_configuration_versions WHERE project_id = %s",
                (project_id,),
            )
            version_number = int(cur.fetchone()[0])
            version_id = f"pcfg_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.project_configuration_versions
                    (id, project_id, version_number, posture, dependency_fingerprint,
                     content_hash, previous_version_id, activated_by)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, NULL, %s)
                """,
                (
                    version_id,
                    project_id,
                    version_number,
                    _canonical_json(_BASELINE_POSTURE),
                    canonical_hash({"project_id": project_id, "baseline": True}),
                    content_hash,
                    actor_person_id,
                ),
            )
        cur.execute(
            "UPDATE app.projects SET active_configuration_version_id = %s, updated_at = NOW() "
            "WHERE id = %s",
            (version_id, project_id),
        )
    return version_id


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


#: Ce que `_coverage` AJOUTE aux comptes. Nommes ici parce que la fonction doit
#: accepter sa propre sortie : le lecteur range `_coverage(...)` dans
#: `capabilities[i]["coverage"]`, puis `validate_capability_ledger` revalide ce
#: meme dict. Une fonction qui refuse ce qu'elle vient de produire rendait
#: `invalid_project_settings: coverage keys are inconsistent` sur TOUS les
#: projets -- Project Settings > General etait mort, pas degrade (2026-08-04).
_DERIVED_COVERAGE_KEYS = ("label", "percentage")


def _coverage(value: dict[str, Any]) -> dict[str, Any]:
    """Valider les comptes bruts et en deriver le libelle.

    IDEMPOTENTE, et plus stricte pour autant : si la valeur porte deja `label` et
    `percentage`, ils doivent CONCORDER avec ce que les comptes donnent. Tolerer
    une paire derivee sans la verifier laisserait passer un libelle perime --
    exactement le genre de vert qui ment.
    """
    if not isinstance(value, dict):
        raise ProjectSettingsValidationError("coverage must be an object")
    present = set(value)
    if (present - set(_COVERAGE_KEYS) - set(_DERIVED_COVERAGE_KEYS)) or (
        set(_COVERAGE_KEYS) - present
    ):
        raise ProjectSettingsValidationError("coverage keys are inconsistent")
    counts: dict[str, int] = {}
    for key in _COVERAGE_KEYS:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ProjectSettingsValidationError("coverage counts must be non-negative integers")
        counts[key] = item
    accounted = sum(counts[key] for key in _COVERAGE_KEYS if key != "applicable")
    if accounted != counts["applicable"]:
        raise ProjectSettingsValidationError("coverage counts do not match the denominator")
    if counts["applicable"] == 0:
        derived = {**counts, "label": "Not applicable", "percentage": None}
    else:
        percentage = round((counts["complete"] / counts["applicable"]) * 100, 1)
        derived = {**counts, "label": f"{percentage:g}% complete", "percentage": percentage}
    # Une paire derivee deja presente est VERIFIEE, pas ignoree : un libelle qui
    # ne correspond plus a ses comptes est un chiffre qui ment, et il serait
    # affiche tel quel.
    for key in _DERIVED_COVERAGE_KEYS:
        if key in value and value[key] != derived[key]:
            raise ProjectSettingsValidationError("coverage label does not match its counts")
    return derived


def validate_capability_ledger(rows: list[dict[str, Any]]) -> None:
    if not isinstance(rows, list) or len(rows) != len(CAPABILITY_SPECS):
        # The count is DERIVED, not spelled. The comparison above has always been
        # `len(CAPABILITY_SPECS)`; only this sentence said "five", and a message
        # that contradicts the rule it reports sends a reader looking for a
        # cardinality check that does not exist.
        raise ProjectSettingsValidationError(
            f"exactly {len(CAPABILITY_SPECS)} capabilities are required"
        )
    keys = [row.get("key") for row in rows]
    if keys != list(CAPABILITY_SPECS) or len(set(keys)) != len(keys):
        raise ProjectSettingsValidationError("capability keys are missing, duplicated or unordered")
    for row in rows:
        spec = CAPABILITY_SPECS[row["key"]]
        if row.get("availability") != spec["availability"]:
            raise ProjectSettingsValidationError("capability availability drifted")
        _coverage(row.get("coverage"))


def _validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Validate Project-owned intent -- and nothing a client must not author.

    Story 48.1 narrowed this deliberately. A caller states what it wants and, when
    a capability needs one, which governed object it picked by stable id. It never
    supplies coverage, blockers, exceptions, hashes, owner routes or evidence:
    those are compiled by the server during prepare, which is what makes the
    authority boundary real rather than shape-validated.
    """
    if not isinstance(intent, dict) or not intent:
        raise ProjectSettingsValidationError("intent must be a non-empty object")
    if not set(intent).issubset({"defaults", "capabilities", "selected_owner_ids"}):
        raise ProjectSettingsValidationError("unsupported Project-owned intent")
    defaults = intent.get("defaults", {})
    if not isinstance(defaults, dict) or not set(defaults).issubset(
        {"reporting_currency", "reporting_timezone", "verification_source"}
    ):
        raise ProjectSettingsValidationError("unsupported Project default")
    capabilities = intent.get("capabilities", {})
    if not isinstance(capabilities, dict) or not set(capabilities).issubset(CAPABILITY_SPECS):
        raise ProjectSettingsValidationError("unsupported Project capability")
    for key, state in capabilities.items():
        if state not in {"enabled", "disabled"}:
            raise ProjectSettingsValidationError("capability intent must be enabled or disabled")
        if CAPABILITY_SPECS[key]["availability"] == "always_present" and state == "disabled":
            raise ProjectSettingsValidationError(f"{key} is always present")
    return {
        "defaults": defaults,
        "capabilities": capabilities,
        "selected_owner_ids": _validate_selected_owner_ids(intent.get("selected_owner_ids", {})),
    }


def _validate_selected_owner_ids(value: Any) -> dict[str, list[str]]:
    """Accept stable owner IDs only: no route, no version, no evidence hash."""
    if not isinstance(value, dict):
        raise ProjectSettingsValidationError("selected_owner_ids must be an object")
    if not set(value).issubset(CAPABILITY_SPECS):
        raise ProjectSettingsValidationError("selected_owner_ids names an unknown capability")
    selected: dict[str, list[str]] = {}
    for capability_key, ids in value.items():
        if not isinstance(ids, list) or not all(
            isinstance(item, str) and 0 < len(item.strip()) <= 64 for item in ids
        ):
            raise ProjectSettingsValidationError(
                "selected_owner_ids must be arrays of stable identifier strings"
            )
        selected[capability_key] = sorted({item.strip() for item in ids})
    return selected


def prepare_change_payload(
    *,
    intent: dict[str, Any],
    proposals: list[dict[str, Any]],
    exception_references: list[dict[str, Any]],
    active_configuration_version_id: str | None,
    dependency_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Compose the frozen review payload from server-compiled proposals only.

    Coverage is counted here from the exact proposal rows -- the six counts, the
    applicable denominator and, when nothing is applicable, ``Not applicable``.
    The previous implementation derived coverage from whichever owner reference
    happened to be last and reported every active Datastream as pending; nothing
    in this payload is caller-authored any more.
    """
    from core.capability_proposals import (
        aggregate_coverage,
        required_blockers,
        settings_coverage,
    )

    normalized_intent = _validate_intent(intent)
    by_capability: dict[str, list[dict[str, Any]]] = {key: [] for key in CAPABILITY_SPECS}
    for proposal in proposals:
        by_capability.setdefault(proposal["capability_key"], []).append(proposal)
    coverage = {
        key: _coverage(
            settings_coverage(
                aggregate_coverage([row["coverage_state"] for row in by_capability.get(key, [])])
            )
        )
        for key in CAPABILITY_SPECS
    }
    referenced_versions = _server_owner_references(proposals)
    return {
        "diff": normalized_intent,
        "impact": {
            "coverage": coverage,
            "matrix": _impact_matrix(proposals),
        },
        "dependency_snapshot": dependency_snapshot,
        "dependency_fingerprint": canonical_hash(dependency_snapshot),
        "referenced_versions": referenced_versions,
        "exceptions": exception_references,
        "blockers": required_blockers(proposals),
        "rollback_target": active_configuration_version_id,
    }


def _server_owner_references(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compose the owner reference set from proposals, keyed by stable identity."""
    from core.capability_proposals import datastream_owner_reference

    references: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        data_ref = {
            "owner_kind": "data",
            "object_type": "datastream",
            "object_id": proposal["datastream_id"],
            "version_id": proposal.get("id") or proposal["content_hash"],
            "evidence_hash": proposal["content_hash"],
            "capability_key": proposal["capability_key"],
            "owner_reference": datastream_owner_reference(proposal["datastream_id"]),
        }
        references[canonical_hash(data_ref)] = data_ref
        for ref in proposal.get("governance_owner_references", []):
            references[canonical_hash(ref)] = ref
    return sorted(
        references.values(),
        key=lambda item: (
            item["owner_kind"],
            item["object_type"],
            item["object_id"],
            item["version_id"],
        ),
    )


def _impact_matrix(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (capability, Datastream): what a reviewer has to read."""
    return sorted(
        (
            {
                "capability_key": proposal["capability_key"],
                "datastream_id": proposal["datastream_id"],
                "applicability": proposal["applicability"],
                "coverage_state": proposal["coverage_state"],
                "reason": proposal["coverage_reason"],
                "changes_grain": bool(
                    proposal["impact"]["grain_before_after"].get("changes_grain")
                ),
                "backfill_required": bool(proposal["impact"]["backfill"].get("required")),
                "downstream_consumers": proposal["impact"]["fan_out"].get(
                    "downstream_consumers", 0
                ),
                "blocker_count": len(proposal["blocker_references"]),
                "exception_count": len(proposal["exception_references"]),
                "proposal_id": proposal.get("id"),
                "content_hash": proposal["content_hash"],
            }
            for proposal in proposals
        ),
        key=lambda item: (item["capability_key"], item["datastream_id"]),
    )


def validate_confirmation(
    prepared_payload: dict[str, Any], live_snapshot: dict[str, Any]
) -> None:
    """Refuse a confirmation whose world moved, and say exactly which part moved."""
    moved = detect_drift(prepared_payload.get("dependency_snapshot") or {}, live_snapshot)
    if moved:
        raise ProjectSettingsStale(
            "Review is stale: " + ", ".join(moved) + " changed after prepare"
        )
    if prepared_payload.get("blockers"):
        raise ProjectSettingsBlocked("Project Settings change has unresolved blockers")


#: Every dimension a confirmation must recheck, in the order a reader would
#: expect to see it named when one of them has moved.
DRIFT_DIMENSIONS: tuple[str, ...] = (
    "active_configuration_version_id",
    "project_configuration",
    "datastream_set",
    "governance_owners",
    "proposals",
)


def _active_posture(conn, project_id: str, active: str | None) -> dict[str, Any]:
    if not active:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT posture FROM app.project_configuration_versions
            WHERE project_id = %s AND id = %s
            """,
            (project_id, active),
        )
        row = cur.fetchone()
    return _json(row[0], {}) if row else {}


def _datastream_set(conn, project_id: str) -> list[dict[str, Any]]:
    """Pin the exact Datastream set and every version a proposal was compiled on.

    Connector contract, source schema, plan, mapping and publication all belong
    here: a review that froze only plan and mapping could be confirmed after the
    source schema changed underneath it, which is the drift this closes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.lifecycle_state, d.module_name, d.source_kind,
                   d.connection_ref_id, d.current_plan_version_id,
                   d.current_mapping_version_id, d.current_published_execution_id,
                   plan.content_hash, plan.capability_fingerprint,
                   mapping.source_schema_hash, mapping.content_hash
            FROM app.datastreams d
            LEFT JOIN app.datastream_plan_versions plan
                   ON plan.id = d.current_plan_version_id
                  AND plan.datastream_id = d.id AND plan.project_id = d.project_id
            LEFT JOIN app.datastream_mapping_versions mapping
                   ON mapping.id = d.current_mapping_version_id
                  AND mapping.datastream_id = d.id AND mapping.project_id = d.project_id
            WHERE d.project_id = %s AND d.archived_at IS NULL
            ORDER BY d.id
            """,
            (project_id,),
        )
        return [
            {
                "id": row[0],
                "lifecycle_state": row[1],
                "connector": {
                    "module_name": row[2],
                    "source_kind": row[3],
                    "connection_ref_id": row[4],
                    "capability_fingerprint": row[9],
                },
                "plan_version_id": row[5],
                "plan_content_hash": row[8],
                "mapping_version_id": row[6],
                "mapping_content_hash": row[11],
                "source_schema_hash": row[10],
                "published_execution_id": row[7],
            }
            for row in cur.fetchall()
        ]


def _dependency_snapshot(
    conn,
    project_id: str,
    *,
    intent: dict[str, Any],
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compose the complete snapshot a prepare freezes and a confirm rechecks."""
    from core.capability_proposals import proposal_fingerprints

    with conn.cursor() as cur:
        cur.execute(
            "SELECT active_configuration_version_id FROM app.projects WHERE id = %s",
            (project_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ProjectSettingsValidationError("Project not found")
        active = row[0]
        cur.execute(
            """
            SELECT capability_key, availability, state, active_version_id
            FROM app.project_capabilities WHERE project_id = %s ORDER BY capability_key
            """,
            (project_id,),
        )
        capability_rows = [
            {
                "capability_key": item[0],
                "availability": item[1],
                "state": item[2],
                "active_version_id": item[3],
            }
            for item in cur.fetchall()
        ]
    posture = _active_posture(conn, project_id, active)
    governance_owners: list[dict[str, Any]] = []
    for proposal in proposals or []:
        for ref in proposal.get("governance_owner_references", []):
            governance_owners.append(
                {
                    "object_type": ref["object_type"],
                    "object_id": ref["object_id"],
                    "version_id": ref["version_id"],
                    "evidence_hash": ref["evidence_hash"],
                }
            )
    unique_owners = sorted(
        {canonical_hash(item): item for item in governance_owners}.values(),
        key=lambda item: (item["object_type"], item["object_id"], item["version_id"]),
    )
    return {
        "active_configuration_version_id": active,
        "project_configuration": {
            "posture": posture,
            "capabilities": capability_rows,
            "requested": {
                "defaults": intent.get("defaults", {}),
                "capabilities": intent.get("capabilities", {}),
                "selected_owner_ids": intent.get("selected_owner_ids", {}),
            },
        },
        "datastream_set": _datastream_set(conn, project_id),
        "governance_owners": unique_owners,
        "proposals": proposal_fingerprints(proposals or []),
    }


def detect_drift(prepared_snapshot: dict[str, Any], live_snapshot: dict[str, Any]) -> list[str]:
    """Name every dimension that moved -- never a single opaque 'stale'.

    ``proposals`` is compared by the frozen content hashes only. A live recompile
    is deliberately not performed here: prepare is the compile step, and silently
    recompiling at confirmation time is exactly how a human ends up approving one
    impact and committing another.
    """
    moved: list[str] = []
    for dimension in DRIFT_DIMENSIONS:
        if canonical_hash(prepared_snapshot.get(dimension)) != canonical_hash(
            live_snapshot.get(dimension)
        ):
            moved.append(dimension)
    return moved


def create_change_set(
    conn,
    *,
    project_id: str,
    intent: dict[str, Any],
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create a Draft from intent alone.

    Owner references are no longer accepted here. They are composed by the server
    during prepare, from the exact proposals it compiles -- so a caller can no
    longer name an owner, a version or an evidence hash the server never resolved.
    """
    normalized_intent = _validate_intent(intent)
    key_hash = canonical_hash(idempotency_key)
    request_hash = canonical_hash({"intent": normalized_intent})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, intent, state
            FROM app.project_change_sets
            WHERE project_id = %s AND idempotency_key_hash = %s
            """,
            (project_id, key_hash),
        )
        existing = cur.fetchone()
        if existing:
            if canonical_hash({"intent": _json(existing[1], {})}) != request_hash:
                raise ProjectSettingsConflict("idempotency key is bound to another Change Set")
            return {"id": existing[0], "state": existing[2], "idempotent_replay": True}
        change_set_id = f"pcset_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.project_change_sets
                (id, project_id, intent, owner_references, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s::jsonb, '[]'::jsonb, %s, %s)
            """,
            (change_set_id, project_id, _canonical_json(normalized_intent), key_hash, actor),
        )
    return {"id": change_set_id, "state": "draft", "idempotent_replay": False}


def _load_change_set(
    conn, project_id: str, change_set_id: str, *, for_update: bool = False
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, state, intent, owner_references, prepared_payload,
                   prepared_payload_hash, dependency_fingerprint, rollback_version_id,
                   activated_version_id, created_by, created_at, prepared_at, confirmed_at
            FROM app.project_change_sets WHERE id = %s AND project_id = %s
            """
            + suffix,
            (change_set_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    keys = (
        "id",
        "project_id",
        "state",
        "intent",
        "owner_references",
        "prepared_payload",
        "prepared_payload_hash",
        "dependency_fingerprint",
        "rollback_version_id",
        "activated_version_id",
        "created_by",
        "created_at",
        "prepared_at",
        "confirmed_at",
    )
    result = dict(zip(keys, row))
    for key, default in (("intent", {}), ("owner_references", []), ("prepared_payload", None)):
        result[key] = _json(result[key], default)
    return result


def _project_org_id(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None:
        raise ProjectSettingsValidationError("Project not found")
    return str(row[0])


def intended_posture(conn, project_id: str, intent: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return the posture this intent would activate, and the hash identifying it.

    The Configuration Version does not exist yet at prepare time, so a proposal
    pins the content hash that will identify it. ``project_configuration_versions``
    is unique on ``(project_id, content_hash)``, so that pin resolves to exactly
    one version once activation mints it -- an exact reference, not a guess.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT active_configuration_version_id FROM app.projects WHERE id = %s",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ProjectSettingsValidationError("Project not found")
    current = _active_posture(conn, project_id, row[0]) or {"defaults": {}, "capabilities": {}}
    posture = _apply_intent(current, intent)
    return posture, canonical_hash(posture)


def _compile_all_proposals(
    conn,
    *,
    project_id: str,
    org_id: str,
    change_set_id: str,
    intent: dict[str, Any],
    actor: str,
    loaded_modules: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Compile every capability the intent touches, plus the always-present two.

    Currency & FX and Reporting Timezone have no activation toggle, so any change
    to this Project's posture recompiles them: they are foundations, not modules a
    Change Set may leave uncounted.
    """
    from core.capability_proposals import compile_capability_proposals

    with conn.cursor() as cur:
        cur.execute(
            "SELECT active_configuration_version_id FROM app.projects WHERE id = %s",
            (project_id,),
        )
        base_version_id = (cur.fetchone() or (None,))[0]
    posture, intended_hash = intended_posture(conn, project_id, intent)
    requested = dict(intent.get("capabilities") or {})
    active_capabilities = posture.get("capabilities") or {}
    keys = [
        key
        for key, spec in CAPABILITY_SPECS.items()
        if spec["availability"] == "always_present"
        or key in requested
        or active_capabilities.get(key) == "enabled"
    ]
    proposals: list[dict[str, Any]] = []
    for capability_key in keys:
        state = requested.get(capability_key) or active_capabilities.get(capability_key)
        if CAPABILITY_SPECS[capability_key]["availability"] == "always_present":
            state = "enabled"
        proposals.extend(
            compile_capability_proposals(
                conn,
                org_id=org_id,
                project_id=project_id,
                change_set_id=change_set_id,
                capability_key=capability_key,
                requested_state=str(state or "disabled"),
                intent=intent,
                base_configuration_version_id=base_version_id,
                intended_configuration_content_hash=intended_hash,
                actor=actor,
                loaded_modules=loaded_modules,
            )
        )
    return proposals


def prepare_change_set(
    conn,
    *,
    project_id: str,
    change_set_id: str,
    actor: str = "project-settings",
    loaded_modules: list[Any] | None = None,
) -> dict[str, Any]:
    """Compile, persist and freeze. Prepare is non-authorizing.

    It returns a non-secret review reference: the prepared payload hash. Nothing
    it returns can be replayed as approval -- authorization needs the separate
    single-use confirmation ceremony.
    """
    from core.capability_proposals import persist_exceptions, persist_proposals

    change_set = _load_change_set(conn, project_id, change_set_id, for_update=True)
    if change_set is None:
        raise ProjectSettingsValidationError("Change Set not found")
    if change_set["state"] != "draft":
        if change_set["prepared_payload"] is not None:
            return {
                **change_set["prepared_payload"],
                "prepared_payload_hash": change_set["prepared_payload_hash"],
                "review_reference": change_set["prepared_payload_hash"],
                "authorizing": False,
                "idempotent_replay": True,
            }
        raise ProjectSettingsConflict("Change Set is not draft")

    org_id = _project_org_id(conn, project_id)
    compiled = _compile_all_proposals(
        conn,
        project_id=project_id,
        org_id=org_id,
        change_set_id=change_set_id,
        intent=change_set["intent"],
        actor=actor,
        loaded_modules=loaded_modules,
    )
    stored = persist_proposals(
        conn, project_id=project_id, change_set_id=change_set_id, proposals=compiled, actor=actor
    )
    exception_references = persist_exceptions(
        conn,
        org_id=org_id,
        project_id=project_id,
        change_set_id=change_set_id,
        proposals=stored,
        actor=actor,
    )
    snapshot = _dependency_snapshot(
        conn, project_id, intent=change_set["intent"], proposals=stored
    )
    payload = prepare_change_payload(
        intent=change_set["intent"],
        proposals=stored,
        exception_references=exception_references,
        active_configuration_version_id=snapshot["active_configuration_version_id"],
        dependency_snapshot=snapshot,
    )
    payload_hash = canonical_hash(payload)
    with conn.cursor() as cur:
        # References land while the parent is still Draft: migration 131 makes them
        # immutable from the moment it leaves that state.
        for ref in payload["referenced_versions"]:
            cur.execute(
                """
                INSERT INTO app.project_change_set_references
                    (change_set_id, project_id, owner_kind, owner_object_type,
                     owner_object_id, owner_version_id, owner_route, evidence_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    change_set_id,
                    project_id,
                    ref["owner_kind"],
                    ref["object_type"],
                    ref["object_id"],
                    ref["version_id"],
                    _canonical_json(ref["owner_reference"]),
                    ref["evidence_hash"],
                ),
            )
        cur.execute(
            """
            UPDATE app.project_change_sets
            SET state = %s, owner_references = %s::jsonb, prepared_payload = %s::jsonb,
                prepared_payload_hash = %s, dependency_fingerprint = %s,
                rollback_version_id = %s, prepared_at = NOW(), updated_at = NOW()
            WHERE id = %s AND project_id = %s AND state = 'draft'
            """,
            (
                "blocked" if payload["blockers"] else "prepared",
                _canonical_json(payload["referenced_versions"]),
                _canonical_json(payload),
                payload_hash,
                payload["dependency_fingerprint"],
                payload["rollback_target"],
                change_set_id,
                project_id,
            ),
        )
        if cur.rowcount != 1:
            raise ProjectSettingsConflict("Change Set prepare raced")
        for capability_key in {proposal["capability_key"] for proposal in stored} | set(
            change_set["intent"].get("capabilities", {})
        ):
            cur.execute(
                """
                UPDATE app.project_capabilities
                SET pending_change_set_id = %s, updated_at = NOW()
                WHERE project_id = %s AND capability_key = %s
                """,
                (change_set_id, project_id, capability_key),
            )
    return {
        **payload,
        "prepared_payload_hash": payload_hash,
        "review_reference": payload_hash,
        "authorizing": False,
        "idempotent_replay": False,
    }


def issue_change_confirmation(
    conn,
    *,
    project_id: str,
    org_id: str,
    change_set_id: str,
    actor_person_id: str,
    idempotency_key: str,
) -> ProjectSettingsConfirmation:
    from core.entry_confirmations import issue_entry_confirmation

    change_set = _load_change_set(conn, project_id, change_set_id)
    if change_set is None or change_set["state"] != "prepared":
        raise ProjectSettingsBlocked("Only an unblocked prepared Change Set can be confirmed")
    payload = {
        "project_id": project_id,
        "org_id": org_id,
        "change_set_id": change_set_id,
        "prepared_payload_hash": change_set["prepared_payload_hash"],
        "dependency_fingerprint": change_set["dependency_fingerprint"],
    }
    issued = issue_entry_confirmation(
        conn,
        actor_person_id=actor_person_id,
        command_type=PROJECT_SETTINGS_COMMAND,
        request_payload=payload,
        idempotency_key=idempotency_key,
        context_reference=f"organization:{org_id}/project:{project_id}/settings/changes/{change_set_id}",
    )
    return ProjectSettingsConfirmation(
        confirmation_id=issued.confirmation_id,
        confirmation_secret=issued.confirmation_secret,
        expires_at=issued.expires_at,
        prepared_payload_hash=change_set["prepared_payload_hash"],
    )


def _apply_intent(current: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(_canonical_json(current))
    result.setdefault("defaults", {}).update(intent.get("defaults", {}))
    result.setdefault("capabilities", {}).update(intent.get("capabilities", {}))
    return result


def read_change_set_impact(conn, *, project_id: str, change_set_id: str) -> dict[str, Any]:
    """Return the exact frozen impact of a prepared Change Set.

    A read never recompiles. What a reviewer opens is what a confirmation will be
    bound to, which is the only way the two can be the same decision.
    """
    from core.capability_proposals import read_exceptions

    change_set = _load_change_set(conn, project_id, change_set_id)
    if change_set is None:
        raise ProjectSettingsValidationError("Change Set not found")
    payload = change_set["prepared_payload"]
    if payload is None:
        raise ProjectSettingsConflict("Change Set has not been prepared")
    proposals = _load_frozen_proposals(conn, project_id, change_set_id)
    return {
        "schema": "project_change_set_impact.v1",
        "project_id": project_id,
        "change_set_id": change_set_id,
        "state": change_set["state"],
        "diff": payload.get("diff"),
        "coverage": (payload.get("impact") or {}).get("coverage"),
        "matrix": (payload.get("impact") or {}).get("matrix"),
        "referenced_versions": payload.get("referenced_versions", []),
        "blockers": payload.get("blockers", []),
        "exceptions": read_exceptions(conn, project_id=project_id),
        "proposal_count": len(proposals),
        "review_reference": change_set["prepared_payload_hash"],
        "dependency_fingerprint": change_set["dependency_fingerprint"],
        "authorizing": False,
        "prepared_at": change_set["prepared_at"].isoformat()
        if change_set["prepared_at"]
        else None,
        "confirmed_at": change_set["confirmed_at"].isoformat()
        if change_set["confirmed_at"]
        else None,
    }


def _intended_hashes(conn, project_id: str, change_set_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT intended_configuration_content_hash
            FROM app.datastream_capability_proposals
            WHERE project_id = %s AND change_set_id = %s
            """,
            (project_id, change_set_id),
        )
        return [{"intended_configuration_content_hash": row[0]} for row in cur.fetchall()]


def _coverage_states_by_capability(proposals: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for proposal in proposals:
        grouped.setdefault(proposal["capability_key"], []).append(proposal["coverage_state"])
    return grouped


def _activated_capability_state(states: list[str]) -> str:
    """Map exact coverage onto the ratified capability status vocabulary.

    ``Ready`` is not the default. A capability whose applicable Datastreams are
    only partly covered is ``Degraded`` -- the active version remains usable, and
    the named gap limits what it promises.
    """
    applicable = [state for state in states if state != "not_applicable"]
    if not applicable:
        return "ready"  # nothing applicable: the posture is satisfied, honestly
    if any(state in {"unavailable", "excluded"} for state in applicable):
        return "degraded"
    if any(state in {"partial", "pending"} for state in applicable):
        return "degraded"
    return "ready"


def _load_frozen_proposals(conn, project_id: str, change_set_id: str) -> list[dict[str, Any]]:
    """Re-read the exact proposals this Change Set froze, in their stored form."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, datastream_id, capability_key, content_hash, dependency_fingerprint,
                   governance_owner_references, coverage_state, applicability,
                   impact, dependency_snapshot, current_plan_version_id
            FROM app.datastream_capability_proposals
            WHERE project_id = %s AND change_set_id = %s
            ORDER BY capability_key, datastream_id
            """,
            (project_id, change_set_id),
        )
        return [
            {
                "id": row[0],
                "datastream_id": row[1],
                "capability_key": row[2],
                "content_hash": row[3],
                "dependency_fingerprint": row[4],
                "governance_owner_references": _json(row[5], []),
                "coverage_state": row[6],
                "applicability": row[7],
                "impact": _json(row[8], {}),
                "dependency_snapshot": _json(row[9], {}),
                "current_plan_version_id": row[10],
            }
            for row in cur.fetchall()
        ]


def confirm_change_set(
    conn,
    *,
    project_id: str,
    org_id: str,
    change_set_id: str,
    actor_person_id: str,
    confirmation_id: str,
    confirmation_secret: str | None,
    prepared_payload_hash: str,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
    loaded_modules: list[Any] | None = None,
) -> dict[str, Any]:
    """Activate a reviewed Change Set. One command, two trusted surfaces.

    The console presents the confirmation secret it was issued. A trusted MCP
    surface presents none: ``confirmation_secret=None`` selects the presence-bound
    path, where the server already resolved the confirmation from evidence it
    minted itself. Both consume the same single-use record through the same
    binding rechecks -- there is no second write path and no weaker one.
    """
    from core.entry_confirmations import (
        bind_entry_confirmation_operation,
        consume_entry_confirmation,
        consume_presence_bound_confirmation,
    )
    from core.operations import MutationResult, OperationSpec, execute_operation

    change_set = _load_change_set(conn, project_id, change_set_id, for_update=True)
    if change_set is None or change_set["state"] not in {"prepared", "confirmed"}:
        raise ProjectSettingsValidationError("Change Set not found")
    if prepared_payload_hash != change_set["prepared_payload_hash"]:
        raise ProjectSettingsStale("Prepared payload hash changed")
    request_payload = {
        "project_id": project_id,
        "org_id": org_id,
        "change_set_id": change_set_id,
        "prepared_payload_hash": change_set["prepared_payload_hash"],
        "dependency_fingerprint": change_set["dependency_fingerprint"],
    }
    context_reference = (
        f"organization:{org_id}/project:{project_id}/settings/changes/{change_set_id}"
    )
    if confirmation_secret is None:
        consumed = consume_presence_bound_confirmation(
            conn,
            confirmation_id=confirmation_id,
            actor_person_id=actor_person_id,
            command_type=PROJECT_SETTINGS_COMMAND,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            context_reference=context_reference,
        )
    else:
        consumed = consume_entry_confirmation(
            conn,
            confirmation_id=confirmation_id,
            confirmation_secret=confirmation_secret,
            actor_person_id=actor_person_id,
            command_type=PROJECT_SETTINGS_COMMAND,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            context_reference=context_reference,
        )
    if consumed.replayed and consumed.operation_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, result FROM app.operations WHERE id = %s", (consumed.operation_id,)
            )
            row = cur.fetchone()
        return {
            "operation_id": consumed.operation_id,
            "outcome": row[0] if row else "outcome_unknown",
            "result": _json(row[1], {}) if row else {},
            "idempotent_replay": True,
        }

    prepared = change_set["prepared_payload"]
    frozen_proposals = _load_frozen_proposals(conn, project_id, change_set_id)
    live_snapshot = _dependency_snapshot(
        conn, project_id, intent=change_set["intent"], proposals=frozen_proposals
    )
    validate_confirmation(prepared, live_snapshot)

    def mutation(inner_conn, operation_id: str):
        from core.project_provenance import ORIGIN_OPERATOR  # noqa: PLC0415

        with inner_conn.cursor() as cur:
            cur.execute(
                """
                SELECT active_configuration_version_id
                FROM app.projects WHERE id = %s AND org_id = %s FOR UPDATE
                """,
                (project_id, org_id),
            )
            project_row = cur.fetchone()
            if project_row is None:
                raise ProjectSettingsValidationError("Project not found")
            prior_id = project_row[0]
            current_posture: dict[str, Any] = {"defaults": {}, "capabilities": {}}
            if prior_id:
                cur.execute(
                    """
                    SELECT posture FROM app.project_configuration_versions
                    WHERE id = %s AND project_id = %s
                    """,
                    (prior_id, project_id),
                )
                prior_row = cur.fetchone()
                current_posture = (
                    _json(prior_row[0], current_posture) if prior_row else current_posture
                )
            posture = _apply_intent(current_posture, change_set["intent"])
            content_hash = canonical_hash(posture)
            # The proposals under review pinned the posture they intended to
            # activate. If the posture computed now differs, the reviewed impact is
            # not the impact about to commit -- refuse rather than reconcile.
            intended = {
                proposal["intended_configuration_content_hash"]
                for proposal in _intended_hashes(inner_conn, project_id, change_set_id)
            }
            if intended and intended != {content_hash}:
                raise ProjectSettingsStale(
                    "Review is stale: project_configuration changed after prepare"
                )
            cur.execute(
                """
                SELECT id FROM app.project_configuration_versions
                WHERE project_id = %s AND content_hash = %s
                """,
                (project_id, content_hash),
            )
            existing = cur.fetchone()
            if existing:
                version_id = existing[0]
            else:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1
                    FROM app.project_configuration_versions
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                version_number = int(cur.fetchone()[0])
                version_id = f"pcfg_{ULID()}"
                cur.execute(
                    """
                    INSERT INTO app.project_configuration_versions
                        (id, project_id, version_number, posture, dependency_fingerprint,
                         content_hash, previous_version_id, activated_by)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    """,
                    (
                        version_id,
                        project_id,
                        version_number,
                        _canonical_json(posture),
                        change_set["dependency_fingerprint"],
                        content_hash,
                        prior_id,
                        actor_person_id,
                    ),
                )
            cur.execute(
                """
                UPDATE app.projects
                SET active_configuration_version_id = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (version_id, project_id),
            )
            defaults = change_set["intent"].get("defaults", {})
            assignments = []
            params: list[Any] = []
            for intent_key, column in (
                ("reporting_currency", "canonical_currency"),
                ("reporting_timezone", "reporting_timezone"),
                ("verification_source", "verification_source_type"),
            ):
                if intent_key in defaults:
                    assignments.extend(
                        [
                            f"{column} = %s",
                            # ORIGIN_OPERATOR, not a fourth label. `app.project_preferences`
                            # constrains these columns to the three EARNED origins
                            # (migration 152, whose comment names `core.project_provenance`
                            # as the single decision point), and a confirmed change set IS
                            # the operator case: a person sent the value. Writing
                            # 'project_change_set' meant every confirmation carrying a
                            # default died on the CHECK, so no Project could ever activate
                            # a configuration version.
                            f"{column.replace('_type', '')}_origin = '{ORIGIN_OPERATOR}'"
                            if column == "verification_source_type"
                            else f"{column}_origin = '{ORIGIN_OPERATOR}'",
                            f"{column.replace('_type', '')}_confirmation_status = 'confirmed'"
                            if column == "verification_source_type"
                            else f"{column}_confirmation_status = 'confirmed'",
                        ]
                    )
                    params.append(defaults[intent_key])
            if assignments:
                params.append(project_id)
                cur.execute(
                    "UPDATE app.project_preferences SET "
                    + ", ".join(assignments)
                    + ", updated_at = NOW() WHERE project_id = %s",
                    params,
                )
            # Every capability the review actually counted advances, not only the
            # ones the intent named: the always-present two are recompiled on every
            # change and must not keep pointing at a retired version.
            coverage_states = _coverage_states_by_capability(frozen_proposals)
            requested_states = change_set["intent"].get("capabilities", {})
            for key in set(requested_states) | set(coverage_states):
                requested = requested_states.get(key)
                if requested == "disabled":
                    state = "disabled"
                else:
                    state = _activated_capability_state(coverage_states.get(key, []))
                cur.execute(
                    """
                    UPDATE app.project_capabilities
                    SET state = %s,
                        active_version_id = %s,
                        pending_change_set_id = NULL,
                        updated_at = NOW()
                    WHERE project_id = %s AND capability_key = %s
                    """,
                    (state, version_id, project_id, key),
                )
            cur.execute(
                """
                UPDATE app.project_change_sets
                SET state = 'confirmed', activated_version_id = %s,
                    confirmed_at = NOW(), updated_at = NOW()
                WHERE id = %s AND project_id = %s
                """,
                (version_id, change_set_id, project_id),
            )
        country_fan_out: dict[str, Any] = {
            "plan_versions": [],
            "candidates": [],
            "data_pointers_moved": False,
        }
        requested_country = (change_set["intent"].get("capabilities") or {}).get(
            "country"
        )
        if requested_country in {"enabled", "disabled"}:
            from core.country_activation import (  # noqa: PLC0415
                apply_country_plan_fan_out,
            )

            # Candidate preparation is part of this atomic confirmation, but
            # current Data pointers remain untouched. A failure rolls back the
            # Project capability and every isolated candidate row together.
            country_fan_out = apply_country_plan_fan_out(
                inner_conn,
                project_id=project_id,
                change_set_id=change_set_id,
                proposals=frozen_proposals,
                actor=actor_person_id,
                enabled=requested_country == "enabled",
                loaded_modules=loaded_modules,
            )
        # Story 48.5: one Project registry edit fans out to every Datastream the
        # frozen proposals proved compatible -- as CANDIDATES. Nothing here
        # publishes, moves a pointer or dispatches a pull; that separation is the
        # whole reason the fan-out is safe to run inside a confirmation.
        #
        # A failure is recorded and swallowed: the posture the operator confirmed
        # is already activated above, and rolling it back because a downstream
        # projection could not be written would refuse a change that succeeded.
        # The compiler recomputes coverage from the bindings that exist, so an
        # absent candidate reads as absent rather than as complete.
        fan_out: dict[str, Any] = {}
        if _activated_capability_state(
            _coverage_states_by_capability(frozen_proposals).get("competitors", [])
        ) != "disabled":
            try:
                from core.entity_bindings import apply_confirmed_fan_out  # noqa: PLC0415

                fan_out = apply_confirmed_fan_out(
                    inner_conn,
                    org_id=org_id,
                    project_id=project_id,
                    proposals=frozen_proposals,
                    actor=actor_person_id,
                )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).error(
                    "project_settings: competitor fan-out failed project=%s change_set=%s: %s",
                    project_id,
                    change_set_id,
                    type(exc).__name__,
                )
                fan_out = {"error": "fan_out_unavailable"}
        # The activated version freezes its complete owner set, including the
        # references the prior version carried that this change did not supersede.
        from core.capability_proposals import (  # noqa: PLC0415
            persist_configuration_owner_references,
        )

        owner_reference_count = persist_configuration_owner_references(
            inner_conn,
            project_id=project_id,
            configuration_version_id=version_id,
            prior_configuration_version_id=prior_id,
            proposals=frozen_proposals,
        )
        bind_entry_confirmation_operation(
            inner_conn, confirmation=consumed, operation_id=operation_id
        )
        return MutationResult(
            outcome="succeeded",
            before_hash=canonical_hash({"active_configuration_version_id": prior_id}),
            after_hash=canonical_hash({"active_configuration_version_id": version_id}),
            result={
                "change_set_id": change_set_id,
                "active_configuration_version_id": version_id,
                "owner_reference_count": owner_reference_count,
                # Activation is a Project act. Data keeps publication authority: no
                # candidate becomes Current and no plan, mapping or publication
                # pointer moves here.
                "data_pointers_moved": bool(
                    country_fan_out.get("data_pointers_moved")
                ),
                "authorized_proposal_ids": sorted(
                    proposal["id"]
                    for proposal in frozen_proposals
                    if proposal["coverage_state"] in {"partial", "pending"}
                ),
                # What the tracked-entity fan-out did, including what it refused
                # to do. `published` is always 0 and is stated rather than
                # omitted, so the absence of a publication is verifiable from the
                # operation record rather than assumed from the code.
                "tracked_entity_fan_out": fan_out,
                "country_plan_fan_out": country_fan_out,
            },
            outbox_payload={"project_id": project_id, "configuration_version_id": version_id},
        )

    spec = OperationSpec(
        command_type=PROJECT_SETTINGS_COMMAND,
        actor=actor_person_id,
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"project:{project_id}", "settings"),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "project-settings-v1",
            "catalog": "project-settings-v1",
            "tool": "project-settings-v1",
        },
        request_payload=request_payload,
        provider_references={},
        confirmation_mode="human",
        # operations.py stores only the digest of this reference. On the
        # presence-bound path there is no secret to reference at all, so the
        # confirmation id stands in -- an identifier, never authorization material.
        confirmation_reference=confirmation_secret or confirmation_id,
        trace_id=trace_id,
    )
    operation = execute_operation(conn, spec, mutation=mutation)
    return {
        "operation_id": operation.operation_id,
        "outcome": operation.outcome,
        "result": operation.result,
        "idempotent_replay": operation.replayed,
    }


def read_project_settings(
    conn, *, project_id: str, can_edit: bool, can_manage: bool = False
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.description, p.org_id, o.name,
                   p.active_configuration_version_id,
                   pp.canonical_currency, pp.canonical_currency_origin,
                   pp.canonical_currency_confirmation_status,
                   pp.reporting_timezone, pp.reporting_timezone_origin,
                   pp.reporting_timezone_confirmation_status,
                   pp.verification_source_type, pp.verification_source_origin,
                   pp.verification_source_confirmation_status
            FROM app.projects p
            JOIN app.organizations o ON o.id = p.org_id
            LEFT JOIN app.project_preferences pp ON pp.project_id = p.id
            WHERE p.id = %s AND p.status = 'active'
            """,
            (project_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ProjectSettingsValidationError("Project not found")
        active_posture: dict[str, Any] = {}
        if row[5]:
            cur.execute(
                """
                SELECT posture FROM app.project_configuration_versions
                WHERE project_id = %s AND id = %s
                """,
                (project_id, row[5]),
            )
            posture_row = cur.fetchone()
            active_posture = _json(posture_row[0], {}) if posture_row else {}
        # Story 49.2: applicability names the Business Domains this Project is
        # linked to, resolved through the authority. A link may point at an
        # identity minted in Master Data, and joining the superseded store alone
        # dropped that row from the list -- so a Project read as applicable to
        # nothing while its links plainly said otherwise.
        cur.execute(
            f"""
            SELECT DISTINCT d.id, d.name
            FROM {catalogue.DOMAIN_SOURCE} d
            JOIN app.mdm_business_links l
              ON l.org_id = d.org_id AND l.taxonomy_type = 'business_domain'
             AND l.taxonomy_id = d.id
            WHERE l.project_id = %s AND d.status = 'active'
              AND l.retired_at IS NULL
            ORDER BY d.name
            """,
            (project_id,),
        )
        domains = [{"id": item[0], "name": item[1]} for item in cur.fetchall()]
        cur.execute(
            """
            SELECT c.capability_key, c.availability, c.state, c.active_version_id,
                   c.pending_change_set_id, pending.state, pending.prepared_payload_hash,
                   pending.prepared_payload
            FROM app.project_capabilities c
            LEFT JOIN app.project_change_sets pending
              ON pending.id = c.pending_change_set_id AND pending.project_id = c.project_id
            -- EVERY key is named, and nothing falls into an ELSE. This CASE used
            -- to end in `ELSE 5`, which was exact while five keys existed and
            -- became a tie the moment a sixth arrived: `competitors` and
            -- `placement_mapping` both ranked 5, and two rows tied in an ORDER BY
            -- are not ordered. `validate_capability_ledger` compares this order
            -- to `CAPABILITY_SPECS`, so the envelope would have raised
            -- `capability keys are ... unordered` on some reads and not others --
            -- Project Settings dead by intermittence, which is worse than dead.
            WHERE c.project_id = %s ORDER BY
            CASE c.capability_key WHEN 'country' THEN 1 WHEN 'currency_fx' THEN 2
              WHEN 'reporting_timezone' THEN 3 WHEN 'tax_fees' THEN 4
              WHEN 'competitors' THEN 5 WHEN 'placement_mapping' THEN 6
              WHEN 'analytics_alignment' THEN 7 ELSE 8 END
            """,
            (project_id,),
        )
        cap_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, state, intent, prepared_payload, prepared_payload_hash, created_by,
                   created_at, prepared_at, confirmed_at
            FROM app.project_change_sets WHERE project_id = %s ORDER BY created_at DESC LIMIT 50
            """,
            (project_id,),
        )
        change_rows = cur.fetchall()
        # Coverage per capability, counted from the newest persisted proposal for
        # each (Datastream, capability) pair -- one query, not one per capability.
        cur.execute(
            """
            SELECT p.capability_key, p.coverage_state, COUNT(*)
            FROM app.datastream_capability_proposals p
            JOIN app.datastreams d
              ON d.id = p.datastream_id AND d.project_id = p.project_id
            WHERE p.project_id = %s AND d.archived_at IS NULL
              AND p.created_at = (
                  SELECT MAX(inner_p.created_at)
                  FROM app.datastream_capability_proposals inner_p
                  WHERE inner_p.project_id = p.project_id
                    AND inner_p.datastream_id = p.datastream_id
                    AND inner_p.capability_key = p.capability_key
              )
            GROUP BY p.capability_key, p.coverage_state
            """,
            (project_id,),
        )
        coverage_rows = cur.fetchall()
    from core.capability_proposals import aggregate_coverage, read_exceptions, settings_coverage

    persisted_exceptions: dict[str, list[dict[str, Any]]] = {}
    for item in read_exceptions(conn, project_id=project_id):
        persisted_exceptions.setdefault(str(item["capability_key"]), []).append(item)
    observed: dict[str, list[str]] = {key: [] for key in CAPABILITY_SPECS}
    for capability_key, coverage_state, count in coverage_rows:
        observed.setdefault(str(capability_key), []).extend([str(coverage_state)] * int(count))
    persisted_coverage = {
        key: settings_coverage(aggregate_coverage(observed.get(key, [])))
        for key in CAPABILITY_SPECS
    }
    capabilities = []
    for (
        key,
        availability,
        state,
        active_version_id,
        pending_change_set_id,
        pending_state,
        pending_payload_hash,
        pending_payload_value,
    ) in cap_rows:
        spec = CAPABILITY_SPECS.get(key)
        if spec is None:
            raise ProjectSettingsValidationError("unknown capability key")
        pending_payload = _json(pending_payload_value, {}) or {}
        pending_references = [
            ref
            for ref in pending_payload.get("referenced_versions", [])
            if ref.get("capability_key") == key
        ]
        # Exceptions come from their own accountable rows, not from a payload copy:
        # the same reference has to read identically in Settings, Data and MCP.
        capability_exceptions = persisted_exceptions.get(key, []) or [
            item
            for item in pending_payload.get("exceptions", [])
            if item.get("capability_key") in {None, key}
        ]
        capability_blockers = [
            item
            for item in pending_payload.get("blockers", [])
            if item.get("capability_key") in {None, key}
        ]
        # Owner links are semantic references, never browser paths: the client
        # resolves them through the canonical navigation registry, so a route Epic
        # 49 renames does not leave a dead link frozen in a pinned version.
        evidence_links = [
            {
                "owner": str(ref.get("owner_kind", "owner")).title(),
                "owner_reference": ref["owner_reference"],
                "object_type": ref.get("object_type"),
                "object_id": ref.get("object_id"),
                "version_id": ref.get("version_id"),
            }
            for ref in pending_references
            if isinstance(ref.get("owner_reference"), dict)
        ]
        capabilities.append(
            {
                "key": key,
                "availability": availability,
                "dependencies": spec["dependencies"],
                "active": {"state": state, "version_id": active_version_id},
                "pending": {
                    "change_set_id": pending_change_set_id,
                    "state": pending_state,
                    "prepared_payload_hash": pending_payload_hash,
                }
                if pending_change_set_id
                else None,
                # Counted from the persisted proposal rows. Before Story 48.1 this
                # reported every active Datastream as pending, whatever was true.
                "coverage": _coverage(persisted_coverage[key]),
                "exceptions": capability_exceptions,
                "blockers": capability_blockers,
                "owner_links": evidence_links or _owner_links(conn, project_id, key),
            }
        )
    validate_capability_ledger(capabilities)
    active_defaults = active_posture.get("defaults", {})
    if not isinstance(active_defaults, dict):
        active_defaults = {}
    defaults = {
        "reporting_currency": _default_value(
            active_defaults.get("reporting_currency"), row[6], row[7], row[8], "semantic-model"
        ),
        "reporting_timezone": _default_value(
            active_defaults.get("reporting_timezone"), row[9], row[10], row[11], "controls-quality"
        ),
        # The effective verification source follows the same boundary: Settings
        # picks the default, Governance owns its reconciliation policy.
        "verification_source": _default_value(
            active_defaults.get("verification_source"),
            row[12],
            row[13],
            row[14],
            "controls-quality",
        ),
    }
    from core.project_external_sharing import read_external_sharing  # noqa: PLC0415

    external_sharing = read_external_sharing(conn, project_id=project_id)
    external_sharing["can_change"] = can_manage
    return {
        "project": {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "organization": {"id": row[3], "name": row[4]},
            "active_configuration_version_id": row[5],
            "can_edit": can_edit,
            "can_manage": can_manage,
            "business_domains": domains,
            "defaults": defaults,
            # `proactive-assertions.md` decision 2: the project-scoped capability
            # that decides whether anything may leave the platform at all. It is
            # NOT a `project_capabilities` row -- it compiles into no Datastream
            # and changes no figure -- so it travels beside the defaults, which is
            # the store it lives in.
            "external_sharing": external_sharing,
        },
        "capabilities": capabilities,
        "changes": [
            {
                "id": item[0],
                "state": item[1],
                "diff_summary": _json(item[2], {}),
                "impact_summary": (_json(item[3], {}) or {}).get("impact"),
                "prepared_payload_hash": item[4],
                "blockers": (_json(item[3], {}) or {}).get("blockers", []),
                "created_by": item[5],
                "created_at": item[6].isoformat() if item[6] else None,
                "prepared_at": item[7].isoformat() if item[7] else None,
                "confirmed_at": item[8].isoformat() if item[8] else None,
            }
            for item in change_rows
        ],
    }


def _default_value(
    active: Any,
    proposal: Any,
    origin: Any,
    status: Any,
    governance_section: str,
) -> dict[str, Any]:
    """One Project default plus the Governance section that owns its policy."""
    from core.project_overview import owner_reference

    return {
        "active": active,
        "pending": proposal if status != "confirmed" else None,
        "origin": origin,
        "confirmation_status": status,
        "owner_reference": owner_reference("governance", governance_section),
    }


def _owner_links(conn, project_id: str, capability: str) -> list[dict[str, Any]]:
    """Return canonical owners, pinning Country to its exact current registry."""

    from core.capability_proposals import governance_owner_reference
    from core.project_overview import owner_reference

    governance_reference = governance_owner_reference(capability)
    if capability == "country":
        from core.country_registry import fetch_country_registry  # noqa: PLC0415

        registry = fetch_country_registry(conn, project_id=project_id)
        if registry is not None:
            governance_reference = governance_owner_reference(
                capability,
                object_type="registry",
                object_id=str(registry["id"]),
                version_id=(
                    str(registry["current_version_id"])
                    if registry.get("current_version_id")
                    else None
                ),
            )
    return [
        {"owner": "Governance", "owner_reference": governance_reference},
        {"owner": "Data", "owner_reference": owner_reference("data", "datastreams")},
    ]
