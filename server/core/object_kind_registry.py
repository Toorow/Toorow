"""A client-declared object kind, and the source that feeds it (Story 64.1).

This module is domain-free on purpose, exactly like :mod:`core.master_data`
underneath it. It does not know what a video, a venue or a product is; it knows
that a Project may declare an object kind, that a Datastream feeds it, and that
an object without a stable identity is not an object.

WHAT IT ADDS TO WHAT ALREADY EXISTED. `master_data.create_registry` has minted a
generic owner per (Project, object_kind) since migration 140, and `object_kind`
is an opaque string the core never compares to a literal. It had exactly ONE
caller in the repository -- :mod:`core.country_registry`. The generic store was
therefore reachable by geography and by nothing else. What was missing is not the
store, it is the sentence "this kind is fed by that source".

THE IDENTITY IS THE MAPPING'S GRAIN, AND IT IS NOT COPIED HERE. A Datastream's
published mapping already declares which fields identify a row --
`mapping_payload.grain`. `governance.md:82` is explicit that Governance consumes
mapping versions and never keeps a second copy, so the binding PINS a version and
reads the grain from it. A copy could disagree with its source, and the copy is
the one Governance would believe.

WHY A DECLARATION MAY BE REFUSED. An object kind whose feeding mapping declares
no grain has no stable identity: two rows spelling the same thing are the same
object on Monday and two objects on Tuesday, and every alias resolved against it
(Story 64.10) inherits that instability. The refusal is the whole value of the
declaration, so it lives in a pure function that a test can exercise without a
database -- the discipline `tests/core/test_master_data.py` states in its own
header.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.audit import declare_action, insert_audit_row
from core.master_data import (
    MasterDataConflict,
    MasterDataError,
    MasterDataNotFound,
    _kind,
    _label,
    _mint,
    _required,
    _row,
    create_registry,
    fetch_registry,
)

# ---------------------------------------------------------------------------
# What this module writes to the audit trail -- AD-42: an action is declared
# where it is WRITTEN, never in a central list nobody owns.
# ---------------------------------------------------------------------------

ACTION_ENTITY_TYPE_DECLARED = declare_action("mdm.entity_type.declared")

_BINDING_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "registry_id",
    "datastream_id",
    "mapping_version_id",
    "namespace",
    "identity_mode",
    "label_field",
    "created_by",
    "created_at",
    "released_at",
    "released_by",
)


# ---------------------------------------------------------------------------
# The refusals. Pure, so they are provable without a database.
# ---------------------------------------------------------------------------


def identity_fields(mapping_payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The field ids that identify one row of the feeding Datastream.

    Reads `grain` from a published mapping payload and nothing else. Order is
    preserved because the grain is an ordered declaration and a reader shows it
    as the operator wrote it; duplicates are not collapsed here -- a grain that
    repeats a field is a malformed mapping, and :func:`validate_declaration`
    says so rather than quietly deduplicating it.
    """

    grain = (mapping_payload or {}).get("grain")
    if grain is None:
        return ()
    if isinstance(grain, str) or not isinstance(grain, Sequence):
        raise MasterDataError("mapping grain must be a list of field ids")
    return tuple(str(field) for field in grain)


IDENTITY_MODES = ("source_key", "governed_label")


