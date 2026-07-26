"""toorow -- Day-0 context seed layer (Story 44.2).

Builds a first, useful knowledge layer for a project WITHOUT any human action
and WITHOUT any LLM:

  1. Schema docs are ensured by DELEGATING to the 11.2 generator
     (``core.schema_context_gen.generate_schema_context``) -- this module never
     writes ``app.schema_context`` itself (AD-17: single writer). OPT-IN ONLY,
     see "What day 0 actually delivers" below.
  2. One PLATFORM-scoped topic per connector module connected to the project
     (``Connector: <display name>``), whose body is composed of VERBATIM strings
     read from the connector registry on disk (``server/modules/<module>/
     manifest.json`` + the module's ``api_catalog.json``). No paraphrase, no
     generation.
  3. ``describes`` edges from that topic to every schema doc of the module's
     relations (the module's own dbt staging models).

What day 0 actually delivers (ORCHESTRATOR DECISION, review wave 2)
-------------------------------------------------------------------
The two hooks -- project creation and post-landing -- NEVER run the schema-doc
generator. ``seed_project_context_best_effort`` takes ``ensure_schema`` and
defaults it to ``False``; both call sites pass ``ensure_schema=False``
explicitly. Running it there meant profiling the whole warehouse on EVERY pull
and at EVERY project creation: a second DuckDB connection fighting the writer's
lock, generator-internal commits landing mid-run, and preview extraction over
relations belonging to other tenants.

The honest consequence, which this module does not dress up: schema docs come
ONLY from the nightly 11.2 generator, itself gated by ``SCHEMA_CONTEXT_ENABLED``
-- an env var that is currently set NOWHERE in deploy (turning it on is a human
decision, tracked separately). So:

* the connector TOPIC is seeded immediately, on the first hook that fires;
* the ``describes`` EDGES link whatever schema docs ALREADY exist at the moment
  of each subsequent pull. Seeded edges therefore appear at the first pull
  AFTER schema docs exist -- never before. Nothing in this module creates them.

``ensure_schema=True`` stays available to an explicit caller (backfill script,
test) that knowingly owns that cost.

Idempotency contract
--------------------
* Topics are keyed on (title, platform scope). A second run finds the existing
  topic and creates nothing. Migration 113 puts a partial UNIQUE INDEX behind
  that key, so two concurrent seeds cannot both win; the loser catches the
  ``UniqueViolation`` inside a SAVEPOINT, re-reads the winner and carries on.
* Edges are keyed on (from_id, to_id, edge_type). A second run creates nothing.
  The check-then-insert is likewise wrapped in a per-edge SAVEPOINT so a racing
  insert costs one skipped edge, not the whole run.
* A topic whose LATEST version was written by somebody other than
  ``system:seed`` is NEVER overwritten -- the human edit wins and the seed
  reports it as skipped. A topic with NO version rows at all counts as
  human-owned too (fail CLOSED: unknown authorship is never overwritten).
* An ARCHIVED platform topic is left strictly alone: no body rewrite, no edges
  (``create_graph_edge`` rejects archived nodes, and one rejection used to abort
  the whole run forever).
* One unhealthy module never aborts the run: the per-module body is guarded.

System actor, not a user
------------------------
Platform-scoped writes here bypass the HTTP layer on purpose: the seed calls the
domain store directly with ``created_by='system:seed'``. The
``CONTEXT_PLATFORM_WRITERS`` allowlist in ``core.context_api`` gates *human*
identities arriving through ``/api/context``; it does not apply to this
in-process system actor, which is reachable only from project creation and the
post-landing hook (both server-side, both best-effort).
"""

from __future__ import annotations

import itertools
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.context_store import create_graph_edge, create_topic, update_topic

try:  # psycopg is always present in the server image; stay importable without it.
    from psycopg.errors import UniqueViolation
except Exception:  # pragma: no cover -- keeps `except UniqueViolation` legal

    class UniqueViolation(Exception):  # type: ignore[no-redef]
        """Placeholder used only when psycopg is not importable."""


logger = logging.getLogger(__name__)

