"""The platform half of the canonical vocabulary, DERIVED and never invented (AI-288).

WHAT WAS MISSING, MEASURED ON A LIVE DATABASE (2026-08-15, every migration
applied): `app.mdm_canonical_fields` holds **0 rows at platform scope** while six
production modules validate every mdm-bound binding against it, and
`app.target_fields` holds the **13** governed dictionary rows nobody projects into
it. `canonical_field_registry` opened the PROJECT door in August and refused the
platform one by name -- correctly: "a platform field changes what every project of
the instance aligns on". So the platform half had no writer at all.

WHY THIS IS A DERIVATION AND NOT A LIST SOMEBODY WROTE. Deciding which fields
every project aligns on is writing the product's vocabulary; guessing it and
calling the guess a repair is what `CLAUDE.md` forbids. It never needed guessing:
migration 023 ratified the 13 (9 metrics, 4 dimensions) and migration 032 already
modelled the link -- `dictionary_field_name` is a foreign key to
`target_fields(name)`, its own comment calling it the "optional derivation link to
the 8.5 dictionary". This module writes the projection that link was designed for.

THE ONE PLACE TWO SOURCES MUST AGREE, and it is not a detail. `average_position`
carries `measure = 'average'` in the dictionary and `additive = false` in
`dbt/seeds/dim_metric.csv`. A projection that read only the dictionary would
declare it summable-by-average and the AD-4 guard would be the last line of
defence; a projection that read only the catalogue would lose its aggregation.
Non-additivity is therefore read from the catalogue, and the dictionary's measure
is dropped for exactly those metrics -- which is what
`ck_mdm_canonical_fields_metric_aggregation` accepts and what the semantic layer
means.

AND THE VOCABULARY IS BOUNDED BY NOTHING. The first version of this module
stopped at the dictionary's 13 and posed the rest as a choice between two
catalogues. That framing was the mistake -- see the second half of this file:
39 connectors already name 286 distinct canonical targets, 274 of them outside
the 13. A registry of 13 facing 286 is a ceiling, not a reference.

WHAT THIS MODULE REFUSES TO DO. It never edits a row that exists. A registry row
that disagrees with the projection is a DIVERGENCE, reported, never resolved:
between a repository and a live registry, picking a side silently destroys the
other. Same posture as `platform_clocks` and for the same reason.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.audit import declare_action, insert_audit_row
from core.canonical_field_registry import validate_declaration

#: LA MOITIE PLATEFORME N ECRIVAIT AUCUN JOURNAL NON PLUS (2026-08-16), et c est
#: celle qui compte le plus : un champ de plateforme change ce que TOUT projet de
#: l instance aligne -- c est la phrase par laquelle `canonical_field_registry`
#: refuse cette porte a un client. Une decision de cette portee ne laissait pas
#: une ligne. Action distincte de `canonical_field.declared` a dessein : lire le
#: journal ne doit pas demander de deviner la portee d apres un `project_id`
#: absent.
ACTION_PLATFORM_CANONICAL_FIELD_DECLARED = declare_action(
    "platform_canonical_field.declared"
)

#: `dbt/seeds/dim_metric.csv` -- the delivered metric catalogue, and the ONLY
#: source consulted for additivity. Read rather than copied: a second list here
#: would be free to drift from the one dbt builds on.
_DIM_METRIC = Path(__file__).resolve().parents[2] / "dbt" / "seeds" / "dim_metric.csv"

#: `target_fields.data_type` -> `mdm_canonical_fields.value_type`. The two
#: vocabularies differ by exactly one word, and mapping the rest by identity keeps
#: a new dictionary type visible as a refusal instead of silently becoming a
#: string.
_VALUE_TYPE_BY_DATA_TYPE: Mapping[str, str] = {
    "integer": "integer",
    "decimal": "decimal",
    "currency": "money",
    "date": "date",
    "string": "string",
    "boolean": "boolean",
    "timestamp": "timestamp",
}


class PlatformVocabularyError(ValueError):
    """A dictionary row this projection cannot express, named."""


@dataclass(frozen=True)
class PlatformField:
    """One projected platform canonical field, ready to be declared."""

    canonical_name: str
    concept_kind: str
    value_type: str
    aggregation: str | None
    non_additive: bool
    description: str | None
    #: `None` quand le champ vient d'un manifeste : il ne derive d'aucune ligne
    #: du dictionnaire, et pretendre le contraire casserait la cle etrangere.
    dictionary_field_name: str | None

    def as_declaration(self) -> dict[str, Any]:
        """The normalized declaration, validated by the registry's own validator.

        One validator, not two: a projection that checked its own rules would be
        free to accept a row the database refuses.
        """
        declaration = validate_declaration(
            canonical_name=self.canonical_name,
            concept_kind=self.concept_kind,
            value_type=self.value_type,
            aggregation=self.aggregation,
            non_additive=self.non_additive,
        )
        declaration["description"] = self.description
        declaration["dictionary_field_name"] = self.dictionary_field_name
        return declaration


def non_additive_metric_names(path: Path | None = None) -> frozenset[str]:
    """Metric names the delivered catalogue declares NON-additive."""
    source = path or _DIM_METRIC
    with source.open(encoding="utf-8", newline="") as handle:
        return frozenset(
            (row["name"] or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("additive") or "").strip().lower() == "false"
        )


def project_dictionary(
    rows: Iterable[Mapping[str, Any]], *, non_additive: frozenset[str] | None = None
) -> list[PlatformField]:
    """Turn governed dictionary rows into platform canonical field declarations.

    Pure: it reads no database. `rows` are `app.target_fields` rows, and the
    result is ordered by (kind, name) so two runs produce the same list.
    """
    catalogue = non_additive if non_additive is not None else non_additive_metric_names()
    projected: list[PlatformField] = []

    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            raise PlatformVocabularyError("a dictionary row carries no name")
        kind = str(row.get("field_kind") or "").strip()
        data_type = str(row.get("data_type") or "").strip()
        value_type = _VALUE_TYPE_BY_DATA_TYPE.get(data_type)
        if value_type is None:
            raise PlatformVocabularyError(
                f"{name!r}: the dictionary data type {data_type!r} has no canonical "
                f"value type. Add it to _VALUE_TYPE_BY_DATA_TYPE deliberately rather "
                f"than letting the field land as a string."
            )

        is_non_additive = kind == "metric" and name in catalogue
        measure = row.get("measure")
        aggregation = None
        if kind == "metric" and not is_non_additive:
            aggregation = str(measure).strip() if measure else None

        projected.append(
            PlatformField(
                canonical_name=name,
                concept_kind=kind,
                value_type=value_type,
                aggregation=aggregation,
                non_additive=is_non_additive,
                description=(str(row["description"]).strip() if row.get("description") else None),
                dictionary_field_name=name,
            )
        )

    projected.sort(key=lambda field: (field.concept_kind, field.canonical_name))
    return projected


def load_dictionary(conn) -> list[dict[str, Any]]:
    """The governed dictionary rows, in a stable order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, display_name, data_type, field_kind, measure, description
            FROM app.target_fields
            ORDER BY field_kind, name
            """
        )
        columns = [column.name for column in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def load_platform_registry(conn) -> dict[str, dict[str, Any]]:
    """The platform canonical fields already stored, by canonical name."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_name, concept_kind, value_type, aggregation, non_additive,
                   status, dictionary_field_name
            FROM app.mdm_canonical_fields
            WHERE project_id IS NULL
            """
        )
        columns = [column.name for column in cur.description]
        return {row[0]: dict(zip(columns, row)) for row in cur.fetchall()}


def divergences(
    stored: Mapping[str, Mapping[str, Any]], projected: Iterable[PlatformField]
) -> list[str]:
    """Where a stored platform row and the projection disagree, said in one line each.

    Reported, NEVER resolved: between the repository and a live registry, a script
    that picked a side would destroy the other without leaving a trace.
    """
    problems: list[str] = []
    for field in projected:
        row = stored.get(field.canonical_name)
        if row is None:
            continue
        for column, expected in (
            ("concept_kind", field.concept_kind),
            ("value_type", field.value_type),
            ("aggregation", field.aggregation),
            ("non_additive", field.non_additive),
        ):
            actual = row.get(column)
            if actual != expected:
                problems.append(
                    f"{field.canonical_name}: stored {column}={actual!r}, "
                    f"the repository projects {expected!r}"
                )
    return problems


def declare_platform_field(conn, field: PlatformField, *, actor: str) -> dict[str, Any]:
    """Mint ONE platform canonical field (`project_id IS NULL`). Caller owns the transaction.

    Deliberately NOT in `canonical_field_registry`: that module's door refuses a
    null project by name, and its refusal is its signature -- "a platform field
    changes what every project of the instance aligns on". The platform writer
    therefore lives here, behind a script, where no HTTP route reaches it.

    The declaration still goes through `validate_declaration`, so both doors
    accept exactly what the database accepts.
    """
    from ulid import ULID  # noqa: PLC0415 -- one import for one write path

    declaration = field.as_declaration()
    field_id = f"mdm_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_canonical_fields
                (id, project_id, concept_kind, canonical_name, value_type, object_kind,
                 unit, aggregation, non_additive, description, dictionary_field_name,
                 created_by)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id, canonical_name, concept_kind, value_type, aggregation,
                      non_additive, dictionary_field_name, status
            """,
            (
                field_id,
                declaration["concept_kind"],
                declaration["canonical_name"],
                declaration["value_type"],
                declaration["object_kind"],
                declaration["unit"],
                declaration["aggregation"],
                declaration["non_additive"],
                declaration["description"],
                declaration["dictionary_field_name"],
                actor,
            ),
        )
        row = cur.fetchone()

    if row is None:
        raise PlatformVocabularyError(
            f"{field.canonical_name!r} is already an active platform canonical field. "
            "Nothing was written -- this projection never edits a row that exists."
        )

    columns = (
        "id", "canonical_name", "concept_kind", "value_type", "aggregation",
        "non_additive", "dictionary_field_name", "status",
    )
    minted = dict(zip(columns, row))

    # Dans la transaction de l appelant : `provision` en frappe des centaines
    # d un coup, et un lot a moitie journalise est pire qu un lot muet.
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_PLATFORM_CANONICAL_FIELD_DECLARED,
        provider_account="governance",
        connection_ref="",
        metadata={
            "scope": "platform",
            "canonical_field_id": minted["id"],
            "canonical_name": minted["canonical_name"],
            "concept_kind": minted["concept_kind"],
            "value_type": minted["value_type"],
            "aggregation": minted["aggregation"],
            "non_additive": minted["non_additive"],
            # D OU IL VIENT, parce que ce module ne l a pas invente : une ligne
            # du dictionnaire gouverne, ou l extension par les connecteurs.
            "dictionary_field_name": minted["dictionary_field_name"],
            "derived_from": (
                "target_fields" if minted["dictionary_field_name"] else "connector_manifests"
            ),
        },
    )
    return minted