def mapped_field_ids(mapping_payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The field ids the pinned mapping declares. Read, never copied."""

    fields = (mapping_payload or {}).get("fields") or ()
    return tuple(
        str(entry.get("field_id"))
        for entry in fields
        if isinstance(entry, Mapping) and entry.get("field_id")
    )


def validate_declaration(
    *,
    object_kind: str,
    label: str,
    mapping_payload: Mapping[str, Any] | None,
    identity_mode: str = "source_key",
    label_field: str | None = None,
) -> tuple[str, ...]:
    """Return the identity fields, or refuse the declaration with the reason.

    TWO MODES, AND THE SECOND IS THE MAJORITY CASE (Story 64.13). Measured on the
    workbook that motivated this epic, ONE entity of five carries a key:

        video 531 rows / 527 ids | restaurant, produit, recette, film: NO key

    The first version of this function knew only `source_key` and refused the
    other four outright -- including `restaurant`, the entity carrying the
    measured factor 3 in audience. A resolver that refuses ambiguity is worth
    nothing if the declaration refuses the objects.

    ``source_key``      the pinned mapping's grain IS the identity.
    ``governed_label``  the source carries no key. ``label_field`` names the
                        column whose normalized value resolves through
                        ``master_data_aliases``, and the NODE is the identity.

    The refusals, each naming what would break downstream:

    * a malformed kind or label -- the shape guards of the generic core, applied
      here so a caller learns at declaration time rather than at INSERT time;
    * ``source_key`` with NO GRAIN -- no stable identity. Two rows spelling the
      same thing would be one object today and two tomorrow, and every alias
      resolved against that identity (Story 64.10) inherits the instability;
    * an EMPTY or REPEATED field id inside the grain -- a malformed mapping,
      reported rather than quietly deduplicated, because the repair belongs to
      the Datastream's Mapping workbench;
    * ``governed_label`` with no ``label_field``, or one the mapping does not
      declare -- a label column that does not exist resolves nothing, and the
      declaration would look complete while producing no object at all;
    * ``governed_label`` carrying a grain -- not an error in the data, a
      CONTRADICTION in the declaration: a source that has a key should say so and
      be joined on it, not resolved by spelling.
    """

    _kind(object_kind, "object_kind")
    _label(label, "label")
    if identity_mode not in IDENTITY_MODES:
        raise MasterDataError(
            f"unknown identity mode {identity_mode!r} -- expected one of {IDENTITY_MODES}"
        )

    fields = identity_fields(mapping_payload)

    if identity_mode == "governed_label":
        if not label_field or not str(label_field).strip():
            raise MasterDataError(
                "a governed_label source must name the field carrying the label -- "
                "without it there is nothing to resolve"
            )
        declared = mapped_field_ids(mapping_payload)
        if declared and label_field not in declared:
            raise MasterDataError(
                f"the mapping declares no field {label_field!r} -- the label column "
                "must exist in the pinned mapping version"
            )
        if fields:
            raise MasterDataError(
                "this source declares a grain, so it HAS a key: declare it as "
                "source_key and join on it rather than resolving it by spelling"
            )
        return ()

    if label_field:
        raise MasterDataError(
            "a source_key source is identified by its grain -- a label field here "
            "would be read by nobody"
        )
    if not fields:
        raise MasterDataError(
            "the feeding mapping declares no grain, so this object kind has no "
            "stable identity -- declare the grain on the Datastream's mapping "
            "first, or declare this source as governed_label"
        )
    if any(not field.strip() for field in fields):
        raise MasterDataError("the mapping grain carries an unnamed field")
    if len(set(fields)) != len(fields):
        raise MasterDataError(
            "the mapping grain names a field twice -- repair it on the Datastream's "
            "mapping rather than here"
        )
    return fields


# ---------------------------------------------------------------------------
# The declaration itself. Callers own the transaction (the module-wide rule of
# core.master_data: a mutation and its audit evidence commit together).
# ---------------------------------------------------------------------------


def declare_object_kind(
    conn,
    *,
    org_id: str,
    project_id: str,
    object_kind: str,
    label: str,
    datastream_id: str,
    mapping_version_id: str,
    namespace: str,
    actor: str,
    identity_mode: str = "source_key",
    label_field: str | None = None,
) -> dict[str, Any]:
    """Declare an object kind for a Project and bind the source that feeds it.

    Idempotent on the registry (``create_registry`` reads back an existing owner).
    The binding is refused only within its OWN namespace: a kind may be fed by
    several sources -- the dossier's `video` arrives from the public connector as
    measures and from the client workbook as annotations -- and forbidding the
    second was Story 64.1's mistake (64.13). What keeps "one identity per object"
    is that every mode lands on the same ``node_id``, not a single feeder.

    ``version_scope="node"`` is passed explicitly and is the whole point of a
    client object: each video, each product carries its OWN payload of
    properties. Left to the column default it would have been one payload for the
    entire kind.
    """

    payload = _mapping_payload(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        mapping_version_id=mapping_version_id,
    )
    fields = validate_declaration(
        object_kind=object_kind,
        label=label,
        mapping_payload=payload,
        identity_mode=identity_mode,
        label_field=label_field,
    )

    registry = create_registry(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=object_kind,
        label=label,
        actor=actor,
        version_scope="node",
    )

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_source_bindings
                (id, org_id, project_id, registry_id, datastream_id,
                 mapping_version_id, namespace, identity_mode, label_field, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {", ".join(_BINDING_COLUMNS)}
            """,
            (
                _mint("mdsrc"),
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
                registry["id"],
                _required(datastream_id, "datastream_id"),
                _required(mapping_version_id, "mapping_version_id"),
                _required(namespace, "namespace"),
                identity_mode,
                label_field,
                _required(actor, "actor"),
            ),
        )
        row = cur.fetchone()

    if row is None:
        # The partial unique index refused a second live feeder IN THIS NAMESPACE.
        # Say which one holds it: "already bound" without the holder sends nobody
        # anywhere, and the namespace is the half that tells them it is not a
        # conflict with the OTHER source they just added.
        existing = fetch_source_binding(
            conn, project_id=project_id, registry_id=registry["id"], namespace=namespace
        )
        holder = (existing or {}).get("datastream_id", "unknown")
        raise MasterDataConflict(
            f"object kind {object_kind!r} is already fed in namespace {namespace!r} "
            f"by datastream {holder} -- release that binding before declaring another"
        )

    binding = _row(row, _BINDING_COLUMNS, "source binding")
    return {"registry": registry, "binding": binding, "identity_fields": list(fields)}


# ---------------------------------------------------------------------------
# The feeder-less declaration (Story 68.1). A registry IS the entity type;
# this door declares it as configuration, before any source feeds it (68.2).
# ---------------------------------------------------------------------------


class EntityTypeExists(MasterDataConflict):
    """The kind is already declared HERE, with a DIFFERENT key or label.

    A subclass of the generic conflict so a door can name the exact refusal
    (``entity_type_exists``) while a caller that only knows the generic shape
    still catches it. A REPLAY of the same declaration is not this error -- it
    returns the existing registry, and the two stay distinguishable.
    """

    code = "entity_type_exists"


def validate_entity_type_declaration(
    *, object_kind: Any, canonical_key: Any, display_name: Any
) -> tuple[str, str, str]:
    """The normalized declaration, or a refusal naming what to repair.

    Pure, like :func:`validate_declaration` above: the shape guards of the
    generic core applied at declaration time, so a caller learns here rather
    than at INSERT time. ``canonical_key`` follows the kind's own snake_case
    rule -- it names a field the same way the kind names a type.
    """

    return (
        _kind(object_kind, "object_kind"),
        _kind(canonical_key, "canonical_key"),
        _label(display_name, "display_name"),
    )


def declaration_matches(
    registry: Mapping[str, Any], *, canonical_key: str, display_name: str
) -> bool:
    """True when an existing registry IS this declaration -- the replay path.

    Compared on the WHOLE declaration: same kind is necessary and not enough,
    because the refusal must tell "you already said exactly this" apart from
    "this kind means something else here".
    """

    return (
        str(registry.get("canonical_key") or "") == canonical_key
        and str(registry.get("label") or "") == display_name
    )


def _entity_type_conflict(object_kind: str, holder: Mapping[str, Any]) -> EntityTypeExists:
    """The named refusal, naming the kind AND its holder.

    "Already declared" without the holder sends nobody anywhere: the id is
    what a screen opens, and the declared key and label are what the caller
    compares against its own declaration to find the difference.
    """

    return EntityTypeExists(
        f"entity type {object_kind!r} is already declared in this Project by "
        f"registry {holder.get('id')} (declared by {holder.get('created_by')}) with "
        f"canonical key {holder.get('canonical_key')!r} and label {holder.get('label')!r} "
        "-- replaying the SAME declaration returns it; a different one is another "
        "authority over the same kind"
    )


def _record_entity_type(conn, *, actor: str, project_id: str, **metadata: Any) -> None:
    """One audit row, ON the caller's transaction.

    `insert_audit_row` rather than `write_audit_row`: the second opens its own
    connection, so a declaration later rolled back would leave behind the row
    asserting it happened. The gesture and its evidence commit together or not
    at all -- the rule `core.master_data` states for every mutation.
    """

    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=ACTION_ENTITY_TYPE_DECLARED,
        provider_account="",
        connection_ref="",
        metadata={"project_id": project_id, **metadata},
    )