#: Monotonic counter giving each SAVEPOINT a unique, SQL-safe name.
_savepoint_counter = itertools.count()


@contextmanager
def _savepoint(conn: Any, label: str) -> Iterator[None]:
    """Run a block inside a SAVEPOINT so a failure does not poison the tx.

    Without this, a single ``UniqueViolation`` from a racing seed aborts the
    caller's whole transaction: every later statement fails with
    ``current transaction is aborted``. Callers catch the re-raised exception
    and continue on a clean transaction.
    """
    name = f"sp_seed_{label}_{next(_savepoint_counter)}"
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        # ROLLBACK TO alone leaves the subtransaction live for the rest of
        # the caller's tx (SubtransSLRU pressure under many lost races) --
        # RELEASE it too. The cleanup is guarded so a dead connection
        # re-raises the ORIGINAL error, not the cleanup failure.
        try:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
                cur.execute(f"RELEASE SAVEPOINT {name}")
        except Exception:
            logger.warning("context_seed: savepoint_cleanup_failed name=%s", name)
        raise
    else:
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {name}")


#: Identity recorded on every row this module writes.
SEED_ACTOR = "system:seed"

#: First line of every seeded topic body (frontmatter-style marker).
SEED_BANNER = (
    "> Auto-generated from the connector registry — edits welcome, "
    "regeneration will not overwrite."
)

#: Deterministic topic title prefix -- the idempotency key with the scope.
TOPIC_TITLE_PREFIX = "Connector: "

#: Edge type linking a connector topic to the schema docs it describes.
DESCRIBES_EDGE = "describes"

#: Connector registry root (same layout the loader scans).
DEFAULT_MODULES_DIR = Path(__file__).parent.parent / "modules"


# ---------------------------------------------------------------------------
# Registry reads (on-disk connector registry -- verbatim strings only)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from disk, returning None on any read/parse problem."""
    import json  # noqa: PLC0415

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("context_seed: unreadable registry file %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def load_registry_entry(
    module_name: str, *, modules_dir: Path | None = None
) -> dict[str, Any] | None:
    """Return ``{manifest, catalog, relations}`` for a connector module.

    ``relations`` are the module's OWN dbt staging model names
    (``server/modules/<module>/dbt/staging/stg_*.sql``) -- the only on-disk,
    non-invented mapping between a connector module and warehouse relations.
    Returns None when the module has no readable manifest.
    """
    base = Path(modules_dir) if modules_dir is not None else DEFAULT_MODULES_DIR
    module_dir = base / module_name
    manifest = _read_json(module_dir / "manifest.json")
    if manifest is None:
        return None

    catalog = _read_json(module_dir / "api_catalog.json")

    staging_dir = module_dir / "dbt" / "staging"
    relations: list[str] = []
    if staging_dir.is_dir():
        relations = sorted(path.stem for path in staging_dir.glob("*.sql"))

    return {"manifest": manifest, "catalog": catalog, "relations": relations}


def connector_topic_title(manifest: dict[str, Any]) -> str:
    """Deterministic topic title for a connector module."""
    display_name = manifest.get("display_name") or manifest.get("name") or ""
    return f"{TOPIC_TITLE_PREFIX}{display_name}".strip()


