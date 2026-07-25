"""toorow -- Media plan N:M mapping service (Story 22.3, FR38 / CAP-26).

Pure business logic over a psycopg connection (pattern: core.mediaplan_store).
The caller owns the transaction lifecycle (commit / rollback). Postgres is the
sole writer of the plan (AD-8); dbt reads a governed mirror.

This is the anti-double-count node of Epic 22. A plan line is mapped N:M to
1..N real campaigns/placements, and a campaign may be shared by 1..N lines. The
mapping NEVER touches facts (AD-4/AD-6): the connectors' totals are byte-identical
with or without a mapping. It only decides how a campaign's real spend is
VENTILATED across the plan lines that claim it -- and the ventilation must sum
back to the original spend EXACTLY (no cent created or lost).

Key invariants (proven by tests):
  * SUM(split_weight) == 1.0 EXACTLY per (plan_id, connector, campaign_ref)
    (Decimal, zero tolerance). Enforced INSIDE the transaction under a per-plan
    SELECT ... FOR UPDATE lock so two concurrent writes cannot leave a sum != 1.0.
  * The mapping is keyed on the STABLE (plan_id, line_key) -- it survives
    re-imports. A line dropped from the active version -> status 'orphaned'
    (visible, re-attachable), never silently deleted (the "point dur").
  * Default split is équiréparti 1/N PER CAMPAIGN across the lines that map it
    (decision 4), with the rounding remainder on the first line_key so the
    weights sum to EXACTLY 1.0.

Business errors reuse the typed hierarchy from core.mediaplan_store (MediaPlan*
Error) so the API layer maps them to 4xx identically (lesson 12.3).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from core.audit import (
    ACTION_MEDIA_PLAN_MAPPING_ORPHANED,
    ACTION_MEDIA_PLAN_MAPPING_REBALANCED,
    ACTION_MEDIA_PLAN_MAPPING_SET,
    insert_audit_row,
)
from core.mediaplan_store import (
    MediaPlanNotFoundError,
    MediaPlanStateError,
    MediaPlanValidationError,
)

# split_weight is stored as NUMERIC(7,6): six fractional digits, quantum 1e-6.
_WEIGHT_QUANTUM = Decimal("0.000001")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Pure split computation (testable without a DB)
# ---------------------------------------------------------------------------


def compute_default_splits(line_keys: list[str]) -> dict[str, Decimal]:
    """Equal-split 1/N across ``line_keys``, summing to EXACTLY 1.0 (Decimal).

    Deterministic largest-remainder at the NUMERIC(7,6) quantum (1e-6):
      * work in integer micro-units (1_000_000 total) to avoid any float;
      * base = floor(1_000_000 / n) micro-units per line;
      * the remainder micro-units are handed +1 to the first lines in
        line_key SORTED order, so the sequence is stable and reproducible.

    Example (n=3): 1_000_000 // 3 = 333_333, remainder 1 -> first line gets
    333_334 -> 0.333334 + 0.333333 + 0.333333 = 1.000000 EXACTLY.

    Raises MediaPlanValidationError on an empty list, a duplicate line_key, or
    more than 1_000_000 lines (review F-5): the NUMERIC(7,6) quantum is 1e-6, so
    beyond 1e6 lines base = floor(1_000_000 / n) = 0 -> a line would receive a
    split_weight of 0 and its spend would silently vanish from the ventilation.
    """
    if not line_keys:
        raise MediaPlanValidationError(
            "Impossible de répartir : aucune ligne cible."
        )
    ordered = sorted(line_keys)
    if len(set(ordered)) != len(ordered):
        raise MediaPlanValidationError(
            "Impossible de répartir : clé de ligne dupliquée."
        )
    if len(ordered) > 1_000_000:
        raise MediaPlanValidationError(
            "Impossible de répartir : trop de lignes cibles "
            "(maximum 1 000 000 pour un poids non nul)."
        )

    n = len(ordered)
    total_micro = 1_000_000
    base, remainder = divmod(total_micro, n)

    out: dict[str, Decimal] = {}
    for i, key in enumerate(ordered):
        micro = base + (1 if i < remainder else 0)
        out[key] = (Decimal(micro) * _WEIGHT_QUANTUM).quantize(_WEIGHT_QUANTUM)
    return out


def validate_split_sum(weights: list[Decimal], *, campaign_ref: str) -> None:
    """Assert the split weights sum to EXACTLY 1.0 (Decimal, zero tolerance).

    Raises MediaPlanValidationError (422) with the observed percentage in the
    message when they do not, e.g. a 80/30 mistake -> "constaté : 110 %".
    """
    total = sum(weights, Decimal("0"))
    if total != _ONE:
        pct = (total * Decimal("100")).normalize()
        # Render without a trailing exponent (e.g. 110 not 1.1E+2).
        pct_str = f"{pct:f}"
        raise MediaPlanValidationError(
            "La somme des répartitions pour la campagne "
            f"« {campaign_ref} » doit faire 100 % (constaté : {pct_str} %)."
        )


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _fmt(val: Any) -> Any:
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, Decimal):
        return format(val, "f")
    from datetime import date, datetime  # noqa: PLC0415

    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


def _parse_weight(value: Any, *, connector: str, campaign_ref: str) -> Decimal:
    """Parse an explicit split_weight into a Decimal in (0, 1] at the 1e-6 quantum.

    Refuses float coercion / non-finite / out-of-range so the stored weight is
    always exact (NUMERIC(7,6) never receives a lossy value).
    """
    try:
        weight = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise MediaPlanValidationError(
            f"Le poids de répartition de « {campaign_ref} » ({connector}) "
            "doit être un nombre décimal."
        ) from exc
    if not weight.is_finite():
        raise MediaPlanValidationError(
            f"Le poids de répartition de « {campaign_ref} » ({connector}) "
            "doit être un nombre fini."
        )
    if weight <= 0 or weight > _ONE:
        raise MediaPlanValidationError(
            f"Le poids de répartition de « {campaign_ref} » ({connector}) "
            "doit être compris dans l'intervalle ]0, 1]."
        )
    quantised = weight.quantize(_WEIGHT_QUANTUM)
    if quantised != weight:
        raise MediaPlanValidationError(
            f"Le poids de répartition de « {campaign_ref} » ({connector}) "
            "ne peut pas avoir plus de 6 décimales."
        )
    return quantised


# ---------------------------------------------------------------------------
# Internal: plan / version resolution & locking
# ---------------------------------------------------------------------------


def _lock_plan(conn: Any, plan_id: str) -> None:
    """Serialise mapping writes of a plan: SELECT ... FOR UPDATE on its root row.

    Anti-course: two concurrent mapping writes on the same plan take this lock in
    turn, so neither can observe a stale set of a campaign's weights and leave a
    SUM(split_weight) != 1.0 (the invariant is validated after the delete+insert
    while the lock is held).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.media_plans WHERE id = %s FOR UPDATE",
            (plan_id,),
        )
        if cur.fetchone() is None:
            raise MediaPlanNotFoundError("Plan introuvable.")