def declare_entity_type(
    conn,
    *,
    org_id: str,
    project_id: str,
    object_kind: Any,
    canonical_key: Any,
    display_name: Any,
    actor: str,
) -> dict[str, Any]:
    """Declare an entity type as governed configuration. No feeder required.

    A registry IS the entity type (migration 140), so the declaration is the
    registry row and nothing else: ``master_data_source_bindings`` stays empty
    until a source is bound (Story 68.2), and "declared, not fed" reads as
    ``live_source_count = 0`` in the registries lens -- never as "never
    declared".

    IDEMPOTENCE VS REFUSAL. ``create_registry`` alone cannot serve here: its
    ``ON CONFLICT DO NOTHING`` re-read returns the existing row whether or not
    it is THIS declaration, and a silent re-read is how a replay and a real
    duplicate become indistinguishable. So the existing row is read FIRST and
    the whole declaration compared:

    * same kind + same key + same label -- a replay: the existing registry is
      returned with ``replayed=True`` and NOTHING is written, not even an
      audit row (no mutation happened; an audit row asserting one would be a
      lie);
    * same kind, different key or label -- the named conflict
      :class:`EntityTypeExists`, naming the kind and the registry that holds
      it.

    ``version_scope="node"`` is passed explicitly, exactly as in
    :func:`declare_object_kind`: each instance of the declared type carries
    its OWN payload of properties, and the column default would give one
    payload to the whole kind.

    The caller owns the transaction: the mutation and its audit row commit
    together.
    """

    kind, key, name = validate_entity_type_declaration(
        object_kind=object_kind, canonical_key=canonical_key, display_name=display_name
    )

    existing = fetch_registry(conn, project_id=project_id, object_kind=kind)
    if existing is not None:
        if declaration_matches(existing, canonical_key=key, display_name=name):
            return {"registry": existing, "replayed": True}
        raise _entity_type_conflict(kind, existing)

    registry = create_registry(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=kind,
        label=name,
        actor=actor,
        version_scope="node",
        canonical_key=key,
    )
    if not declaration_matches(registry, canonical_key=key, display_name=name):
        # A concurrent declare won the race and `create_registry` re-read ITS
        # row under ON CONFLICT. That row is not this declaration: same named
        # refusal as the fetch-first path, never a silent success.
        raise _entity_type_conflict(kind, registry)

    _record_entity_type(
        conn,
        actor=actor,
        project_id=project_id,
        registry_id=registry["id"],
        object_kind=kind,
        canonical_key=key,
        display_name=name,
    )
    return {"registry": registry, "replayed": False}


# ---------------------------------------------------------------------------
# The key binding of Story 68.2: a mapping column declares which entity type
# its values are keys OF. The declaration lives IN the mapping payload -- the
# governed, immutable, pinnable store; `governance.md:82` forbids a second
# one -- and everything below either READS it or refuses it, purely.
# ---------------------------------------------------------------------------

#: The stable half of every designation refusal. A binding's
#: `blocking_reason` carries ``<reason>:<kind>`` -- the parameterized shape
#: `target_field_not_found:<field>` already uses in the mapping contract, so a
#: screen reads a stable code AND the type it concerns.
REASON_TYPE_UNDECLARED = "entity_type_not_declared"
REASON_TYPE_ARCHIVED = "entity_type_archived"
REASON_KEY_UNDECLARED = "entity_type_key_not_declared"
REASON_CONTRADICTS_MDM = "entity_designation_contradicts_mdm_target"

#: The designation could not be VERIFIED (the lookup itself failed).
#: Fail-closed, exactly like the mdm registry resolution in
#: `save_field_mapping`: an unreadable registry blocks rather than passes.
REASON_LOOKUP_FAILED = "entity_type_lookup_failed"

#: The named group a read surface counts (AC4, AD-9): columns the payload's
#: OWN profile marks as candidate keys whose binding declares nothing.
UNDECLARED_KEY_CANDIDATES = "undeclared_key_candidates"


@dataclass(frozen=True)
class EntityDesignationIssue:
    """One refused designation: the column, the type, and the stable reason.

    ``message`` is the sentence a human reads; it names where the repair
    belongs, because "invalid designation" alone sends the operator to the
    wrong desk.
    """

    field_id: str
    object_kind: str
    reason: str
    message: str