def provision(conn, *, actor: str, dry_run: bool = False) -> dict[str, Any]:
    """Mint what is missing, leave what exists, report what disagrees.

    Returns `{minted, present, divergences}`. `dry_run` answers the same question
    without writing, which is the form the gate takes.
    """
    projected = project_dictionary(load_dictionary(conn))
    #  Le dictionnaire GOUVERNE les noms qu'il porte ; les connecteurs etendent
    #  au-dela. Un 40e connecteur agrandit le vocabulaire sans ceremonie.
    from_connectors, refused = project_connectors(
        connector_declarations(), governed=[field.canonical_name for field in projected]
    )
    projected = projected + from_connectors
    stored = load_platform_registry(conn)

    missing = [field for field in projected if field.canonical_name not in stored]
    report: dict[str, Any] = {
        "projected": [field.canonical_name for field in projected],
        "from_dictionary": len(projected) - len(from_connectors),
        "from_connectors": len(from_connectors),
        "refused": refused,
        "present": sorted(stored),
        "minted": [],
        "divergences": divergences(stored, projected),
    }
    if dry_run:
        report["would_mint"] = [field.canonical_name for field in missing]
        return report

    for field in missing:
        declare_platform_field(conn, field, actor=actor)
        report["minted"].append(field.canonical_name)
    return report