def _active_line_keys(conn: Any, plan_id: str) -> set[str]:
    """Return the line_keys of the plan's ACTIVE version (empty set if none)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.line_key
            FROM app.media_plan_lines l
            JOIN app.media_plan_versions v ON v.id = l.version_id
            WHERE v.plan_id = %s AND v.is_active
            """,
            (plan_id,),
        )
        return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Write: set the complete mapping set of a single line
# ---------------------------------------------------------------------------


def set_line_mappings(
    conn: Any,
    *,
    plan_id: str,
    line_key: str,
    entries: list[dict[str, Any]],
    actor: str,
) -> dict[str, Any]:
    """REPLACE the whole mapping set of one line (delete + insert), atomically.

    ``entries`` = [{connector, campaign_ref, split_weight?}]. The line's previous
    mappings are deleted and the new ones inserted in the same transaction, under
    a per-plan FOR UPDATE lock (anti-course).

    Default split (decision 4 -- équiréparti PER CAMPAIGN, not per line): after
    the replacement, for each (connector, campaign_ref) TOUCHED, if NONE of that
    campaign's ACTIVE mappings carry an explicit weight -> the campaign is split
    1/N across the N lines that map it (N counted across the plan, remainder on
    the first line_key so it sums to EXACTLY 1.0). If ANY explicit weight exists
    on that campaign -> the existing weights must sum to EXACTLY 1.0 (Decimal,
    zero tolerance) else MediaPlanValidationError (422) with the observed sum.

    line_key must exist in the plan's ACTIVE version, EXCEPT when it already
    carries orphaned mappings (re-attachment of an orphaned line is allowed).

    Returns the resulting mapping set for the line (grouped shape of list_mappings
    filtered to this line_key).
    """
    if not isinstance(line_key, str) or not line_key.strip():
        raise MediaPlanValidationError("La clé de ligne ne peut pas être vide.")
    line_key = line_key.strip()
    if not isinstance(entries, list):
        raise MediaPlanValidationError("« mappings » doit être une liste.")

    # Normalise + de-duplicate entries by (connector, campaign_ref): a payload may
    # not repeat a target (last-writer-wins would hide data).
    normalised: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise MediaPlanValidationError("Chaque mapping doit être un objet.")
        connector = (raw.get("connector") or "").strip()
        campaign_ref = (raw.get("campaign_ref") or "").strip()
        if not connector:
            raise MediaPlanValidationError("Le connecteur ne peut pas être vide.")
        if not campaign_ref:
            raise MediaPlanValidationError(
                "La référence de campagne ne peut pas être vide."
            )
        key = (connector, campaign_ref)
        if key in seen:
            raise MediaPlanValidationError(
                f"La cible « {campaign_ref} » ({connector}) est dupliquée "
                "dans la requête."
            )
        seen.add(key)
        weight_raw = raw.get("split_weight")
        weight = (
            _parse_weight(weight_raw, connector=connector, campaign_ref=campaign_ref)
            if weight_raw is not None
            else None
        )
        normalised.append(
            {"connector": connector, "campaign_ref": campaign_ref, "weight": weight}
        )

    # --- transaction body (caller commits) -------------------------------------
    _lock_plan(conn, plan_id)  # per-plan serialisation + existence

    active_keys = _active_line_keys(conn, plan_id)

    with conn.cursor() as cur:
        # Does this line already carry orphaned mappings? (re-attachment path)
        cur.execute(
            """
            SELECT COUNT(*) FROM app.plan_line_mappings
            WHERE plan_id = %s AND line_key = %s AND status = 'orphaned'
            """,
            (plan_id, line_key),
        )
        has_orphaned = int(cur.fetchone()[0]) > 0

        if line_key not in active_keys and not has_orphaned:
            raise MediaPlanValidationError(
                f"La ligne « {line_key} » est inconnue de la version active."
            )

        # Which campaigns this line CURRENTLY maps (before replacement) -- we must
        # re-validate those too, since removing this line from a campaign changes
        # that campaign's split set.
        cur.execute(
            """
            SELECT connector, campaign_ref FROM app.plan_line_mappings
            WHERE plan_id = %s AND line_key = %s
            """,
            (plan_id, line_key),
        )
        previously_touched = {(r[0], r[1]) for r in cur.fetchall()}

        # Replace: delete this line's mappings, then insert the new set.
        cur.execute(
            "DELETE FROM app.plan_line_mappings WHERE plan_id = %s AND line_key = %s",
            (plan_id, line_key),
        )

        # Status of the re-inserted rows: 'active' iff the line is in the active
        # version, else 'orphaned' (re-attachment keeps it honestly orphaned until
        # the line comes back in a published version).
        row_status = "active" if line_key in active_keys else "orphaned"

        for entry in normalised:
            # When the caller left the weight implicit, a provisional placeholder
            # (the 1e-6 quantum) is written and OVERWRITTEN by the per-campaign
            # rebalance below (implicit regime always recomputes 1/N). When the
            # caller supplied an explicit weight, it is written as-is and the
            # campaign is validated (SUM==1.0), never overwritten.
            provisional = entry["weight"] if entry["weight"] is not None else _WEIGHT_QUANTUM
            cur.execute(
                """
                INSERT INTO app.plan_line_mappings
                    (plan_id, line_key, connector, campaign_ref, split_weight,
                     status, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
                """,
                (
                    plan_id,
                    line_key,
                    entry["connector"],
                    entry["campaign_ref"],
                    provisional,
                    row_status,
                    actor,
                ),
            )

        # Every campaign this line now maps + every campaign it used to map.
        touched = previously_touched | {
            (e["connector"], e["campaign_ref"]) for e in normalised
        }
        explicit_targets = {
            (e["connector"], e["campaign_ref"])
            for e in normalised
            if e["weight"] is not None
        }

        for connector, campaign_ref in sorted(touched):
            _rebalance_campaign(
                cur,
                plan_id=plan_id,
                connector=connector,
                campaign_ref=campaign_ref,
                has_explicit_here=(connector, campaign_ref) in explicit_targets,
            )

        insert_audit_row(
            conn,
            identity=actor,
            action=ACTION_MEDIA_PLAN_MAPPING_SET,
            provider_account="platform",
            connection_ref="",
            metadata={
                "plan_id": plan_id,
                "line_key": line_key,
                "entry_count": len(normalised),
                "status": row_status,
            },
        )

    return {
        "plan_id": str(plan_id),
        "line_key": line_key,
        "mappings": _list_line_mappings(conn, plan_id=plan_id, line_key=line_key),
    }