def entity_designations(
    mapping_payload: Mapping[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    """The ``(field_id, object_kind)`` pairs a mapping payload designates.

    Read from `fields[].binding.designates_object_kind` and nothing else. An
    ABSENT key and an explicit null both yield no pair here -- the two stay
    distinguishable in :func:`entity_designation_groups`, because "nobody
    declared anything" and "declared: this column designates nothing" are not
    the same fact (AC4).
    """

    pairs = []
    for entry in (mapping_payload or {}).get("fields") or ():
        if not isinstance(entry, Mapping):
            continue
        binding = entry.get("binding")
        if not isinstance(binding, Mapping):
            continue
        kind = binding.get("designates_object_kind")
        if isinstance(kind, str) and kind.strip():
            pairs.append((str(entry.get("field_id") or ""), kind))
    return tuple(pairs)


def validate_entity_designations(
    mapping_payload: Mapping[str, Any] | None,
    *,
    declared_types: Mapping[str, Mapping[str, Any]],
    mdm_object_kinds: Mapping[str, Any] | None = None,
) -> tuple[EntityDesignationIssue, ...]:
    """The designations that cannot be honored, each with its named refusal.

    Pure, like :func:`validate_declaration` above: ``declared_types`` is what
    the caller already read from the registry Story 68.1 writes (kind ->
    ``canonical_key`` + ``lifecycle_state``) and ``mdm_object_kinds`` what it
    read from `mdm_canonical_fields` -- this function never touches a
    database, so a test proves the rules without one. The lookup stays a
    lookup, never a write (the Epic-13 boundary).

    The refusals, each naming where the repair belongs:

    * a type NOBODY DECLARED in this Project -- declaring it is the Master
      Data gesture (68.1), never something a mapping validation invents;
    * a type ARCHIVED (``disabled``) -- it was declared and is no longer
      honored; a draft carrying it must not publish as if it still were;
    * a type whose KEY SHAPE was never declared -- a registry that predates
      the 68.1 door carries no ``canonical_key``, and a key bound to a shape
      nobody declared reconciles on a guess;
    * a designation that CONTRADICTS the column's `mdm_target` -- the
      canonical field already says which object it qualifies
      (`mdm_canonical_fields.object_kind`, migration 241), and two different
      answers to "which object" is one answer too many. The mechanism
      composes with `mdm_common_keys`; it never forks a second identity
      model (AC5).
    """

    object_kinds = mdm_object_kinds or {}
    issues = []
    for entry in (mapping_payload or {}).get("fields") or ():
        if not isinstance(entry, Mapping):
            continue
        binding = entry.get("binding")
        if not isinstance(binding, Mapping):
            continue
        kind = binding.get("designates_object_kind")
        if not isinstance(kind, str) or not kind.strip():
            continue
        field_id = str(entry.get("field_id") or "")

        registry = declared_types.get(kind)
        if registry is None:
            issues.append(
                EntityDesignationIssue(
                    field_id=field_id,
                    object_kind=kind,
                    reason=REASON_TYPE_UNDECLARED,
                    message=(
                        f"entity type {kind!r} is not declared in this Project -- "
                        "declare it in Master Data (the 68.1 door), or remove the "
                        "designation in the Datastream's mapping"
                    ),
                )
            )
            continue
        if str(registry.get("lifecycle_state") or "") == "disabled":
            issues.append(
                EntityDesignationIssue(
                    field_id=field_id,
                    object_kind=kind,
                    reason=REASON_TYPE_ARCHIVED,
                    message=(
                        f"entity type {kind!r} was archived -- restore it in Master "
                        "Data, or remove the designation in the Datastream's mapping"
                    ),
                )
            )
            continue
        if not registry.get("canonical_key"):
            issues.append(
                EntityDesignationIssue(
                    field_id=field_id,
                    object_kind=kind,
                    reason=REASON_KEY_UNDECLARED,
                    message=(
                        f"entity type {kind!r} declares no canonical key -- the "
                        "68.1 declaration names the key shape; a registry that "
                        "predates it must be re-declared before a key binds to it"
                    ),
                )
            )
            continue

        mdm_target = binding.get("mdm_target")
        mdm_kind = object_kinds.get(mdm_target) if mdm_target else None
        if mdm_kind and mdm_kind != kind:
            issues.append(
                EntityDesignationIssue(
                    field_id=field_id,
                    object_kind=kind,
                    reason=REASON_CONTRADICTS_MDM,
                    message=(
                        f"the designation {kind!r} contradicts the canonical "
                        f"field's object kind {mdm_kind!r} -- two answers to "
                        "which object this column carries; keep one"
                    ),
                )
            )
    return tuple(issues)


def entity_designation_groups(
    mapping_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The named groups a read surface counts (AC4, AD-9).

    Only what the payload DECLARES lands in a group -- nothing is guessed:

    * ``designated`` -- field id -> entity type: the bound columns;
    * ``declared_untyped`` -- columns carrying an explicit null: somebody
      declared "this column designates nothing", which is not the same fact
      as an absent key;
    * ``undeclared_key_candidates`` -- columns the payload's OWN profile
      marks as a candidate key (``unique`` observed by profiling, never this
      reader's guess) whose binding declares nothing at all. This is the
      group a screen counts when it says "N key candidates are still
      unbound".
    """

    designated: dict[str, str] = {}
    declared_untyped: list[str] = []
    undeclared: list[str] = []
    for entry in (mapping_payload or {}).get("fields") or ():
        if not isinstance(entry, Mapping):
            continue
        field_id = str(entry.get("field_id") or "")
        binding = entry.get("binding")
        if not isinstance(binding, Mapping):
            binding = {}
        if "designates_object_kind" not in binding:
            profile = entry.get("profile")
            if isinstance(profile, Mapping) and (
                profile.get("unique") is True
                or profile.get("cardinality_signal") == "unique"
            ):
                undeclared.append(field_id)
        elif binding.get("designates_object_kind") is None:
            declared_untyped.append(field_id)
        else:
            designated[field_id] = str(binding["designates_object_kind"])
    return {
        "designated": designated,
        "declared_untyped": declared_untyped,
        UNDECLARED_KEY_CANDIDATES: undeclared,
    }


def fetch_entity_type_lookup(
    conn, *, project_id: str, object_kinds: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """The declared-types lookup the designation validation reads.

    One query for every kind a payload designates, scoped by project -- the
    SAME registry Story 68.1 writes, and a READ only: the Epic-13 boundary
    `save_field_mapping` states applies to every caller of this function.
    """

    kinds = sorted({str(kind) for kind in object_kinds if str(kind).strip()})
    if not kinds:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT object_kind, canonical_key, lifecycle_state
              FROM app.master_data_registries
             WHERE project_id = %s AND object_kind = ANY(%s)
            """,
            (_required(project_id, "project_id"), kinds),
        )
        rows = cur.fetchall()
    return {
        str(kind): {"canonical_key": key, "lifecycle_state": state}
        for kind, key, state in rows
    }


def release_source_binding(
    conn, *, project_id: str, registry_id: str, namespace: str, actor: str
) -> dict[str, Any]:
    """Retire ONE live binding, named by its namespace. Nothing is deleted.

    The namespace is required and not optional (Story 64.13): a registry may now
    be fed by several sources, and a release that took only the registry would
    have retired whichever one the database returned first -- the "resolved by
    write order" defect this epic refuses everywhere else.

    An old Result may pin what was true then, so a released binding stays readable
    with the date and the person who released it.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_source_bindings
               SET released_at = NOW(), released_by = %s
             WHERE project_id = %s AND registry_id = %s AND namespace = %s
               AND released_at IS NULL
            RETURNING {", ".join(_BINDING_COLUMNS)}
            """,
            (_required(actor, "actor"), project_id, registry_id, _required(namespace, "namespace")),
        )
        row = cur.fetchone()
    if row is None:
        raise MasterDataNotFound(
            f"no live source binding in namespace {namespace!r} for this registry"
        )
    return _row(row, _BINDING_COLUMNS, "source binding")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch_source_binding(
    conn, *, project_id: str, registry_id: str, namespace: str
) -> dict[str, Any] | None:
    """One live binding, named by its namespace."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_BINDING_COLUMNS)}
              FROM app.master_data_source_bindings
             WHERE project_id = %s AND registry_id = %s AND namespace = %s
               AND released_at IS NULL
            """,
            (project_id, registry_id, namespace),
        )
        row = cur.fetchone()
    return _row(row, _BINDING_COLUMNS, "source binding") if row is not None else None


def list_source_bindings(
    conn, *, project_id: str, registry_id: str
) -> list[dict[str, Any]]:
    """Every live binding of a registry, ordered by namespace.

    Ordered so two reads of an unchanged registry produce the same list: the
    dossier's `video` is fed by two sources, and a screen that reordered them
    between refreshes would look like it had changed something.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_BINDING_COLUMNS)}
              FROM app.master_data_source_bindings
             WHERE project_id = %s AND registry_id = %s AND released_at IS NULL
             ORDER BY namespace
            """,
            (project_id, registry_id),
        )
        rows = cur.fetchall()
    return [_row(row, _BINDING_COLUMNS, "source binding") for row in rows]


