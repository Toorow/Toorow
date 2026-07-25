"""Governed geographic posture previews and atomic confirmation (CAP-27)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping

from ulid import ULID

from core.datastream_intents import compile_geographic_intent, normalize_intent
from core.geographic_reporting import GLOBAL, LOCAL_MARKETS, GeographicPosture


class GeographicChangeError(RuntimeError):
    code = "geographic_change_error"


class GeographicPreviewNotFound(GeographicChangeError):
    code = "geographic_preview_not_found"


class GeographicPreviewStale(GeographicChangeError):
    code = "geographic_preview_stale"


class GeographicPreviewBlocked(GeographicChangeError):
    code = "geographic_preview_blocked"


class GeographicPreviewConflict(GeographicChangeError):
    code = "geographic_preview_conflict"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def posture_diff(previous: GeographicPosture, target: GeographicPosture) -> dict[str, list[str]]:
    previous_codes = set(previous.country_codes)
    target_codes = set(target.country_codes)
    return {
        "added_country_codes": sorted(target_codes - previous_codes),
        "removed_country_codes": sorted(previous_codes - target_codes),
    }


def dependency_fingerprint(
    posture: GeographicPosture,
    dependencies: Iterable[Mapping[str, object]],
) -> str:
    normalized = [
        {
            "datastream_id": item.get("datastream_id"),
            "plan_version_id": item.get("plan_version_id"),
            "plan_content_hash": item.get("plan_content_hash"),
            "capability_fingerprint": item.get("capability_fingerprint"),
            "mapping_version_id": item.get("mapping_version_id"),
            "source_schema_hash": item.get("source_schema_hash"),
        }
        for item in dependencies
    ]
    normalized.sort(key=lambda item: str(item["datastream_id"]))
    return _sha256({"posture": posture.as_dict(), "datastreams": normalized})


def _find_report(capabilities: Mapping[str, object], report_id: str) -> dict[str, Any]:
    reports = capabilities.get("reports")
    if not isinstance(reports, list):
        return {}
    return next(
        (dict(item) for item in reports if isinstance(item, dict) and item.get("id") == report_id),
        {},
    )


def _estimate_volume(report: Mapping[str, object]) -> dict[str, object]:
    pagination = report.get("pagination")
    if not isinstance(pagination, Mapping):
        return {"upper_bound_rows": None, "confidence": "unavailable"}
    row_limit = pagination.get("row_limit")
    max_pages = pagination.get("max_pages")
    if isinstance(row_limit, int) and isinstance(max_pages, int):
        return {"upper_bound_rows": row_limit * max_pages, "confidence": "provider_bound"}
    if isinstance(row_limit, int):
        return {"upper_bound_rows": row_limit, "confidence": "per_page_only"}
    return {"upper_bound_rows": None, "confidence": "unbounded_or_unknown"}


def build_datastream_impact(
    datastream: Mapping[str, object],
    previous: GeographicPosture,
    target: GeographicPosture,
    capabilities: dict[str, Any] | None,
    *,
    earliest_country_date: str | None,
) -> dict[str, Any]:
    """Build one deterministic, source-agnostic preview entry without writes."""

    source_intent = datastream.get("intent")
    if not isinstance(source_intent, dict):
        raise GeographicPreviewBlocked("current Datastream intent is unavailable")
    compiled = compile_geographic_intent(source_intent, target, capabilities)
    proposed_intent, proposed_content_hash = normalize_intent(compiled)
    geographic = proposed_intent.get("geographic", {})
    compilation_status = geographic.get("compilation_status")
    country_capability = compilation_status in {"country_complete", "preserved_full_grain"}

    current_geo = source_intent.get("geographic")
    current_status = (
        current_geo.get("compilation_status") if isinstance(current_geo, dict) else None
    )
    current_plan_has_country = current_status in {
        "country_complete",
        "preserved_full_grain",
    }
    retained_country = current_plan_has_country and earliest_country_date is not None
    evidenced_country_date = earliest_country_date if current_plan_has_country else None
    local_target = target.mode == LOCAL_MARKETS
    backfill_required = local_target and country_capability and not retained_country
    coverage_state = (
        "consolidated"
        if target.mode == GLOBAL
        else "unavailable"
        if not country_capability
        else "complete"
        if retained_country
        else "partial"
    )

    source = proposed_intent.get("source", {})
    report = _find_report(capabilities or {}, str(source.get("report_id") or ""))
    quota = report.get("quota_cost") if isinstance(report.get("quota_cost"), dict) else {}
    blocking_gaps: list[dict[str, object]] = []
    if local_target and not country_capability:
        blocking_gaps.append(
            {
                "code": "geographic_country_unavailable",
                "message": "This report has no compatible canonical-country joint grain.",
                "repair": {"action": "select_country_compatible_report_or_global_mode"},
            }
        )

    datastream_id = str(datastream.get("id") or "")
    provider_limit = report.get("max_provider_backfill_days")
    max_backfill_days = provider_limit if isinstance(provider_limit, int) else None
    return {
        "datastream_id": datastream_id,
        "datastream_name": datastream.get("name"),
        "module_name": datastream.get("module_name"),
        "current_plan_version_id": datastream.get("current_plan_version_id"),
        "country_capability": country_capability,
        "compatibility": "compatible" if not blocking_gaps else "blocked",
        "effective_country_field": geographic.get("effective_country_field"),
        "earliest_available_country_date": evidenced_country_date,
        "max_provider_backfill_days": max_backfill_days,
        "backfill_bound_evidence": (
            "provider_capability" if max_backfill_days is not None else "unavailable"
        ),
        "estimated_volume": _estimate_volume(report),
        "estimated_cost": quota,
        "blocking_gaps": blocking_gaps,
        "backfill_required": backfill_required,
        "provider_pull_required": backfill_required,
        "retained_history_preserved": True,
        "coverage_state": coverage_state,
        "proposed_intent": proposed_intent,
        "proposed_content_hash": proposed_content_hash,
        "backfill_contract": {
            "refetch_path": f"/api/datastreams/{datastream_id}/refetch",
            "reprocess_path": f"/api/datastreams/{datastream_id}/executions",
            "publication_path": f"/api/datastreams/{datastream_id}/executions",
            "candidate_publication_required": True,
        },
    }


def validate_confirmation_dependencies(
    preview: Mapping[str, object], current_fingerprint: str
) -> None:
    if preview.get("dependency_fingerprint") != current_fingerprint:
        raise GeographicPreviewStale("preview dependencies changed; create a fresh preview")
    impact = preview.get("impact")
    datastreams = impact.get("datastreams", []) if isinstance(impact, Mapping) else []
    for item in datastreams if isinstance(datastreams, list) else []:
        if isinstance(item, Mapping) and item.get("blocking_gaps"):
            raise GeographicPreviewBlocked("preview contains blocking geographic gaps")


def _mint_preview_id() -> str:
    return f"gprev_{ULID()}"


def _posture_from_dict(value: Mapping[str, object]) -> GeographicPosture:
    mode = str(value.get("geographic_mode") or GLOBAL)
    codes = value.get("local_market_country_codes")
    return GeographicPosture(
        mode=mode,
        country_codes=tuple(str(code) for code in codes) if isinstance(codes, list) else (),
    )


def _preview_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "project_id",
        "previous_posture",
        "target_posture",
        "dependency_fingerprint",
        "impact",
        "status",
        "confirmation",
        "requested_by",
        "created_at",
        "confirmed_at",
    )
    result = dict(zip(keys, row))
    for key in ("created_at", "confirmed_at"):
        if isinstance(result.get(key), (date, datetime)):
            result[key] = result[key].isoformat()
    return result


CapabilityLoader = Callable[[Mapping[str, object], str, object, list[Any]], dict[str, Any] | None]


def _fetch_governed_datastreams(project_id: str, conn: object) -> list[dict[str, Any]]:
    """Read current governed plan dependencies and retained-country evidence."""

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT ds.id, ds.name, ds.module_name, ds.connection_ref_id,
                   ds.current_plan_version_id, pv.content_hash,
                   pv.capability_fingerprint, ds.date_window_days,
                   pv.normalized_payload, ds.enabled,
                   history.earliest_completed_date,
                   ds.current_mapping_version_id, mapping.source_schema_hash
            FROM app.datastreams ds
            JOIN app.datastream_plan_versions pv
              ON pv.id = ds.current_plan_version_id
             AND pv.datastream_id = ds.id
             AND pv.project_id = ds.project_id
            LEFT JOIN LATERAL (
                SELECT MIN(date_from)::text AS earliest_completed_date
                FROM app.pull_jobs
                WHERE datastream_id = ds.id AND state = 'done'
            ) history ON TRUE
            LEFT JOIN app.datastream_mapping_versions mapping
              ON mapping.id = ds.current_mapping_version_id
             AND mapping.datastream_id = ds.id
             AND mapping.project_id = ds.project_id
            WHERE ds.project_id = %s
              AND ds.archived_at IS NULL
              AND ds.current_plan_version_id IS NOT NULL
            ORDER BY ds.id
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    keys = (
        "id",
        "name",
        "module_name",
        "connection_ref_id",
        "current_plan_version_id",
        "current_plan_content_hash",
        "current_capability_fingerprint",
        "date_window_days",
        "intent",
        "enabled",
        "earliest_completed_date",
        "current_mapping_version_id",
        "current_source_schema_hash",
    )
    return [dict(zip(keys, row)) for row in rows]


def _default_capability_loader(
    datastream: Mapping[str, object],
    identity: str,
    conn: object,
    loaded_modules: list[Any],
) -> dict[str, Any] | None:
    intent = datastream.get("intent")
    source = intent.get("source", {}) if isinstance(intent, Mapping) else {}
    if source.get("kind") != "connector_pull":
        return None
    from core.source_capabilities import get_scoped_source_capabilities  # noqa: PLC0415

    return get_scoped_source_capabilities(
        project_id=str(datastream.get("project_id") or source.get("project_id") or ""),
        connection_ref_id=str(source.get("connection_ref_id") or ""),
        identity=identity,
        loaded_modules=loaded_modules,
        conn=conn,
    )


def _current_dependencies(
    project_id: str,
    identity: str,
    conn: object,
    loaded_modules: list[Any],
    capability_loader: CapabilityLoader,
) -> tuple[GeographicPosture, list[dict[str, Any]], dict[str, dict[str, Any] | None], str]:
    from core.geographic_reporting import fetch_project_geographic_posture  # noqa: PLC0415

    posture = fetch_project_geographic_posture(project_id, conn)
    datastreams = _fetch_governed_datastreams(project_id, conn)
    capabilities_by_id: dict[str, dict[str, Any] | None] = {}
    dependencies: list[dict[str, object]] = []
    for datastream in datastreams:
        datastream["project_id"] = project_id
        capabilities = capability_loader(datastream, identity, conn, loaded_modules)
        datastream_id = str(datastream["id"])
        capabilities_by_id[datastream_id] = capabilities
        dependencies.append(
            {
                "datastream_id": datastream_id,
                "plan_version_id": datastream.get("current_plan_version_id"),
                "plan_content_hash": datastream.get("current_plan_content_hash"),
                "capability_fingerprint": _sha256(capabilities) if capabilities else None,
                "mapping_version_id": datastream.get("current_mapping_version_id"),
                "source_schema_hash": datastream.get("current_source_schema_hash"),
            }
        )
    return posture, datastreams, capabilities_by_id, dependency_fingerprint(posture, dependencies)


def create_geographic_change_preview(
    *,
    project_id: str,
    target: GeographicPosture,
    identity: str,
    idempotency_key: str,
    conn: object,
    loaded_modules: list[Any],
    capability_loader: CapabilityLoader | None = None,
) -> dict[str, Any]:
    """Persist an immutable no-side-effect preview of a posture change."""

    if not idempotency_key.strip():
        raise ValueError("Idempotency-Key is required")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT id, project_id, previous_posture, target_posture,
                   dependency_fingerprint, impact, status, confirmation,
                   requested_by, created_at, confirmed_at
            FROM app.geographic_change_previews
            WHERE project_id = %s AND idempotency_key_hash = %s
            """,
            (project_id, key_hash),
        )
        existing = cur.fetchone()
    if existing is not None:
        preview = _preview_dict(existing)
        if preview["target_posture"] != target.as_dict():
            raise GeographicPreviewConflict("idempotency key belongs to another target posture")
        preview["idempotent_replay"] = True
        return preview

    loader = capability_loader or _default_capability_loader
    previous, datastreams, capabilities_by_id, fingerprint = _current_dependencies(
        project_id, identity, conn, loaded_modules, loader
    )
    impacts = [
        build_datastream_impact(
            datastream,
            previous,
            target,
            capabilities_by_id[str(datastream["id"])],
            earliest_country_date=datastream.get("earliest_completed_date"),
        )
        for datastream in datastreams
    ]
    diff = posture_diff(previous, target)
    impact = {
        "posture_diff": diff,
        "datastreams": impacts,
        "affected_datastream_count": len(impacts),
        "blocking_gap_count": sum(len(item["blocking_gaps"]) for item in impacts),
        "backfill_required": any(item["backfill_required"] for item in impacts),
        "provider_pull_required": any(item["provider_pull_required"] for item in impacts),
    }
    preview_id = _mint_preview_id()
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            INSERT INTO app.geographic_change_previews
                (id, project_id, previous_posture, target_posture,
                 dependency_fingerprint, impact, status, confirmation,
                 idempotency_key_hash, requested_by)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb,
                    'pending', NULL, %s, %s)
            RETURNING id, project_id, previous_posture, target_posture,
                      dependency_fingerprint, impact, status, confirmation,
                      requested_by, created_at, confirmed_at
            """,
            (
                preview_id,
                project_id,
                _canonical_json(previous.as_dict()),
                _canonical_json(target.as_dict()),
                fingerprint,
                _canonical_json(impact),
                key_hash,
                identity,
            ),
        )
        inserted = cur.fetchone()
    conn.commit()  # type: ignore[attr-defined]
    result = _preview_dict(inserted)
    result["idempotent_replay"] = False
    return result


def _fetch_preview_for_update(preview_id: str, project_id: str, conn: object) -> dict[str, Any]:
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT id, project_id, previous_posture, target_posture,
                   dependency_fingerprint, impact, status, confirmation,
                   requested_by, created_at, confirmed_at
            FROM app.geographic_change_previews
            WHERE id = %s AND project_id = %s
            FOR UPDATE
            """,
            (preview_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise GeographicPreviewNotFound("geographic preview not found")
    return _preview_dict(row)


def confirm_geographic_change(
    *,
    preview_id: str,
    project_id: str,
    identity: str,
    backfill_decision: str,
    conn: object,
    loaded_modules: list[Any],
    capability_loader: CapabilityLoader | None = None,
) -> dict[str, Any]:
    """Confirm a fresh preview and commit plan pointers + posture atomically."""

    if backfill_decision not in {"defer", "request"}:
        raise ValueError("backfill_decision must be 'defer' or 'request'")
    preview = _fetch_preview_for_update(preview_id, project_id, conn)
    if preview["status"] == "confirmed":
        return {**(preview.get("confirmation") or {}), "idempotent_replay": True}
    if preview["status"] != "pending":
        raise GeographicPreviewStale("preview is no longer pending")

    loader = capability_loader or _default_capability_loader
    previous, datastreams, capabilities_by_id, fingerprint = _current_dependencies(
        project_id, identity, conn, loaded_modules, loader
    )
    validate_confirmation_dependencies(preview, fingerprint)
    target = _posture_from_dict(preview["target_posture"])
    if previous.as_dict() != preview["previous_posture"]:
        raise GeographicPreviewStale("project posture changed; create a fresh preview")

    preview_impacts = {
        item["datastream_id"]: item for item in preview["impact"].get("datastreams", [])
    }
    fresh_impacts: dict[str, dict[str, Any]] = {}
    for datastream in datastreams:
        datastream_id = str(datastream["id"])
        fresh = build_datastream_impact(
            datastream,
            previous,
            target,
            capabilities_by_id[datastream_id],
            earliest_country_date=datastream.get("earliest_completed_date"),
        )
        expected = preview_impacts.get(datastream_id)
        if (
            expected is None
            or expected.get("proposed_content_hash") != fresh["proposed_content_hash"]
        ):
            raise GeographicPreviewStale("compiled plan changed; create a fresh preview")
        fresh_impacts[datastream_id] = fresh

    from core.audit import (  # noqa: PLC0415
        ACTION_PROJECT_GEOGRAPHIC_CHANGE_CONFIRMED,
        insert_audit_row,
    )
    from core.datastream_intents import save_datastream_intent  # noqa: PLC0415
    from core.geographic_reporting import persist_project_geographic_posture  # noqa: PLC0415

    version_results: list[dict[str, object]] = []
    try:
        for datastream in datastreams:
            datastream_id = str(datastream["id"])
            saved = save_datastream_intent(
                datastream_id=datastream_id,
                project_id=project_id,
                intent=datastream["intent"],
                identity=identity,
                idempotency_key=f"geography:{preview_id}:{datastream_id}",
                conn=conn,
                geographic_posture=target,
                capabilities=capabilities_by_id[datastream_id],
                reason="geographic_posture_confirmed",
                commit=False,
            )
            version_results.append(
                {
                    "datastream_id": datastream_id,
                    "previous_plan_version_id": datastream["current_plan_version_id"],
                    "plan_version_id": saved["id"],
                    "version_number": saved["version_number"],
                    "coverage_state": fresh_impacts[datastream_id]["coverage_state"],
                }
            )

        persist_project_geographic_posture(project_id, target, conn)
        backfill_actions = [
            {
                "datastream_id": item["datastream_id"],
                **item["backfill_contract"],
            }
            for item in fresh_impacts.values()
            if item["backfill_required"]
        ]
        coverage_statuses = [item["coverage_state"] for item in fresh_impacts.values()]
        resulting_coverage = (
            "consolidated"
            if target.mode == GLOBAL
            else "unavailable"
            if not coverage_statuses or "unavailable" in coverage_statuses
            else "partial"
            if "partial" in coverage_statuses
            else "complete"
        )
        confirmation = {
            "preview_id": preview_id,
            "project_id": project_id,
            "previous_posture": previous.as_dict(),
            "new_posture": target.as_dict(),
            "posture_diff": posture_diff(previous, target),
            "plan_versions": version_results,
            "backfill_decision": backfill_decision,
            "backfill_actions": backfill_actions,
            "coverage_state": resulting_coverage,
            "idempotent_replay": False,
        }
        insert_audit_row(
            conn,
            identity=identity,
            action=ACTION_PROJECT_GEOGRAPHIC_CHANGE_CONFIRMED,
            provider_account="",
            connection_ref="",
            metadata=confirmation,
        )
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(
                """
                UPDATE app.geographic_change_previews
                SET status = 'confirmed', confirmation = %s::jsonb,
                    confirmed_by = %s, confirmed_at = NOW()
                WHERE id = %s AND project_id = %s AND status = 'pending'
                """,
                (_canonical_json(confirmation), identity, preview_id, project_id),
            )
            if cur.rowcount != 1:
                raise GeographicPreviewStale("preview was confirmed concurrently")
        conn.commit()  # type: ignore[attr-defined]
        return confirmation
    except Exception:
        conn.rollback()  # type: ignore[attr-defined]
        raise