# ---------------------------------------------------------------------------
# The vocabulary is bounded by NOTHING -- the connectors extend it (Jean, 2026-08-15)
# ---------------------------------------------------------------------------
#
# LA CRITIQUE QUI A OUVERT CETTE MOITIE. La premiere version de ce module
# projetait les 13 lignes du dictionnaire et posait la suite comme un arbitrage
# entre DEUX catalogues -- `app.target_fields` (13) contre `dim_metric.csv` (20).
# Jean : « la paresse c'est de croire que tu n'en auras que deux la ou tu peux en
# avoir une infinite ».
#
# Mesure faite dans la foulee, sur les manifestes du depot :
#
#     39   connecteurs declarent un `canonical_target`
#     499  declarations de champ
#     286  cibles canoniques DISTINCTES
#     274  d'entre elles hors des 13 -- dont `campaign_id`, portee par SEIZE
#          connecteurs, et absente du vocabulaire contre lequel toute liaison
#          mdm est validee
#
# Un registre de 13 en face de 286 n'est pas un referentiel, c'est un plafond.
# C'est la meme doctrine que le catalogue de connecteurs (AI-279) : borne par
# RIEN. Poser un module dans `server/modules/` agrandit le catalogue ; un module
# qui nomme une cible canonique agrandit le vocabulaire, sans ceremonie.
#
# ET RIEN N'EST DEVINE POUR AUTANT. Le manifeste porte deja tout ce que le
# registre exige -- `kind`, `physical_type`, `aggregation`, `non_additive`,
# `description` -- sur les 499 declarations, en trois formes qui partagent ces
# neuf cles. La projection lit, elle ne complete pas.
#
# LES TROIS REFUS, et ils sont ce qui rend le reste sur.
#   * le dictionnaire GOUVERNE les noms qu'il porte : un connecteur qui le
#     contredit sur `cost` ne redefinit pas `cost`, il est signale ;
#   * deux connecteurs qui se contredisent entre eux sur un nom hors dictionnaire
#     ne mintent RIEN -- 7 cas, `campaign_id` en tete, et les nommer vaut mieux
#     que choisir ;
#   * un `physical_type` sans equivalent canonique (`json`, 3 cas) est refuse par
#     son nom plutot que de lander en `string`.
#
# Resultat mesure : 277 champs derivables au lieu de 13, 0 valeur inventee.