def list_entity_types(
    conn, *, project_id: str
) -> list[dict[str, Any]]:
    """Every entity type declared in this Project, with its feeding state.

    The same reading the ``_CLIENT_OBJECT_REGISTRIES`` lens serves the console
    (Story 68.1, AC3): ``live_source_count = 0`` is "declared, not fed", a
    state a reader must tell apart from "never declared" -- and from a read
    that failed, which the DOOR answers 503 rather than an empty list here.

    Ordered by label so two reads of an unchanged Project produce the same
    list. Timestamps are isoformatted: both doors (REST and MCP) serialize
    this shape verbatim.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.object_kind, r.canonical_key, r.label, r.lifecycle_state,
                   r.version_scope, r.created_by, r.created_at, r.updated_at,
                   (SELECT COUNT(*) FROM app.master_data_source_bindings b
                     WHERE b.registry_id = r.id AND b.released_at IS NULL
                   ) AS live_source_count,
                   (SELECT COUNT(*) FROM app.master_data_nodes n
                     WHERE n.registry_id = r.id AND n.archived_at IS NULL
                   ) AS node_count
              FROM app.master_data_registries r
             WHERE r.project_id = %s
             ORDER BY r.label, r.id
            """,
            (_required(project_id, "project_id"),),
        )
        rows = cur.fetchall()
    return [
        {
            "registry_id": row[0],
            "object_kind": row[1],
            "canonical_key": row[2],
            "display_name": row[3],
            "lifecycle_state": row[4],
            "version_scope": row[5],
            "created_by": row[6],
            "created_at": row[7].isoformat() if row[7] is not None else None,
            "updated_at": row[8].isoformat() if row[8] is not None else None,
            "live_source_count": int(row[9]),
            "node_count": int(row[10]),
        }
        for row in rows
    ]