def _rebalance_campaign(
    cur: Any,
    *,
    plan_id: str,
    connector: str,
    campaign_ref: str,
    has_explicit_here: bool,
) -> None:
    """Enforce the SUM=1.0 invariant for one (plan, connector, campaign_ref).

    Considers ONLY the ACTIVE mappings of the campaign (orphaned ones do not
    ventilate spend). Regime is decided by THIS write's intent for the campaign:

      * no ACTIVE mapping left           -> nothing to do (fully unmapped now);
      * has_explicit_here is True        -> the caller supplied an explicit weight
                                            for this campaign: validate the stored
                                            active weights SUM == 1.0 EXACTLY
                                            (Decimal, zero tolerance) else 422 with
                                            the observed sum. Mixing an explicit
                                            line with an implicit placeholder line
                                            correctly fails here (sum != 1.0).
      * has_explicit_here is False       -> équiréparti 1/N across the active lines
                                            (remainder on the first line_key), then
                                            persisted. The default is recomputed for
                                            the WHOLE campaign so adding/removing a
                                            line rebalances the others (decision 4).

    Driving the regime off has_explicit_here (this write's intent) -- not off the
    stored bytes -- keeps it unambiguous: a legitimate explicit weight numerically
    equal to the 1e-6 provisional placeholder is never misread as implicit.
    """
    cur.execute(
        """
        SELECT line_key, split_weight FROM app.plan_line_mappings
        WHERE plan_id = %s AND connector = %s AND campaign_ref = %s
              AND status = 'active'
        ORDER BY line_key
        """,
        (plan_id, connector, campaign_ref),
    )
    rows = cur.fetchall()
    if not rows:
        return  # campaign fully unmapped now -> nothing to validate

    line_keys = [r[0] for r in rows]

    if has_explicit_here:
        weights = [Decimal(r[1]) for r in rows]
        validate_split_sum(weights, campaign_ref=campaign_ref)
        return

    # Implicit regime: équiréparti 1/N across active lines, persist.
    splits = compute_default_splits(line_keys)
    for line_key, weight in splits.items():
        cur.execute(
            """
            UPDATE app.plan_line_mappings
            SET split_weight = %s, updated_at = now()
            WHERE plan_id = %s AND line_key = %s AND connector = %s
                  AND campaign_ref = %s AND status = 'active'
            """,
            (weight, plan_id, line_key, connector, campaign_ref),
        )


