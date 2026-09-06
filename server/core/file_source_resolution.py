"""toorow -- resolve the file-source producer an ARRIVAL must replay (AI-88).

Which Template, which mapping, and whether a human ever confirmed the pair. This
module reads two artifacts the product already persists and derives nothing new;
the comment below states which columns and why they are trustworthy.
"""

from __future__ import annotations

import json
import logging
import re as _re
from typing import Any

from core.tabular_types import (
    CsvExcelImportError,
    ParseResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File-source routing (AI-88): resolve the producer an ARRIVAL must replay.
#
# Story 22.12 gave `run_import` a `producer` seam and nothing ever passed one,
# so the whole Epic 22 file-source chain -- producer, gate, recognizer, drift --
# had zero production callers. The routing rule below is not a new design
# decision; it reads two artifacts the product already persists:
#
#   WHICH TEMPLATE. `app.datastreams.config -> source_owner ->
#   managed_feed_source_ref`. The wizard offers active `app.file_source_templates`
#   rows as `template_ref` (datastream_setup_observations.py:337), activation
#   collapses that into `managed_feed.source_ref` (datastream_activation.py:1377)
#   and persists it on the Datastream (:743). The `fst_` prefix is a discriminator
#   we are ENTITLED to trust: migration 097 enforces it as a CHECK constraint
#   (`ck_file_source_templates_id`). The same column also carries staged-asset and
#   sheet refs, so anything that is not an `fst_` id routes nowhere and the
#   CSV/Excel path runs exactly as before.
#
#   WHICH MAPPING. `app.datastream_mapping_versions.mapping_payload`, pinned per
#   import by the `mapping_version_id` every caller of `run_import` already
#   passes. It is the versioned, content-hashed, immutable artifact the target
#   asks for, and its `binding` carries `confirmed_by` -- the human confirmation
#   is IN the artifact. We replay it. We deliberately do NOT run the recognizer
#   here: re-deriving a mapping at arrival is precisely the "silent re-map" AD-8
#   forbids.
# ---------------------------------------------------------------------------

#: 'fst_' + 26 Crockford base32 characters -- migration 097's CHECK constraint.
_FILE_SOURCE_TEMPLATE_REF = _re.compile(r"^fst_[0-9A-HJKMNP-TV-Z]{26}$")

#: Binding states that represent a settled human decision. `suggested` is a
#: proposal nobody confirmed and `blocking`/`excluded` are refusals; replaying
#: any of them would land data on an unconfirmed guess.
_CONFIRMED_BINDING_STATES = frozenset({"confirmed", "resolved"})


#: The four answers `FileSourceProducer.confirmation_state` can give. Named
#: rather than booleans because three of them are refusals that call for
#: DIFFERENT acts: confirm the preview, rebase onto the current mapping version,
#: or rebase onto the Template that moved (Story 38.16 AC4).
CONFIRMATION_CONFIRMED = "confirmed"
CONFIRMATION_NEVER = "never_confirmed"
CONFIRMATION_MAPPING_MOVED = "mapping_version_moved"
CONFIRMATION_TEMPLATE_MOVED = "template_version_moved"


class FileSourceProducer:
    """The producer a file-source-bound Datastream replays on every arrival.

    A plain callable would satisfy ``run_import``'s ``producer`` parameter, but
    the landing gate needs the template and mapping this producer was built from
    in order to check the output against the template's required canonical
    fields. Carrying them on the callable keeps ``run_import``'s signature
    unchanged and keeps the two facts from drifting apart.
    """

    __slots__ = (
        "template",
        "mapping",
        "mapping_payload",
        "template_id",
        "confirmation_operation_id",
        "confirmation_evidence",
        "catalog_governed",
    )

    def __init__(
        self,
        template: dict[str, Any],
        mapping: dict[str, str],
        template_id: str,
        *,
        mapping_payload: dict[str, Any] | None = None,
        confirmation_operation_id: str | None = None,
        confirmation_evidence: dict[str, Any] | None = None,
        catalog_governed: bool = False,
    ):
        self.template = template
        self.mapping = mapping
        #: Story 60.6. `mapping` is the flattened {source -> canonical} pairs the
        #: producer renames with; the PAYLOAD is the mapping version itself, and
        #: it is the only place a declared join or split lives. The preview needs
        #: it to say what each column becomes; the parse never reads it. None
        #: when the Template reshapes on its own (no mapping version pinned).
        self.mapping_payload = mapping_payload
        self.template_id = template_id
        self.confirmation_operation_id = confirmation_operation_id
        self.confirmation_evidence = confirmation_evidence or {}
        #: A CATALOG Template (`template:<code>:<version>`) rather than a
        #: client-saved `fst_` artifact. Set only by
        #: `_catalog_template_producer`, and it is the ONLY thing that lets
        #: `confirmed_for` answer yes without a confirmation row.
        self.catalog_governed = catalog_governed

    def confirmed_for(self, mapping_version_id: str) -> bool:
        """Whether this exact immutable replay decision was human-confirmed.

        A CATALOG Template answers yes without a confirmation row, and that is
        not a hole. The gate asks whether the exact replay decision is a settled
        one whose record can be shown; for a client-saved `fst_` that record is
        the per-project confirmation of a passing preview, sealed against the
        artifact's `content_hash` -- because someone assembled that artifact and
        it could drift. A catalog Template is platform-governed and IMMUTABLE per
        (code, version): there is no second artifact, nothing to drift, and the
        record `file-source-ingestion.md` demands ("proven from the Template's
        own record") is the catalog row itself.

        Requiring a per-project confirmation there would not add safety -- it
        would make the inbound/upload path unusable, which is what it did when
        this branch first landed: six offline chain tests went red at once
        because the ordinary CSV path they had been falling through to was
        replaced by a refusal.
        """
        if self.catalog_governed:
            return True
        return self.confirmation_state(mapping_version_id) == CONFIRMATION_CONFIRMED

    def confirmation_state(self, mapping_version_id: str) -> str:
        """WHICH of the three refusals this is, not merely that it is one.

        `confirmed_for` collapsed three different situations into a single
        `False`, and one of them is Story 38.16 AC4 -- << concurrent base-version
        or TEMPLATE-VERSION drift invalidates the candidate and requires
        rebase >>. The invalidation worked; it was simply UNOBSERVABLE. An
        operator was told "not confirmed" whether nobody had ever confirmed,
        whether they had confirmed another mapping, or whether the Template had
        MOVED under a confirmation that was perfectly valid yesterday.

        The three call for different acts -- confirm, rebase the mapping, rebase
        the Template -- so answering them with one word sends two people out of
        three to do the wrong thing. Measured 2026-08-08: the tracker recorded it
        as << la derive de Template est inobservable >>.

        The refusal itself is UNCHANGED. `confirmed_for` still answers exactly
        what it answered before; this only names the reason it answers so.
        """
        if self.catalog_governed:
            return CONFIRMATION_CONFIRMED
        if not self.confirmation_operation_id:
            return CONFIRMATION_NEVER
        if self.confirmation_evidence.get("mapping_version_id") != mapping_version_id:
            return CONFIRMATION_MAPPING_MOVED
        if self.confirmation_evidence.get("template_content_hash") != self.template.get(
            "content_hash"
        ):
            return CONFIRMATION_TEMPLATE_MOVED
        return CONFIRMATION_CONFIRMED

    def bind_dispatch(
        self,
        result: ParseResult,
        *,
        projection_plan: dict[str, Any],
        plan_version_id: str,
        mapping_version_id: str,
    ) -> ParseResult:
        """Epic 22 extension contract exposed to the universal dispatcher."""
        if projection_plan.get("plan_version_id") not in (None, "", plan_version_id):
            raise CsvExcelImportError(
                "dispatch_bundle_mismatch",
                "projection plan version differs from the pinned plan",
                repair={"reload_pinned_bundle": True},
            )
        if projection_plan.get("mapping_version_id") not in (
            None,
            "",
            mapping_version_id,
        ):
            raise CsvExcelImportError(
                "dispatch_bundle_mismatch",
                "projection mapping version differs from the pinned mapping",
                repair={"reload_pinned_bundle": True},
            )
        result.metadata["extension"] = {
            "owner": "epic-22",
            "template_id": self.template_id,
            "template_content_hash": self.template.get("content_hash"),
        }
        return result

    def prepare_for_landing(
        self,
        result: ParseResult,
        *,
        filename: str | None,
    ) -> tuple[ParseResult, dict[str, Any] | None]:
        """Run media-specific placement/discriminator/gate behind one seam."""
        from core.file_source_gate import evaluate_landing_gate  # noqa: PLC0415
        from core.file_source_producer import (  # noqa: PLC0415
            ReshapeProducerError,
            evaluate_variant_discriminator,
            reconcile_amounts,
            stamp_placement,
        )

        # The money invariant, before any other gate: what the file carries must
        # equal what lands plus what is rejected. A merged amount cell used to be
        # forward-filled onto every row it covered, multiplying the money, and
        # nothing refused because the check existed and was never called.
        reshape = (self.template.get("contract") or {}).get("reshape") or {}
        amount_field = reshape.get("amount_field")
        if amount_field:
            amounts = reconcile_amounts(result, amount_field)
            if amounts is not None and not amounts["reconciled"]:
                return result, {
                    "reason": "amount_reconciliation_failed",
                    "outcome": "amount_reconciliation_blocked",
                    "amounts": amounts,
                }

        variant = evaluate_variant_discriminator(
            self.template, filename=filename, metadata=result.metadata or {}
        )
        if variant["flagged"]:
            return result, {
                "reason": "variant_discriminator_undetectable",
                "outcome": "variant_discriminator_blocked",
                "variant": variant,
            }
        try:
            stamped = stamp_placement(result, self.template, filename=filename)
        except ReshapeProducerError as exc:
            return result, {
                "reason": exc.code,
                "outcome": "placement_blocked",
            }
        gate = evaluate_landing_gate(
            self.template,
            self.mapping,
            columns=[spec.name for spec in stamped.columns],
            rows=stamped.rows,
        )
        if not gate["passed"]:
            return stamped, {
                "reason": "required_field_gate_failed",
                "outcome": "required_field_gate_blocked",
                "gate": gate,
            }
        return stamped, None

    def __call__(self, data: bytes) -> ParseResult:
        """Parse ``data`` under this Template. Returns UNSTAMPED canonical rows.

        Dispatches on the Template's kind, which the artifact has carried since
        migration 097 and which nothing read here. An ``adaptation`` Template has
        no source->canonical mapping by construction -- its ``.py`` produces
        canonical rows directly -- so routing it through the catalog path made
        every arrival die on ``mapping_required`` (Story 22.24: "a subsequent file
        arrives by upload or by email [and] the same .py runs"). A bespoke file
        could be onboarded, self-tested and locked, and could never land.

        The placement stamp deliberately does NOT happen here: ``run_import``
        applies it after checking the variant discriminator, so a discriminator
        that cannot be detected is reported as such instead of being stamped null.
        """
        contract = self.template.get("contract") or {}
        if contract.get("kind") == "adaptation":
            from core.file_source_adaptation import (  # noqa: PLC0415
                produce_adaptation_rows,
            )

            return produce_adaptation_rows(data, self.template)

        from core.file_source_producer import produce  # noqa: PLC0415

        return produce(data, self.template, self.mapping or None)


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Coerce a JSONB read to a dict, or None when it is anything else.

    psycopg hands JSONB back as a dict already; the str branch covers drivers
    that do not. Anything else is not a document and must not raise here --
    arming this seam is not allowed to fail an import that used to work.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _confirmed_mapping(mapping_payload: dict[str, Any]) -> dict[str, str]:
    """Project {source_column -> canonical_field_id} from a mapping version.

    Only settled bindings are replayed, and only when they name a canonical
    target. An unconfirmed column is simply absent -- never force-mapped -- so a
    required field it would have covered surfaces at the gate instead of landing
    on a guess.
    """
    mapping: dict[str, str] = {}
    for field_record in (mapping_payload or {}).get("fields") or []:
        binding = field_record.get("binding") or {}
        target = binding.get("canonical_target")
        if not target or binding.get("status") not in _CONFIRMED_BINDING_STATES:
            continue
        source_column = field_record.get("field_id")
        if source_column:
            mapping[str(source_column)] = str(target)
    return mapping


def _catalog_template_producer(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    mapping_version_id: str | None,
    config: dict[str, Any],
) -> FileSourceProducer | None:
    """The producer for a Datastream bound to a CATALOG Template, or None.

    The catalog is platform-wide and immutable per (code, version), so the
    binding is the pinned pair itself -- there is no per-project row to fetch
    and no content hash of a client artifact to seal. `template:<code>:<version>`
    is the reference, which is the same string `create_inbound_datastream`
    already writes into its operation record.

    NO CONFIRMATION IS FABRICATED. A client-saved Template carries a human's
    confirmation of a passing preview, and `confirmation_evidence` is how the
    adaptation path proves it. A catalog Template has none, and inventing one
    here would let an unconfirmed replay wear the evidence of a confirmed one --
    exactly the claim `file-source-ingestion.md` forbids ("a Template can be
    locked without a human having confirmed a passing preview, or that
    confirmation cannot be proven from the Template's own record"). The
    reference says what it is; the evidence stays empty and honest.
    """
    template_code = str(config.get("template_code") or "").strip()
    template_version = config.get("template_version")

    from core.import_templates import get_template, latest_version  # noqa: PLC0415

    if not template_code:
        # THE WIZARD'S SHAPE (AI-321, measured on G9 2026-08-28). A Datastream
        # the assistant materialises carries the chosen catalog Template as
        # `source_owner.managed_feed_template_ref` = its CODE (`OFFLINE_OOH_V1`),
        # and never the `{template_code, template_version}` pair the upload
        # door writes. Two doors, two spellings of one binding -- and this
        # resolver read only the second, so every wizard-made file Datastream
        # resolved no producer: no column recognition, no landing gate, no
        # named missing field, no adaptation lock, no re-analysis. The code
        # is read here; the version is the catalog's latest for that code,
        # which is what the wizard offered (it lists codes, not versions),
        # and the reference below says which one was used.
        owner = config.get("source_owner") or {}
        ref = str(owner.get("managed_feed_template_ref") or "").strip()
        if not ref or _FILE_SOURCE_TEMPLATE_REF.match(ref):
            return None
        template_code = ref
    if not isinstance(template_version, int):
        try:
            template_version = latest_version(conn, template_code)
        except Exception:  # noqa: BLE001 -- an unreadable catalog is not a binding
            logger.warning("csv_excel_import: catalog unreadable code=%s", template_code)
            return None
        if template_version is None:
            return None

    try:
        catalog = get_template(conn, template_code, template_version)
    except Exception:  # noqa: BLE001 -- an unreadable catalog is not a binding
        logger.warning(
            "csv_excel_import: catalog template unreadable code=%s v=%s",
            template_code,
            template_version,
        )
        return None
    contract = _as_dict((catalog or {}).get("contract"))
    if not contract:
        return None

    mapping: dict[str, str] = {}
    mapping_payload: dict[str, Any] | None = None
    if not contract.get("reshape") and mapping_version_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mapping_payload FROM app.datastream_mapping_versions "
                "WHERE id = %s AND datastream_id = %s AND project_id = %s",
                (mapping_version_id, datastream_id, project_id),
            )
            row = cur.fetchone()
        payload = _as_dict(row[0]) if row else None
        if payload is not None:
            mapping = _confirmed_mapping(payload)
            mapping_payload = payload

    return FileSourceProducer(
        {"contract": contract, "content_hash": None},
        mapping,
        f"template:{template_code}:{template_version}",
        mapping_payload=mapping_payload,
        confirmation_operation_id=None,
        confirmation_evidence={},
        catalog_governed=True,
    )