def describe_object_kind(
    conn, *, project_id: str, object_kind: str
) -> dict[str, Any]:
    """What a reader needs about one declared kind, in one call.

    The registry, the Datastream that feeds it, the identity fields READ FROM the
    pinned mapping version (never from a copy), and how many live nodes it holds.

    `unavailable_reason` is the honest half: a kind whose registry exists but
    whose binding was released is NOT the same state as a kind that was never
    declared, and a screen that showed both as empty would send an operator to
    create something that already exists.
    """

    registry = fetch_registry(conn, project_id=project_id, object_kind=object_kind)
    if registry is None:
        raise MasterDataNotFound(f"no registry for object kind {object_kind!r}")

    bindings = list_source_bindings(conn, project_id=project_id, registry_id=registry["id"])
    unavailable_reason = None if bindings else "no_live_source_binding"

    # One entry per source, because the identity is answered differently by each
    # and a reader that saw only the first would believe a keyed feeder was the
    # whole story (Story 64.13).
    sources: list[dict[str, Any]] = []
    for binding in bindings:
        payload = _mapping_payload(
            conn,
            project_id=project_id,
            datastream_id=binding["datastream_id"],
            mapping_version_id=binding["mapping_version_id"],
        )
        sources.append(
            {
                "namespace": binding["namespace"],
                "datastream_id": binding["datastream_id"],
                "identity_mode": binding["identity_mode"],
                "identity_fields": list(
                    identity_fields(payload)
                    if binding["identity_mode"] == "source_key"
                    else ()
                ),
                "label_field": binding["label_field"],
            }
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM app.master_data_nodes
             WHERE project_id = %s AND registry_id = %s AND archived_at IS NULL
            """,
            (project_id, registry["id"]),
        )
        node_count = int(cur.fetchone()[0])

    return {
        "registry": registry,
        "sources": sources,
        "node_count": node_count,
        "unavailable_reason": unavailable_reason,
    }


# ---------------------------------------------------------------------------
# The discovery read (Story 68.7)
#
# ONE function, TWO doors: the MCP tool `list_entity_reconciliation_context`
# (core.entity_context_mcp) and the console REST route (core.entity_context_api)
# both serve :func:`describe_entity_reconciliation_context` and nothing else.
# A second implementation is how "what may I cross?" starts answering
# differently depending on who asks.
#
# Every section carries a `status`: "ok" with its counts, or "unavailable" with
# a NAMED reason. A dependency that has not landed (Story 68.3's matching) or
# one a partial checkout does not carry is reported unavailable -- never as a
# count of zero, because zero is a measurement nobody took (the CoverageReport
# rule in core.semantic_coverage). A database outage does not degrade a
# section either: it raises through, and the DOOR answers 503 / ToolError --
# "I could not look" is not "there is nothing".
# ---------------------------------------------------------------------------

#: The unavailable reasons, stable: a reader switches on the code, never on
#: the sentence.
REASON_ENTITY_MATCHING_PENDING = "entity_matching_pending"
REASON_ENTITY_RULE_SETS_PENDING = "entity_rule_sets_pending"

#: The named unattached groups (AC2, AD-9). The first is the group Story 68.2
#: names in the mapping payload itself (:data:`UNDECLARED_KEY_CANDIDATES`);
#: the second is Story 68.3's `unmatched` verdict, named here before the
#: verdicts table exists so its absence is declared, not silent.
UNATTACHED_UNDECLARED_KEY_CANDIDATES = UNDECLARED_KEY_CANDIDATES
UNATTACHED_UNMATCHED_OCCURRENCES = "unmatched_occurrences"


def _current_mapping_version_payloads(
    conn, *, project_id: str
) -> list[dict[str, Any]]:
    """The mapping version each published Datastream currently points at.

    The ONLY mapping that counts is the pinned one (the rule
    `semantic_coverage._ACTIVE_MAPPING_VERSIONS` states): a newer draft may
    exist and not be published, and a designation read from a draft would
    advertise a binding the Project has not decided on.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.name, v.id, v.version_number, v.mapping_payload
              FROM app.datastreams d
              JOIN app.datastream_mapping_versions v
                ON v.id = d.current_mapping_version_id
               AND v.datastream_id = d.id
               AND v.project_id = d.project_id
             WHERE d.project_id = %s
             ORDER BY d.name, d.id
            """,
            (_required(project_id, "project_id"),),
        )
        rows = cur.fetchall()
    versions = []
    for datastream_id, name, version_id, version_number, payload in rows:
        if isinstance(payload, str):  # pragma: no cover - driver-dependent
            import json

            payload = json.loads(payload)
        versions.append(
            {
                "datastream_id": str(datastream_id),
                "datastream_name": name,
                "mapping_version_id": str(version_id),
                "mapping_version_number": version_number,
                "mapping_payload": payload or {},
            }
        )
    return versions


def _entity_bindings_section(
    versions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The bindings section, and its unattached group. Pure (AC2, AC4 of 68.2).

    Only what the payload DECLARES lands here -- `entity_designation_groups`
    reads `fields[].binding.designates_object_kind` and the payload's own
    profile, and guesses nothing. Returns ``(section, unattached_group)`` so
    the group is assembled beside the section it is counted from.
    """

    designations: list[dict[str, Any]] = []
    by_object_kind: dict[str, int] = {}
    declared_untyped_count = 0
    unattached_by_datastream: list[dict[str, Any]] = []
    for version in versions:
        groups = entity_designation_groups(version["mapping_payload"])
        for field_id, kind in sorted(groups["designated"].items()):
            designations.append(
                {
                    "datastream_id": version["datastream_id"],
                    "datastream_name": version["datastream_name"],
                    "mapping_version_id": version["mapping_version_id"],
                    "field_id": field_id,
                    "object_kind": kind,
                }
            )
            by_object_kind[kind] = by_object_kind.get(kind, 0) + 1
        declared_untyped_count += len(groups["declared_untyped"])
        undeclared = list(groups[UNDECLARED_KEY_CANDIDATES])
        if undeclared:
            unattached_by_datastream.append(
                {
                    "datastream_id": version["datastream_id"],
                    "datastream_name": version["datastream_name"],
                    "field_ids": undeclared,
                    "count": len(undeclared),
                }
            )
    section = {
        "status": "ok",
        "designation_count": len(designations),
        "by_object_kind": dict(sorted(by_object_kind.items())),
        "declared_untyped_count": declared_untyped_count,
        "designations": designations,
    }
    unattached = {
        "group": UNATTACHED_UNDECLARED_KEY_CANDIDATES,
        "status": "ok",
        "count": sum(entry["count"] for entry in unattached_by_datastream),
        "by_datastream": unattached_by_datastream,
    }
    return section, unattached


def _load_entity_derivation_family() -> str | None:
    """The 68.6 contract, probed: the rule-set family of entity derivation.

    Story 68.6 mounts the derivation rule set on the generic lifecycle under
    `core.entity_rule_derivation.FAMILY_ENTITY_DERIVATION`. A checkout where it
    has not landed is not a Project with no rule sets -- the section says
    `unavailable` with :data:`REASON_ENTITY_RULE_SETS_PENDING` instead.
    """

    try:
        from core.entity_rule_derivation import FAMILY_ENTITY_DERIVATION  # noqa: PLC0415
    except ImportError:
        return None
    return FAMILY_ENTITY_DERIVATION


def _entity_rule_sets_section(
    conn, *, project_id: str, object_kinds: Sequence[str]
) -> dict[str, Any]:
    """The published derivation rule-set version of each declared type (68.6).

    One head per (Project, entity type) -- the head's name IS the object kind
    (the 68.6 draft door states it), so one `active_version` lookup per kind
    answers "which rule-set version derives this type's classifications". A
    type with no published version is NAMED, never omitted: "no rule set yet"
    is a fact the LLM needs before it quotes a classification.
    """

    family = _load_entity_derivation_family()
    if family is None:
        return {
            "status": "unavailable",
            "reason": {
                "code": REASON_ENTITY_RULE_SETS_PENDING,
                "message": (
                    "Entity derivation rule sets (Story 68.6) are not available "
                    "in this build. This is not a count of zero."
                ),
            },
        }

    from core.governance_rule_sets import active_version  # noqa: PLC0415

    published: list[dict[str, Any]] = []
    without: list[str] = []
    for kind in object_kinds:
        active = active_version(conn, project_id=project_id, family=family, name=kind)
        if active is None:
            without.append(kind)
            continue
        head, version = active
        payload = version.get("payload") or {}
        published.append(
            {
                "object_kind": kind,
                "rule_set_id": str(head["id"]),
                "version_id": str(version["id"]),
                "version_number": version.get("version_number"),
                "content_hash": version.get("content_hash"),
                "derived_attribute_count": len(payload.get("derived_attributes") or ()),
            }
        )
    return {
        "status": "ok",
        "published_count": len(published),
        "published": published,
        "no_published_rule_set": without,
    }


def _load_matching_coverage_reader():  # noqa: ANN202 -- the 68.3 contract, probed
    """The 68.3 contract: `entity_key_matching.matching_coverage(conn, project_id)`.

    Story 68.3 lands the occurrence-verdict coverage with the CoverageReport
    contract (`unavailable` never rendered as zero). Until it does, the
    section says `unavailable` with :data:`REASON_ENTITY_MATCHING_PENDING`.
    If 68.3 lands the reader under another name, this ONE import is the seam
    to adjust -- the mapping below already speaks the CoverageReport shape
    (`state`, `coverage.bound/eligible/by_state`, `unavailable_reason`).
    """

    try:
        from core.entity_key_matching import matching_coverage  # noqa: PLC0415
    except ImportError:
        return None
    return matching_coverage


def _matching_coverage_section(
    conn, *, project_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The matching coverage section, and the unmatched-occurrences group."""

    reader = _load_matching_coverage_reader()
    if reader is None:
        reason = {
            "code": REASON_ENTITY_MATCHING_PENDING,
            "message": (
                "Governed matching (Story 68.3) has not landed: occurrences of "
                "a bound key are not resolved to nodes yet, so no coverage is "
                "measurable. This is not a count of zero."
            ),
        }
        return (
            {"status": "unavailable", "reason": reason},
            {
                "group": UNATTACHED_UNMATCHED_OCCURRENCES,
                "status": "unavailable",
                "reason": reason,
            },
        )

    report = reader(conn, project_id=project_id)
    if hasattr(report, "as_dict"):
        report = report.as_dict()
    if str(report.get("state") or "") == "unavailable":
        reason = report.get("unavailable_reason") or {
            "code": "matching_coverage_unavailable",
            "message": "The matching coverage could not be read.",
        }
        return (
            {"status": "unavailable", "reason": reason},
            {
                "group": UNATTACHED_UNMATCHED_OCCURRENCES,
                "status": "unavailable",
                "reason": reason,
            },
        )
    coverage = report.get("coverage") or {}
    by_state = dict(coverage.get("by_state") or {})
    section = {
        "status": "ok",
        "resolved": coverage.get("bound"),
        "eligible": coverage.get("eligible"),
        "by_state": by_state,
    }
    unattached = {
        "group": UNATTACHED_UNMATCHED_OCCURRENCES,
        "status": "ok",
        "count": by_state.get("unmatched"),
    }
    return section, unattached


def _crossable_attributes_section(
    conn, *, project_id: str, object_kinds: Sequence[str]
) -> dict[str, Any]:
    """WHICH attributes an agent may cross by, per declared type (Story 69.4).

    THE POINT OF THE WHOLE SURFACE, AND THE ONE SECTION THAT MAKES AN AGENT
    HONEST BY CONSTRUCTION. Without it a model can only find out that a cross
    is impossible by attempting it and reading a refusal -- which it will
    instead present as "no data". This says, before the question is asked,
    exactly which words this Project can be analysed BY.

    TWO ORIGINS, NAMED SEPARATELY, because they are repaired differently. A
    CARRIED attribute is a column of the user's own reference file: if it is
    missing, the repair is to import it. A DERIVED classification comes from a
    rule the user published (68.6): if it is missing, the repair is to publish
    the rule -- and the version that produced it is part of the answer, because
    republishing changes answers without touching a single fact.

    A type with no crossable attribute at all is NAMED, never omitted: "this
    entity carries nothing to analyse by" is precisely what a model must know
    before promising an analysis.
    """

    if not object_kinds:
        return {"status": "ok", "count": 0, "by_object_kind": [], "without_attribute": []}

    by_kind: dict[str, dict[str, Any]] = {
        kind: {"object_kind": kind, "carried": [], "derived": []} for kind in object_kinds
    }
    with conn.cursor() as cur:
        # The attribute NAMES a node version carries, per type. Names only --
        # the model channel is bounded and values are rows, not vocabulary.
        cur.execute(
            """
            SELECT r.object_kind, a.key AS attribute, COUNT(DISTINCT v.node_id) AS nodes
              FROM app.master_data_object_versions v
              JOIN app.master_data_registries r
                ON r.id = v.registry_id
              CROSS JOIN LATERAL jsonb_each(
                  COALESCE(v.payload -> 'attributes', '{}'::jsonb)
              ) AS a(key, value)
             WHERE r.project_id = %s AND r.object_kind = ANY(%s)
               AND v.status = 'current' AND v.node_id IS NOT NULL
             GROUP BY r.object_kind, a.key
             ORDER BY r.object_kind, a.key
            """,
            (project_id, list(object_kinds)),
        )
        for object_kind, attribute, nodes in cur.fetchall():
            by_kind[str(object_kind)]["carried"].append(
                {"attribute": str(attribute), "node_count": int(nodes)}
            )
        cur.execute(
            """
            SELECT r.object_kind, d.attribute, d.rule_set_version_id,
                   COUNT(DISTINCT d.node_id) AS nodes
              FROM app.master_data_derived_attributes d
              JOIN app.master_data_registries r
                ON r.id = d.registry_id
              JOIN app.governance_rule_sets g
                ON g.id = d.rule_set_id
             WHERE r.project_id = %s AND r.object_kind = ANY(%s)
               AND g.current_version_id = d.rule_set_version_id
             GROUP BY r.object_kind, d.attribute, d.rule_set_version_id
             ORDER BY r.object_kind, d.attribute
            """,
            (project_id, list(object_kinds)),
        )
        for object_kind, attribute, version_id, nodes in cur.fetchall():
            by_kind[str(object_kind)]["derived"].append(
                {
                    "attribute": str(attribute),
                    "rule_set_version_id": str(version_id),
                    "node_count": int(nodes),
                }
            )

    rows = [by_kind[kind] for kind in sorted(by_kind)]
    without = [row["object_kind"] for row in rows if not row["carried"] and not row["derived"]]
    return {
        "status": "ok",
        "count": sum(len(row["carried"]) + len(row["derived"]) for row in rows),
        "by_object_kind": rows,
        "without_attribute": without,
    }


def _import_freshness_section(conn, *, project_id: str) -> dict[str, Any]:
    """WHEN each file feed last landed something (Story 69.4).

    A cross can be perfectly available and still answer about a stale month.
    The ledger already records the moment a snapshot was observed; nothing read
    it back for an agent. `last_published_at` is the last import that actually
    PUBLISHED -- an opened-then-failed run is not freshness, and counting it
    would let a broken feed look current.

    A Datastream that never published is NAMED with a NULL date, not omitted:
    "never" and "not listed" are different facts and only one of them names a
    gesture.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.datastream_id,
                   MAX(l.snapshot_observed_at) FILTER (WHERE l.outcome = 'published')
                       AS last_snapshot_at,
                   MAX(l.created_at) FILTER (WHERE l.outcome = 'published')
                       AS last_published_at,
                   COUNT(*) FILTER (WHERE l.outcome = 'published') AS published_count
              FROM app.managed_feed_import_ledger l
              JOIN app.datastreams d
                ON d.id = l.datastream_id AND d.project_id = l.project_id
             WHERE l.project_id = %s AND d.archived_at IS NULL
             GROUP BY l.datastream_id
             ORDER BY l.datastream_id
            """,
            (project_id,),
        )
        rows = [
            {
                "datastream_id": str(row[0]),
                "last_snapshot_at": row[1].isoformat() if row[1] else None,
                "last_published_at": row[2].isoformat() if row[2] else None,
                "published_import_count": int(row[3]),
            }
            for row in cur.fetchall()
        ]
    return {
        "status": "ok",
        "count": len(rows),
        "by_datastream": rows,
        "never_published": [row["datastream_id"] for row in rows if not row["last_published_at"]],
    }


def describe_entity_reconciliation_context(
    conn, *, project_id: str
) -> dict[str, Any]:
    """What an LLM may cross in this Project, and what is not attached (68.7).

    THE ONE READ BOTH DOORS SERVE. The MCP tool and the console REST route
    call this function and shape nothing: the AD-1 envelope is built HERE so
    the two doors cannot drift (AC3). The payload is small BY DESIGN -- ids,
    counts and version pins, never rows (the model-channel budget) -- and
    every unattached group is NAMED with its count, because a group that is
    omitted reads as a group that is empty (AC2).

    Four sections, each `ok` with its counters or `unavailable` with a named
    reason (never a fake zero):

    * ``entity_types`` -- what the Project declares (Story 68.1), with the
      feeding state of each type;
    * ``bindings`` -- which mapping columns designate which type (Story
      68.2's `designates_object_kind`, read from the PINNED mapping version);
    * ``rule_sets`` -- the published derivation rule-set version per type
      (Story 68.6);
    * ``matching_coverage`` -- resolved-of-eligible per the CoverageReport
      contract (Story 68.3, pending: reported `unavailable`);
    * ``crossable_attributes`` -- which attributes each type may be analysed
      BY, carried and rule-derived, with the rule version (Story 69.4). THE
      section that makes an agent honest by construction: without it, a model
      discovers that a cross is impossible only by attempting it, and reports
      that as "no data";
    * ``import_freshness`` -- when each file feed last PUBLISHED (Story 69.4).
      A cross can be available and still answer about a stale month.
    """

    from core.main import _envelope  # noqa: PLC0415 -- the AD-1 builder, one address

    project_id = _required(project_id, "project_id")
    types = list_entity_types(conn, project_id=project_id)
    object_kinds = [str(entry["object_kind"]) for entry in types]

    bindings, unattached_candidates = _entity_bindings_section(
        _current_mapping_version_payloads(conn, project_id=project_id)
    )
    rule_sets = _entity_rule_sets_section(conn, project_id=project_id, object_kinds=object_kinds)
    coverage, unattached_occurrences = _matching_coverage_section(conn, project_id=project_id)
    # Story 69.4: what an agent may cross BY, and how stale the answer would be.
    crossable = _crossable_attributes_section(
        conn, project_id=project_id, object_kinds=object_kinds
    )
    freshness = _import_freshness_section(conn, project_id=project_id)

    sections = {
        "bindings": bindings,
        "rule_sets": rule_sets,
        "matching_coverage": coverage,
        "crossable_attributes": crossable,
        "import_freshness": freshness,
    }
    unavailable = [name for name, section in sections.items() if section["status"] != "ok"]
    alerts = []
    if unavailable:
        alerts.append(
            {
                "severity": "info",
                "code": "entity_context_partial",
                "message": (
                    "Sections unavailable (named, never zeroed): " + ", ".join(unavailable)
                ),
            }
        )

    return _envelope(
        {
            "project_id": project_id,
            "entity_types": {
                "status": "ok",
                "count": len(types),
                "items": types,
                "empty_reason": (
                    None
                    if types
                    else {
                        "code": "no_entity_type_declared",
                        "message": (
                            "No entity type has been declared yet. An entity type "
                            "names a business object this Project reconciles."
                        ),
                    }
                ),
            },
            **sections,
            "unattached": [unattached_candidates, unattached_occurrences],
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "entity_reconciliation_context",
            "pull_id": None,
        },
        freshness="live",
        alerts=alerts,
    )


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _mapping_payload(
    conn, *, project_id: str, datastream_id: str, mapping_version_id: str
) -> dict[str, Any]:
    """The pinned mapping version's payload, scoped by all three ids.

    All three, not just the version id: the composite foreign key already makes a
    cross-Datastream pin unwritable, and this read states the same scope so a
    caller cannot learn the grain of a mapping it is not entitled to.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mapping_payload
              FROM app.datastream_mapping_versions
             WHERE id = %s AND datastream_id = %s AND project_id = %s
            """,
            (mapping_version_id, datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise MasterDataNotFound(
            "no such mapping version for this datastream and project"
        )
    payload = row[0]
    if isinstance(payload, str):  # pragma: no cover - driver-dependent
        import json

        payload = json.loads(payload)
    return payload or {}


def object_kind_for_datastream(
    conn, *, project_id: str, datastream_id: str
) -> str | None:
    """The object kind a Datastream feeds, or None (Story 64.15).

    THE SEAM BETWEEN "I DECLARE MY FIELDS" AND "I DECLARE MY OBJECT". A field
    declared while mapping a descriptive stream should qualify the object that
    stream describes -- the duration OF THE VIDEO, per the arbitration of
    2026-08-08. The link already existed: `master_data_source_bindings` names the
    registry a Datastream feeds, and the registry names the kind.

    IT IS DERIVED, NEVER CLAIMED. The declaration route resolves this instead of
    trusting an `object_kind` in the request body: a client that could name any
    kind could attach its column to another object's definition, and the two would
    then answer the same question differently.

    Several live bindings are legal (one per namespace, Story 64.13) but they all
    feed ONE registry, so the kind is unambiguous. `LIMIT 1` here is not a choice
    between candidates: the partial unique index makes them the same registry.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.object_kind
              FROM app.master_data_source_bindings b
              JOIN app.master_data_registries r
                ON r.id = b.registry_id AND r.project_id = b.project_id
             WHERE b.project_id = %s AND b.datastream_id = %s AND b.released_at IS NULL
             LIMIT 1
            """,
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None