# ---------------------------------------------------------------------------
# Orphan status recomputation (called at each publication)
# ---------------------------------------------------------------------------


def refresh_orphan_status(conn: Any, *, plan_id: str) -> dict[str, int]:
    """Recompute mapping status against the plan's ACTIVE version, then rebalance.

    A mapping whose line_key no longer exists in the active version -> 'orphaned';
    a line_key that has come back -> 'active'. Called by the store at each version
    publication (mediaplan_store.publish_version), in the SAME transaction, after
    the active pointer flip. Runs under the per-plan FOR UPDATE lock so a concurrent
    mapping write cannot interleave and leave a SUM(split_weight) != 1.0.

    Rebalance after status flips (review F-1/F-2/F-6, décision « équiréparti
    ajustable »): merely flipping status breaks the SUM=1.0 invariant per
    (connector, campaign_ref):

      * F-1 -- orphaning ONE of the lines that shared a campaign leaves the
        remaining active lines summing to < 1.0 (e.g. L1+L2 at 0.5/0.5, orphan L1
        -> L2 alone at 0.5). Without a fix, 22.4 would ventilate only 50 % of that
        campaign's spend and never list the rest as unmapped.
      * F-2 -- REACTIVATING a line restores its STALE stored weight, which can push
        the campaign's sum > 1.0 (double-count) if the campaign was rebalanced while
        the line was orphaned.
      * F-6 -- a re-attached orphaned line was inserted with the 1e-6 placeholder;
        that placeholder is only overwritten here, at reactivation.

    So for EACH (connector, campaign_ref) touched by an orphan OR a reactivation we
    recompute an équiréparti 1/N split (compute_default_splits, deterministic, exact
    sum 1.0) over the campaign's REMAINING active mappings, OVERWRITING the previous
    weights (explicit ones included -- a structural change of lines invalidates the
    prior manual intent; the user re-adjusts afterwards, which is what "équiréparti
    ajustable" means). A ACTION_MEDIA_PLAN_MAPPING_REBALANCED audit row is written
    per campaign whose weights actually changed (AD-9 traceability), carrying the
    old and new weights per line_key.

    A touched campaign whose lines are ALL now orphaned has no remaining active
    mapping: nothing to rebalance -- it re-appears naturally in list_unmapped_actuals
    (that read subtracts only status='active' mappings, so a fully-orphaned campaign
    is no longer subtracted and is re-listed as unmapped spend).

    Returns {"orphaned": n_orphaned, "reactivated": n_reactivated}.
    """
    _lock_plan(conn, plan_id)  # per-plan serialisation + existence (anti-course)

    active_keys = _active_line_keys(conn, plan_id)

    with conn.cursor() as cur:
        # Orphan: currently active but line_key gone from the active version.
        # RETURNING the touched campaigns so we can rebalance them below.
        if active_keys:
            cur.execute(
                """
                UPDATE app.plan_line_mappings
                SET status = 'orphaned', updated_at = now()
                WHERE plan_id = %s AND status = 'active'
                      AND line_key <> ALL(%s)
                RETURNING connector, campaign_ref
                """,
                (plan_id, list(active_keys)),
            )
        else:
            cur.execute(
                """
                UPDATE app.plan_line_mappings
                SET status = 'orphaned', updated_at = now()
                WHERE plan_id = %s AND status = 'active'
                RETURNING connector, campaign_ref
                """,
                (plan_id,),
            )
        orphaned_rows = cur.fetchall()
        n_orphaned = len(orphaned_rows)
        touched: set[tuple[str, str]] = {(r[0], r[1]) for r in orphaned_rows}

        # Reactivate: currently orphaned but line_key back in the active version.
        n_reactivated = 0
        if active_keys:
            cur.execute(
                """
                UPDATE app.plan_line_mappings
                SET status = 'active', updated_at = now()
                WHERE plan_id = %s AND status = 'orphaned'
                      AND line_key = ANY(%s)
                RETURNING connector, campaign_ref
                """,
                (plan_id, list(active_keys)),
            )
            reactivated_rows = cur.fetchall()
            n_reactivated = len(reactivated_rows)
            touched |= {(r[0], r[1]) for r in reactivated_rows}

        if n_orphaned or n_reactivated:
            insert_audit_row(
                conn,
                identity="system",
                action=ACTION_MEDIA_PLAN_MAPPING_ORPHANED,
                provider_account="platform",
                connection_ref="",
                metadata={
                    "plan_id": str(plan_id),
                    "orphaned": n_orphaned,
                    "reactivated": n_reactivated,
                },
            )

        # F-1/F-2/F-6: re-establish SUM=1.0 for every campaign whose active line set
        # changed, by equirepartition over the REMAINING active mappings.
        for connector, campaign_ref in sorted(touched):
            _rebalance_after_status_change(
                conn,
                cur,
                plan_id=plan_id,
                connector=connector,
                campaign_ref=campaign_ref,
            )

    return {"orphaned": n_orphaned, "reactivated": n_reactivated}