def _profile_display_names(manifest: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for profile in manifest.get("report_profiles", []) or []:
        if isinstance(profile, dict) and profile.get("id"):
            names[profile["id"]] = profile.get("display_name") or profile["id"]
    return names


def build_connector_topic_body(
    manifest: dict[str, Any], catalog: dict[str, Any] | None = None
) -> str:
    """Compose the seeded topic body from VERBATIM registry strings.

    Deterministic: the same registry files always produce the same bytes, so a
    re-run of the seed over an unchanged registry writes no new version.
    """
    public_catalog = manifest.get("public_catalog") or {}
    lines: list[str] = [SEED_BANNER, ""]

    display_name = manifest.get("display_name") or manifest.get("name") or ""
    lines.append(f"# {display_name}")
    lines.append("")

    # --- Source identity ---------------------------------------------------
    lines.append(f"- Module: `{manifest.get('name', '')}`")
    if public_catalog.get("category"):
        lines.append(f"- Category: {public_catalog['category']}")
    lines.append(f"- Module kind: {manifest.get('module_kind', 'kpi')}")
    if manifest.get("auth_type"):
        lines.append(f"- Authentication: {manifest['auth_type']}")
    onboarding = public_catalog.get("onboarding_modes") or []
    if onboarding:
        lines.append(f"- Onboarding modes: {', '.join(sorted(onboarding))}")
    lines.append("")

    # --- Key concepts: report profiles -------------------------------------
    display_names = _profile_display_names(manifest)
    reports = (manifest.get("source_capabilities") or {}).get("reports") or []
    caveats: list[str] = []
    if reports:
        lines.append("## Report profiles")
        lines.append("")
        for report in sorted(reports, key=lambda item: str(item.get("id", ""))):
            report_id = str(report.get("id", ""))
            availability = report.get("availability") or {}
            status = availability.get("status", "")
            label = display_names.get(report_id, report_id)
            lines.append(f"- **{label}** (`{report_id}`) — {status}")
            metrics = sorted(report.get("metrics") or [])
            dimensions = sorted(report.get("dimensions") or [])
            if metrics:
                lines.append(f"  - Metrics: {', '.join(metrics)}")
            if dimensions:
                lines.append(f"  - Dimensions: {', '.join(dimensions)}")
            if status == "unavailable":
                caveats.append(
                    f"Profile `{report_id}` is unavailable "
                    f"({availability.get('reason_code', '')}). "
                    f"{availability.get('follow_up', '')}".strip()
                )
        lines.append("")

    # --- Key concepts: field descriptions (verbatim) -----------------------
    fields = (manifest.get("source_capabilities") or {}).get("fields") or []
    described = [
        field
        for field in fields
        if isinstance(field, dict) and field.get("field_id") and field.get("description")
    ]
    if described:
        lines.append("## Fields")
        lines.append("")
        for field in sorted(described, key=lambda item: str(item["field_id"])):
            lines.append(
                f"- `{field['field_id']}` ({field.get('kind', '')}) — {field['description']}"
            )
        lines.append("")

    # --- Key concepts: canonical mapping -----------------------------------
    metric_mapping = manifest.get("canonical_metric_mapping") or {}
    dimension_mapping = manifest.get("canonical_dimension_mapping") or {}
    if metric_mapping or dimension_mapping:
        lines.append("## Canonical mapping")
        lines.append("")
        for source_field, canonical in sorted(metric_mapping.items()):
            lines.append(f"- metric `{source_field}` → `{canonical}`")
        for source_field, canonical in sorted(dimension_mapping.items()):
            lines.append(f"- dimension `{source_field}` → `{canonical}`")
        lines.append("")

    # --- Caveats: verification state + tiering ------------------------------
    verification = public_catalog.get("verification") or {}
    if verification:
        caveats.insert(
            0,
            f"Live verification: {verification.get('status', '')} "
            f"({verification.get('reason_code', '')}). "
            f"{verification.get('follow_up', '')}".strip(),
        )

    if isinstance(catalog, dict):
        catalog_fields = [f for f in catalog.get("fields") or [] if isinstance(f, dict)]
        if catalog_fields:
            tiers: dict[str, int] = {}
            exposures: dict[str, int] = {}
            for field in catalog_fields:
                tier = str(field.get("tier", ""))
                exposure = str(field.get("exposure", ""))
                tiers[tier] = tiers.get(tier, 0) + 1
                exposures[exposure] = exposures.get(exposure, 0) + 1
            tier_text = ", ".join(f"{count} {tier}" for tier, count in sorted(tiers.items()))
            exposure_text = ", ".join(
                f"{count} {exposure}" for exposure, count in sorted(exposures.items())
            )
            caveats.append(
                f"API catalog tiering: {tier_text} fields; exposure: {exposure_text}."
            )
        for source in catalog.get("sources") or []:
            if isinstance(source, dict) and source.get("url"):
                caveats.append(
                    f"Catalog source ({source.get('kind', '')}, fetched "
                    f"{source.get('fetched_at', '')}): {source['url']}"
                )

    if caveats:
        lines.append("## Caveats")
        lines.append("")
        lines.extend(f"- {caveat}" for caveat in caveats)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Postgres reads
# ---------------------------------------------------------------------------


def connected_modules(conn: Any, project_id: str) -> list[str]:
    """Return the connector modules with at least one datastream in the project."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT module_name
            FROM app.datastreams
            WHERE project_id = %s AND module_name IS NOT NULL
            ORDER BY module_name
            """,
            (project_id,),
        )
        return [row[0] for row in cur.fetchall() if row and row[0]]