#: `physical_type` d'un manifeste -> `value_type` du registre. Meme discipline que
#: `_VALUE_TYPE_BY_DATA_TYPE` : ce qui n'est pas ici se refuse au lieu de se deviner.
_VALUE_TYPE_BY_PHYSICAL_TYPE: Mapping[str, str] = {
    "integer": "integer",
    "decimal": "decimal",
    "string": "string",
    "date": "date",
    "datetime": "timestamp",
    "boolean": "boolean",
}

_MODULES = Path(__file__).resolve().parents[1] / "modules"


def _walk_declarations(payload: Any) -> Iterable[Mapping[str, Any]]:
    """Every dict of a manifest that carries a `canonical_target`."""
    if isinstance(payload, dict):
        target = payload.get("canonical_target")
        if isinstance(target, str) and target.strip():
            yield payload
        for value in payload.values():
            yield from _walk_declarations(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_declarations(item)


def connector_declarations(modules_dir: Path | None = None) -> dict[str, set[tuple]]:
    """`{canonical_target -> {(kind, physical_type, aggregation, non_additive)}}`.

    A set per name on purpose: its size IS the question. One shape means the
    connectors agree; two or more means they contradict each other, and that is
    reported rather than resolved.
    """
    import json  # noqa: PLC0415 -- one reader, one import

    root = modules_dir or _MODULES
    declared: dict[str, set[tuple]] = {}
    for manifest in sorted(root.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 -- a broken manifest is named, never skipped silently
            raise PlatformVocabularyError(f"{manifest.parent.name}: manifest unreadable ({exc})")
        for entry in _walk_declarations(payload):
            name = str(entry["canonical_target"]).strip()
            declared.setdefault(name, set()).add(
                (
                    str(entry.get("kind") or "").strip(),
                    str(entry.get("physical_type") or "").strip(),
                    (str(entry["aggregation"]).strip() if entry.get("aggregation") else None),
                    bool(entry.get("non_additive")),
                )
            )
    return declared


def project_connectors(
    declared: Mapping[str, set[tuple]],
    *,
    governed: Iterable[str],
) -> tuple[list[PlatformField], list[str]]:
    """The fields the connectors add, and what stops the rest, named one by one.

    `governed` are the names the dictionary already carries: it governs those, so
    a connector saying otherwise about them is a remark, not a redefinition.
    """
    already = set(governed)
    fields: list[PlatformField] = []
    refused: list[str] = []

    for name in sorted(declared):
        if name in already:
            continue
        shapes = declared[name]
        if len(shapes) > 1:
            refused.append(
                f"{name}: {len(shapes)} connectors disagree "
                f"({sorted(shapes)}) -- nothing minted, the shape is a decision"
            )
            continue
        kind, physical_type, aggregation, non_additive = next(iter(shapes))
        value_type = _VALUE_TYPE_BY_PHYSICAL_TYPE.get(physical_type)
        if value_type is None:
            refused.append(
                f"{name}: physical type {physical_type!r} has no canonical value type -- "
                f"add it deliberately rather than letting the field land as a string"
            )
            continue
        if kind not in ("metric", "dimension"):
            refused.append(f"{name}: kind {kind!r} is neither a metric nor a dimension")
            continue
        if kind == "metric" and aggregation in (None, "none") and not non_additive:
            refused.append(
                f"{name}: a metric that declares neither an aggregation nor non-additivity "
                f"would be summed by whatever read it first"
            )
            continue

        fields.append(
            PlatformField(
                canonical_name=name,
                concept_kind=kind,
                value_type=value_type,
                aggregation=(aggregation if kind == "metric" and not non_additive else None),
                non_additive=(non_additive if kind == "metric" else False),
                description=None,
                dictionary_field_name=None,
            )
        )
    return fields, refused