def _rebalance_after_status_change(
    conn: Any,
    cur: Any,
    *,
    plan_id: str,
    connector: str,
    campaign_ref: str,
) -> None:
    """Equirepartition a campaign's REMAINING active mappings after a status flip.

    Overwrites the stored weights with compute_default_splits over the active
    line_keys (deterministic, exact sum 1.0), regardless of whether the previous
    weights were implicit or explicit (a structural change of lines invalidates the
    prior manual intent -- décision « équiréparti ajustable »). Writes a
    ACTION_MEDIA_PLAN_MAPPING_REBALANCED audit row when at least one weight changed.

    No active mapping left -> nothing to do (the campaign is now fully unmapped and
    re-appears in list_unmapped_actuals via its status='active' clause).
    """
    cur.execute(
        """
        SELECT line_key, split_weight FROM app.plan_line_mappings
        WHERE plan_id = %s AND connector = %s AND campaign_ref = %s
              AND status = 'active'
        ORDER BY line_key
        """,
        (plan_id, connector, campaign_ref),
    )
    rows = cur.fetchall()
    if not rows:
        return  # fully unmapped now -> nothing to rebalance

    old_weights = {r[0]: Decimal(r[1]) for r in rows}
    new_weights = compute_default_splits(list(old_weights.keys()))

    if new_weights == old_weights:
        return  # already équiréparti -> no write, no audit noise

    for line_key, weight in new_weights.items():
        cur.execute(
            """
            UPDATE app.plan_line_mappings
            SET split_weight = %s, updated_at = now()
            WHERE plan_id = %s AND line_key = %s AND connector = %s
                  AND campaign_ref = %s AND status = 'active'
            """,
            (weight, plan_id, line_key, connector, campaign_ref),
        )

    insert_audit_row(
        conn,
        identity="system",
        action=ACTION_MEDIA_PLAN_MAPPING_REBALANCED,
        provider_account="platform",
        connection_ref="",
        metadata={
            "plan_id": str(plan_id),
            "connector": connector,
            "campaign_ref": campaign_ref,
            "line_keys": sorted(new_weights.keys()),
            "old_weights": {k: format(v, "f") for k, v in old_weights.items()},
            "new_weights": {k: format(v, "f") for k, v in new_weights.items()},
        },
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _list_line_mappings(
    conn: Any, *, plan_id: str, line_key: str
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT connector, campaign_ref, split_weight, status
            FROM app.plan_line_mappings
            WHERE plan_id = %s AND line_key = %s
            ORDER BY connector, campaign_ref
            """,
            (plan_id, line_key),
        )
        return [
            {
                "connector": r[0],
                "campaign_ref": r[1],
                "split_weight": _fmt(r[2]),
                "status": r[3],
            }
            for r in cur.fetchall()
        ]


def list_mappings(conn: Any, *, plan_id: str) -> dict[str, Any]:
    """Return the plan's mappings grouped by line_key (+ status + weights)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.media_plans WHERE id = %s",
            (plan_id,),
        )
        if cur.fetchone() is None:
            raise MediaPlanNotFoundError("Plan introuvable.")

        cur.execute(
            """
            SELECT line_key, connector, campaign_ref, split_weight, status
            FROM app.plan_line_mappings
            WHERE plan_id = %s
            ORDER BY line_key, connector, campaign_ref
            """,
            (plan_id,),
        )
        rows = cur.fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for line_key, connector, campaign_ref, weight, status in rows:
        grouped.setdefault(line_key, []).append(
            {
                "connector": connector,
                "campaign_ref": campaign_ref,
                "split_weight": _fmt(weight),
                "status": status,
            }
        )
    return {
        "plan_id": str(plan_id),
        "lines": [
            {"line_key": k, "mappings": grouped[k]} for k in sorted(grouped)
        ],
    }


# Reasons carried by each unmapped entry (E1-F-1). Distinct so the UI/card can tell
# a genuinely never-mapped campaign apart from spend a mapping DOES exist for but
# that falls OUTSIDE the window of the line(s) that map it.
REASON_UNMAPPED = "sans_mapping"
REASON_OUT_OF_WINDOW = "hors_fenetre_lignes_mappees"


def _parse_iso_day(value: Any) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` string for a date/datetime/str value, else None."""
    from datetime import date, datetime  # noqa: PLC0415

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    # Keep only the date part of an ISO timestamp ("2026-03-15T..." -> "2026-03-15").
    return text[:10]


def list_unmapped_actuals(
    conn: Any,
    *,
    plan_id: str,
    campaign_spend_fn: Any = None,
    campaign_spend_daily_fn: Any = None,
) -> dict[str, Any]:
    """Return the plan-perimeter spend that carries NO active ventilation (AD-9).

    Perimeter = the plan's ACTIVE-version date window [min(start_date),
    max(end_date)] x the campaigns present in fact_daily_kpi with spend. This
    guarantees "100 % du réel reste visible même non mappé" -- nothing ever
    disappears from BOTH the ventilation and this view.

    E1-F-1 (22.8 review, MAJOR) -- day-accurate coverage, not envelope subtraction.
    The mart ventilates a campaign's real spend PER LINE WINDOW (``day BETWEEN
    line.start AND line.end``). Subtracting every campaign that has ANY active
    mapping over the WHOLE plan envelope would silently drop the spend of a mapped
    campaign that falls on a day OUTSIDE its line(s) window -- it would appear in
    neither the ventilation nor here. So we decide coverage DAY BY DAY:

      * a (connector, campaign_ref, day) is COVERED iff at least one ACTIVE line that
        maps that campaign has ``start_date <= day <= end_date``;
      * unmapped = (campaigns with NO active mapping: all their envelope spend, as
        before, ``reason='sans_mapping'``) + (mapped campaigns: the spend of their
        NON-covered days, aggregated, ``reason='hors_fenetre_lignes_mappees'``).

    Conservation: Σ(covered spend) + Σ(unmapped spend) == Σ(total perimeter spend).

    ``campaign_spend_daily_fn(project_id, start, end)`` (defaults to
    ``warehouse.query_campaign_spend_daily``) is injected so the store stays
    DB-driver-only and the logic is unit-testable. ``campaign_spend_fn`` (the
    window-total ``warehouse.query_campaign_spend``) is kept in the signature for
    backward compatibility with the existing caller -- coverage is now decided at the
    daily grain, so the window-total read is no longer the perimeter source. The
    daily fn NEVER returns a silent [] on failure: it raises (WarehouseUnavailable),
    which propagates here (story rule: warehouse unavailable => clean error, not a
    fake empty perimeter).

    Returns:
      {plan_id, window: {start, end} | None, project_id,
       unmapped: [{connector, campaign_ref, spend, reason}, ...]}
    When the active version has no lines, window is None and unmapped is [].
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM app.media_plans WHERE id = %s",
            (plan_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise MediaPlanNotFoundError("Plan introuvable.")
        project_id = row[0]

        # Active-version window = envelope of its lines' dates.
        cur.execute(
            """
            SELECT MIN(l.start_date), MAX(l.end_date)
            FROM app.media_plan_lines l
            JOIN app.media_plan_versions v ON v.id = l.version_id
            WHERE v.plan_id = %s AND v.is_active
            """,
            (plan_id,),
        )
        wmin, wmax = cur.fetchone()

        if wmin is None or wmax is None:
            return {
                "plan_id": str(plan_id),
                "project_id": project_id,
                "window": None,
                "unmapped": [],
            }

        # Active mapped campaigns (for the "no active mapping at all" test).
        cur.execute(
            """
            SELECT DISTINCT connector, campaign_ref
            FROM app.plan_line_mappings
            WHERE plan_id = %s AND status = 'active'
            """,
            (plan_id,),
        )
        mapped = {(r[0], r[1]) for r in cur.fetchall()}

        # E1-F-1: the ACTIVE line windows per (connector, campaign_ref). Each active
        # mapping joined to its line in the ACTIVE version yields the [start, end]
        # window over which that campaign IS ventilated. A campaign may be mapped by
        # several lines -> several windows (any covers a day).
        cur.execute(
            """
            SELECT m.connector, m.campaign_ref, l.start_date, l.end_date
            FROM app.plan_line_mappings m
            JOIN app.media_plan_versions v
                ON v.plan_id = m.plan_id AND v.is_active
            JOIN app.media_plan_lines l
                ON l.version_id = v.id AND l.line_key = m.line_key
            WHERE m.plan_id = %s AND m.status = 'active'
            """,
            (plan_id,),
        )
        windows_by_campaign: dict[tuple[Any, Any], list[tuple[str, str]]] = {}
        for connector, campaign_ref, l_start, l_end in cur.fetchall():
            s = _parse_iso_day(l_start)
            e = _parse_iso_day(l_end)
            if s is None or e is None:
                continue
            windows_by_campaign.setdefault((connector, campaign_ref), []).append((s, e))

    start_iso = _parse_iso_day(wmin)
    end_iso = _parse_iso_day(wmax)

    # Injected DAILY warehouse read -- raises on failure (never a silent []).
    if campaign_spend_daily_fn is None:
        from core import warehouse as _wh  # noqa: PLC0415

        campaign_spend_daily_fn = _wh.query_campaign_spend_daily
    daily_rows = campaign_spend_daily_fn(project_id, start_iso, end_iso)

    def _is_covered(key: tuple[Any, Any], day: str | None) -> bool:
        """A (connector, campaign_ref, day) is covered iff a mapped active line spans it."""
        if day is None:
            return False
        for s, e in windows_by_campaign.get(key, ()):  # empty tuple when unmapped
            if s <= day <= e:
                return True
        return False

    # Aggregate the non-covered spend back to a per-(connector, campaign_ref) total,
    # tagging the reason. A campaign with NO active mapping -> every day is
    # non-covered -> reason 'sans_mapping'. A mapped campaign -> only its
    # out-of-window days accumulate here -> reason 'hors_fenetre_lignes_mappees'.
    accum: dict[tuple[Any, Any], float] = {}
    for r in daily_rows:
        connector = r.get("connector")
        campaign_ref = r.get("campaign_ref")
        key = (connector, campaign_ref)
        day = _parse_iso_day(r.get("day"))
        if _is_covered(key, day):
            continue
        spend = r.get("spend") or 0.0
        accum[key] = accum.get(key, 0.0) + float(spend)

    unmapped: list[dict[str, Any]] = []
    for (connector, campaign_ref), spend in accum.items():
        reason = (
            REASON_OUT_OF_WINDOW
            if (connector, campaign_ref) in mapped
            else REASON_UNMAPPED
        )
        unmapped.append(
            {
                "connector": connector,
                "campaign_ref": campaign_ref,
                "spend": spend,
                "reason": reason,
            }
        )

    # Stable order: unmapped campaigns first, then out-of-window, then by identity.
    unmapped.sort(
        key=lambda u: (u["reason"] != REASON_UNMAPPED, str(u["connector"]), str(u["campaign_ref"]))
    )

    return {
        "plan_id": str(plan_id),
        "project_id": project_id,
        "window": {"start": start_iso, "end": end_iso},
        "unmapped": unmapped,
    }


# Re-export the typed errors so callers can `from core.mediaplan_mapping import ...`
# without also importing mediaplan_store (parity with the store module surface).
__all__ = [
    "REASON_OUT_OF_WINDOW",
    "REASON_UNMAPPED",
    "MediaPlanNotFoundError",
    "MediaPlanStateError",
    "MediaPlanValidationError",
    "compute_default_splits",
    "list_mappings",
    "list_unmapped_actuals",
    "refresh_orphan_status",
    "set_line_mappings",
    "validate_split_sum",
]