def project_schema_docs(conn: Any, project_id: str) -> list[tuple[str, str]]:
    """Return ``(schema_context_id, relation)`` pairs for a project (read-only)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, relation
            FROM app.schema_context
            WHERE project_id = %s
            ORDER BY relation, doc_kind
            """,
            (project_id,),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _find_platform_topic(conn: Any, title: str) -> dict[str, Any] | None:
    """Find the platform-scoped topic with this exact title (the seed's key).

    ACTIVE rows come first. Migration 113 only constrains non-archived rows, so
    an archived twin may legitimately share the title (that is how a curator
    retires one and how the migration's duplicate cleanup works). Ordering by
    created_at alone would let the older archived row shadow the live one and
    freeze the seed forever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, body_md, status
            FROM app.context_topics
            WHERE project_id IS NULL AND title = %s
            ORDER BY (status = 'archived'), created_at
            LIMIT 1
            """,
            (title,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "body_md": row[1], "status": row[2]}


def _latest_version_author(conn: Any, topic_id: str) -> str | None:
    """Return ``changed_by`` of the topic's latest version row, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT changed_by
            FROM app.context_topics_versions
            WHERE topic_id = %s
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (topic_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _edge_exists(conn: Any, *, from_id: str, to_id: str, edge_type: str) -> bool:
    """Existence check on (from_id, to_id, edge_type) -- the edge idempotency key."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM app.context_graph
            WHERE from_id = %s AND to_id = %s AND edge_type = %s
            LIMIT 1
            """,
            (from_id, to_id, edge_type),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Seed steps
# ---------------------------------------------------------------------------


def ensure_schema_docs(
    conn: Any, project_id: str, *, warehouse_conn: Any = None
) -> dict[str, Any]:
    """Ensure schema docs exist by DELEGATING to the 11.2 generator (AD-17).

    Never reimplements profiling. Gated by ``SCHEMA_CONTEXT_ENABLED`` exactly
    like the nightly scheduler step, and a no-op when no warehouse connection is
    available. Returns a small status dict; never raises.
    """
    if os.environ.get("SCHEMA_CONTEXT_ENABLED", "false").strip().lower() != "true":
        return {"status": "skipped", "reason": "schema_context_disabled"}

    try:
        from core.schema_context_gen import generate_schema_context  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 -- best-effort seed step
        logger.warning("context_seed: schema_context_gen import failed: %s", exc)
        return {"status": "skipped", "reason": "import_error"}

    try:
        if warehouse_conn is not None:
            result = generate_schema_context(
                conn,
                project_id=project_id,
                warehouse_conn=warehouse_conn,
                changed_by=SEED_ACTOR,
            )
        else:
            from core.db import get_warehouse_connection  # noqa: PLC0415

            with get_warehouse_connection(read_only=True) as opened:
                result = generate_schema_context(
                    conn,
                    project_id=project_id,
                    warehouse_conn=opened,
                    changed_by=SEED_ACTOR,
                )
    except Exception as exc:  # noqa: BLE001 -- schema docs must never fail the seed
        logger.warning(
            "context_seed: schema doc generation failed project=%s: %s", project_id, exc
        )
        return {"status": "error", "reason": str(exc)}

    return {"status": "ok", "generator": result}


def seed_project_context(
    conn: Any,
    project_id: str,
    *,
    module_names: list[str] | None = None,
    modules_dir: Path | None = None,
    warehouse_conn: Any = None,
    ensure_schema: bool = True,
) -> dict[str, Any]:
    """Idempotently seed the project's knowledge layer from the socles.

    Args:
        conn: open psycopg connection (the CALLER owns the transaction/commit).
        project_id: target project.
        module_names: restrict the seed to these connector modules (the
            post-landing hook passes the single module that just landed). None
            means "every module connected to the project".
        modules_dir: registry root override (tests).
        warehouse_conn: read-only warehouse connection for the 11.2 generator.
        ensure_schema: run step 1 (schema docs) before topics/edges. The hooks
            pass False -- see the module docstring; only an explicit caller that
            owns the profiling cost should pass True.

    Returns a counters dict: ``topics_created``, ``topics_updated``,
    ``topics_skipped_human`` (the skip rule), ``edges_created``,
    ``schema_docs``, plus two observability lists:

    * ``modules`` -- modules whose topic this run actually WROTE (created or
      updated). A steady-state re-run writes nothing and reports ``[]``.
    * ``modules_skipped`` -- modules this run wrote no topic for: unknown in the
      registry, unusable title, archived topic, human-owned topic, lost a
      concurrent create, or raised.
    """
    summary: dict[str, Any] = {
        "project_id": project_id,
        "modules": [],
        "modules_skipped": [],
        "topics_created": 0,
        "topics_updated": 0,
        "topics_skipped_human": 0,
        "edges_created": 0,
        "schema_docs": {"status": "skipped", "reason": "not_requested"},
    }

    if ensure_schema:
        summary["schema_docs"] = ensure_schema_docs(
            conn, project_id, warehouse_conn=warehouse_conn
        )

    modules = (
        [name for name in module_names if name]
        if module_names is not None
        else connected_modules(conn, project_id)
    )
    if not modules:
        return summary

    docs_by_relation: dict[str, list[str]] = {}
    for doc_id, relation in project_schema_docs(conn, project_id):
        docs_by_relation.setdefault(relation, []).append(doc_id)

    for module_name in sorted(set(modules)):
        # One sick module must never abort the whole seed: an unreadable
        # registry entry, a rejected edge, a driver hiccup -- all are contained
        # here and cost exactly that module.
        try:
            _seed_one_module(
                conn,
                project_id,
                module_name=module_name,
                modules_dir=modules_dir,
                docs_by_relation=docs_by_relation,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 -- containment is the point
            logger.warning(
                "context_seed: module_failed project=%s module=%s error=%s",
                project_id,
                module_name,
                exc,
            )
            if module_name not in summary["modules_skipped"]:
                summary["modules_skipped"].append(module_name)

    return summary


def _seed_one_module(
    conn: Any,
    project_id: str,
    *,
    module_name: str,
    modules_dir: Path | None,
    docs_by_relation: dict[str, list[str]],
    summary: dict[str, Any],
) -> None:
    """Seed the topic + describes-edges of ONE connector module.

    Mutates ``summary`` in place. Raises only what the caller is expected to
    contain (see the guard in :func:`seed_project_context`).
    """
    entry = load_registry_entry(module_name, modules_dir=modules_dir)
    if entry is None:
        logger.warning(
            "context_seed: no registry entry for module=%s (skipped)", module_name
        )
        summary["modules_skipped"].append(module_name)
        return

    manifest = entry["manifest"]
    title = connector_topic_title(manifest)
    if not title or title == TOPIC_TITLE_PREFIX.strip():
        logger.warning(
            "context_seed: module=%s has no display name -- no topic title", module_name
        )
        summary["modules_skipped"].append(module_name)
        return

    body_md = build_connector_topic_body(manifest, entry.get("catalog"))

    existing = _find_platform_topic(conn, title)
    if existing is None:
        try:
            with _savepoint(conn, "topic"):
                topic = create_topic(
                    conn,
                    project_id=None,
                    title=title,
                    body_md=body_md,
                    created_by=SEED_ACTOR,
                )
        except UniqueViolation:
            # Migration 113's partial unique index: a concurrent seed created
            # the same platform topic between our SELECT and our INSERT. The
            # winner's row is authoritative; adopt it and keep going.
            winner = _find_platform_topic(conn, title)
            if winner is None or winner["status"] != "active":
                logger.warning(
                    "context_seed: lost the create race for %r and found no active "
                    "winner -- module=%s skipped",
                    title,
                    module_name,
                )
                summary["modules_skipped"].append(module_name)
                return
            logger.info(
                "context_seed: concurrent seed already created %r -- adopting it", title
            )
            summary["modules_skipped"].append(module_name)
            topic_id = winner["id"]
        else:
            topic_id = topic["id"]
            summary["topics_created"] += 1
            summary["modules"].append(module_name)
    elif existing["status"] != "active":
        # An archived platform topic is off limits. create_graph_edge refuses
        # archived nodes with a ValueError, which previously killed the run for
        # good -- so we do not even try to link it.
        logger.info(
            "context_seed: platform topic %r is %s -- module=%s left untouched",
            title,
            existing["status"],
            module_name,
        )
        summary["topics_skipped_human"] += 1
        summary["modules_skipped"].append(module_name)
        return
    else:
        topic_id = existing["id"]
        author = _latest_version_author(conn, topic_id)
        if author != SEED_ACTOR:
            # Skip rule, failing CLOSED: a curator owns this topic (author is
            # their identity), or authorship is unknown (no version row at all,
            # author is None). Either way the seed does not overwrite it.
            summary["topics_skipped_human"] += 1
            summary["modules_skipped"].append(module_name)
        elif existing["body_md"] != body_md:
            update_topic(
                conn,
                topic_id=topic_id,
                patch={"body_md": body_md},
                changed_by=SEED_ACTOR,
            )
            summary["topics_updated"] += 1
            summary["modules"].append(module_name)

    for relation in entry["relations"]:
        for doc_id in docs_by_relation.get(relation, []):
            if _edge_exists(
                conn, from_id=topic_id, to_id=doc_id, edge_type=DESCRIBES_EDGE
            ):
                continue
            try:
                with _savepoint(conn, "edge"):
                    create_graph_edge(
                        conn,
                        project_id=project_id,
                        from_id=topic_id,
                        from_type="topic",
                        to_id=doc_id,
                        to_type="schema_doc",
                        edge_type=DESCRIBES_EDGE,
                        created_by=SEED_ACTOR,
                    )
            except UniqueViolation:
                # Check-then-insert race: another seed inserted the same edge.
                # The desired state holds either way.
                logger.info(
                    "context_seed: edge %s -> %s already inserted concurrently",
                    topic_id,
                    doc_id,
                )
                continue
            summary["edges_created"] += 1


def seed_project_context_best_effort(
    project_id: str,
    *,
    module_names: list[str] | None = None,
    ensure_schema: bool = False,
) -> dict[str, Any] | None:
    """Hook entry point: seed on its own connection, swallowing every failure.

    Used at project creation and after a datastream landing. Seeding is a
    convenience layer: it must NEVER fail the caller's operation. Failures are
    logged at WARNING and the hook returns None.

    ``ensure_schema`` defaults to **False** on purpose (see the module
    docstring): a hook must not trigger a full-warehouse DuckDB profiling pass
    on every pull and every project creation. Both call sites pass it
    explicitly so the intent is readable at the call site too.
    """
    if not project_id:
        return None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            summary = seed_project_context(
                conn,
                project_id,
                module_names=module_names,
                ensure_schema=ensure_schema,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- hook must never break its caller
        logger.warning(
            "context_seed: seed_failed project=%s modules=%s error=%s",
            project_id,
            module_names,
            exc,
        )
        return None

    logger.info(
        "context_seed: seeded project=%s topics_created=%d edges_created=%d "
        "skipped_human=%d modules_written=%s modules_skipped=%s",
        project_id,
        summary["topics_created"],
        summary["edges_created"],
        summary["topics_skipped_human"],
        summary["modules"],
        summary["modules_skipped"],
    )
    return summary