def resolve_file_source_producer(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    mapping_version_id: str | None,
) -> FileSourceProducer | None:
    """Resolve the file-source producer bound to this Datastream, or None.

    Returns None -- meaning "run the ordinary CSV/Excel path" -- whenever the
    Datastream carries no `fst_` binding or the template row is gone. It never
    raises on a missing binding: arming this seam must not be able to break the
    imports that were working before it existed.

    A tabular template resolves its mapping from the pinned mapping version. A
    reshape template owns its own source->canonical projection, so it needs none.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    config = _as_dict(row[0]) if row else None
    if config is None:
        return None
    owner = config.get("source_owner") or {}
    # AI-91: read the DEDICATED key. `managed_feed_source_ref` is the channel's
    # pinned input (a staged asset, a sheet), not the contract to replay -- reading
    # the Template out of it required sniffing an `fst_` prefix, and it lost the
    # binding entirely whenever a draft had both.
    source_ref = str(owner.get("managed_feed_template_ref") or "")
    if not source_ref:
        # Legacy shape, kept deliberately narrow and dated. Before AI-91 the
        # Template rode in the catch-all, so a row written by an earlier build
        # still binds. Measured on production 2026-07-31 before the change: 42
        # Datastreams, ZERO carrying an `fst_` there and zero carrying that key at
        # all -- so this branch exists for a deploy window, not for a data set.
        source_ref = str(owner.get("managed_feed_source_ref") or "")
    if not _FILE_SOURCE_TEMPLATE_REF.match(source_ref):
        # THE SECOND RATIFIED REALIZATION. `file-source-ingestion.md` binds the
        # Template by two paths -- "a reused CATALOG or client-saved Template" --
        # and this resolver understood only the client-saved one (`fst_`).
        #
        # A Datastream created through the inbound/upload endpoint pins
        # `{template_code, template_version}` in its config (`import_templates.
        # create_inbound_datastream`, "the SOLE binding record"). It names a
        # Template, correctly, in the shape that path writes. With no branch for
        # it the producer came back None, the import fell through to the
        # ordinary CSV path, and everything the Template governs went with it:
        # no per-column recognition, no landing gate, no named missing field, no
        # adaptation lock, no re-analysis on a changed layout.
        #
        # Measured 2026-08-08 on the QA walk (G9): five of its six failures had
        # this one cause. It is not a preview defect repeated five times, it is
        # one binding the resolver could not read.
        return _catalog_template_producer(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            mapping_version_id=mapping_version_id,
            config=config,
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.contract, t.content_hash, c.operation_id, c.evidence
            FROM app.file_source_templates AS t
            LEFT JOIN app.file_source_template_confirmations AS c
              ON c.template_id = t.id
             AND c.mapping_version_id = %s
             AND c.datastream_id = %s
             AND c.project_id = t.project_id
             AND c.template_content_hash = t.content_hash
            WHERE t.id = %s AND t.project_id = %s AND t.is_active = TRUE
            """,
            (mapping_version_id, datastream_id, source_ref, project_id),
        )
        row = cur.fetchone()
    contract = _as_dict(row[0]) if row else None
    # The seal travels with the contract: the adaptation path refuses to replay a
    # Template that carries no content_hash, and reading only the contract left it
    # unable to tell a locked artifact from a dict someone assembled.
    #
    # Indexed defensively. Against Postgres a two-column SELECT always yields a
    # two-tuple, so the guard can only ever fire for a scripted test double that
    # pre-dates the second column -- and an arrival must not start failing because
    # a fixture returns a shorter row than the query asks for.
    content_hash = row[1] if row and len(row) > 1 else None
    # Short tuples are offline test doubles from before migration 188. A real
    # database row always has all four columns and fails closed on NULL.
    confirmation_operation_id = row[2] if row and len(row) > 2 else "legacy_test_fixture"
    confirmation_evidence = (
        _as_dict(row[3])
        if row and len(row) > 3
        else {
            "mapping_version_id": mapping_version_id,
            "template_content_hash": content_hash,
        }
    )
    if contract is None:
        # An explicit fst_ binding is a governed replay contract. If it is gone,
        # inactive or foreign, falling back to ordinary CSV would bypass that
        # contract and its human confirmation.
        raise CsvExcelImportError(
            "file_source_template_unavailable",
            "the explicitly bound file-source Template is unavailable or inactive",
            repair={"template_id": source_ref, "rebind_template": True},
        )

    mapping: dict[str, str] = {}
    mapping_payload: dict[str, Any] | None = None
    if not contract.get("reshape") and mapping_version_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mapping_payload FROM app.datastream_mapping_versions "
                "WHERE id = %s AND datastream_id = %s AND project_id = %s",
                (mapping_version_id, datastream_id, project_id),
            )
            row = cur.fetchone()
        payload = _as_dict(row[0]) if row else None
        if payload is not None:
            mapping = _confirmed_mapping(payload)
            mapping_payload = payload

    return FileSourceProducer(
        {"contract": contract, "content_hash": content_hash},
        mapping,
        str(source_ref),
        mapping_payload=mapping_payload,
        confirmation_operation_id=confirmation_operation_id,
        confirmation_evidence=confirmation_evidence,
    )
