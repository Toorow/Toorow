"""AC15 -- a connector SEEDS a Chart Template through its manifest, and never owns it.

WHAT WAS MISSING, MEASURED. Story 72.4 landed the platform-seed projection
(`core.visualization_template_seeds` plus `scripts/register_visualization_template_seeds.py`)
and migration 333 reserved `seed_origin = 'connector_seed'` with its two
coordinates. On 2026-09-01:

    grep -rn "connector_seed" server/ scripts/ --include=*.py
    #   visualization_template_seeds.py -- two COMMENTS explaining the reserved pair
    #   register_visualization_template_seeds.py -- reads `seed_origin = 'platform_seed'`

So the STRUCTURE existed and the GESTURE did not: no code path could ever write a
`connector_seed` row. This module is that gesture.

NOT ONE EXECUTABLE BYTE COMES FROM A MANIFEST. The declaration is read as DATA
from `server/modules/<module>/chart_templates/*.json`; the module is never
imported, no expression is evaluated, and every document goes through
`validate_template_document` -- the same walker that refuses a `bindings` block, a
`member_id`, a `query_spec_version_id` or a `result_id`. A connector that ships a
document the grammar refuses gets that template NAMED as unusable, never a
silently repaired one.

    ls server/modules/*/chart_templates 2>/dev/null | wc -l   # 0 today

Zero connectors declare one today, and that is why the console's "no seed
available" empty state is an honest sentence rather than a placeholder: it states
a fact this module can measure.

THE CONNECTOR NEVER OWNS IT. The head is created in the PROJECT, scoped org and
project like every other head, and the first edit makes it project-owned --
`chart_template_store.append_chart_template_version` flips `seed_origin` and
migration 333's pair CHECK nulls both coordinates in the same statement. Re-running
this gesture never rewrites a version and never resurrects a seed a person has
taken over: a head that already exists is left exactly as it is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chart_template_store import insert_template_version
from core.visualization_specs import VisualizationSpecRefused
from core.visualization_templates import (
    CHART_TEMPLATE_NOUN,
    ValidatedChartTemplate,
    validate_template_document,
)

__all__ = [
    "CONNECTOR_SEED_ORIGIN",
    "DeclaredConnectorSeed",
    "UnusableConnectorSeed",
    "connector_seed_catalogue",
    "declared_chart_templates",
    "seed_connector_chart_templates",
    "seed_head_id",
    "seed_version_id",
]

#: Migration 333's third origin, and the only one that carries the two
#: coordinates. Spelled once so a caller cannot type a fourth.
CONNECTOR_SEED_ORIGIN = "connector_seed"

#: Who is recorded as the author of a seeded version. It is NOT the person who
#: pressed the button: nobody composed this document, a connector shipped it, and
#: recording a person would attribute a design decision to someone who did not
#: make it. The first EDIT is attributed to whoever makes it, by the ordinary
#: writer.
CONNECTOR_SEED_AUTHOR = "connector-seed"

#: The directory a connector declares its templates in, beside the `reports/`
#: directory `loader._discover_reports` already reads. Same shape, same rule: one
#: JSON document per file, the file name is the seed identity.
SEED_DIRNAME = "chart_templates"

MODULES_ROOT = Path(__file__).resolve().parent.parent / "modules"

#: A module name is a directory name, and it is checked as one. A caller that
#: could pass `../..` would make this a file reader instead of a catalogue reader.
_ALLOWED_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True)
class DeclaredConnectorSeed:
    """One Chart Template a connector ships, proven legal before it is offered."""

    module_name: str
    seed_template_id: str
    label: str
    validated: ValidatedChartTemplate

    @property
    def answers_question(self) -> str:
        return self.validated.answers_question

    @property
    def family(self) -> str:
        return self.validated.family


@dataclass(frozen=True)
class UnusableConnectorSeed:
    """One declaration this deployment cannot use, named rather than dropped."""

    module_name: str
    seed_template_id: str
    reason: str


def _is_module_name(name: str) -> bool:
    return bool(name) and set(name) <= _ALLOWED_NAME_CHARS


def seed_head_id(module_name: str, seed_template_id: str, project_id: str) -> str:
    """The head id of one connector seed in one Project. Derived, never random.

    Derived for the reason `visualization_template_seeds.seed_head_id` is: it is
    what makes the gesture idempotent without parsing a document, and what lets a
    second run see that the head already exists.
    """
    return f"vtpl_cseed_{module_name}__{seed_template_id}__{project_id}"


def seed_version_id(module_name: str, seed_template_id: str, project_id: str) -> str:
    """Version 1 of one connector seed. There is never a version 2 from a manifest."""
    return f"vtv_cseed_{module_name}__{seed_template_id}__{project_id}__v1"


def declared_chart_templates(
    module_name: str, *, root: Path | None = None
) -> tuple[list[DeclaredConnectorSeed], list[UnusableConnectorSeed]]:
    """What one connector DECLARES, and what it declares that cannot be used.

    Returns two lists and raises nothing: a connector with a broken template must
    not make the catalogue unreadable for the ones that are fine. Every rejection
    carries the reason a person can act on.
    """
    if not _is_module_name(module_name):
        return [], [
            UnusableConnectorSeed(module_name, "", "this is not a connector module name")
        ]
    directory = (root or MODULES_ROOT) / module_name / SEED_DIRNAME
    if not directory.is_dir():
        return [], []
    declared: list[DeclaredConnectorSeed] = []
    unusable: list[UnusableConnectorSeed] = []
    for path in sorted(directory.glob("*.json")):
        seed_template_id = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unusable.append(
                UnusableConnectorSeed(module_name, seed_template_id, f"unreadable file: {exc}")
            )
            continue
        if not isinstance(payload, dict):
            unusable.append(
                UnusableConnectorSeed(
                    module_name, seed_template_id, "the file is not a template document"
                )
            )
            continue
        label = payload.pop("label", None)
        try:
            validated = validate_template_document(payload)
        except VisualizationSpecRefused as exc:
            unusable.append(
                UnusableConnectorSeed(
                    module_name,
                    seed_template_id,
                    f"this {CHART_TEMPLATE_NOUN} is refused on {len(exc.refusals)} point(s): "
                    + "; ".join(r.message for r in exc.refusals),
                )
            )
            continue
        declared.append(
            DeclaredConnectorSeed(
                module_name=module_name,
                seed_template_id=seed_template_id,
                label=str(label or validated.answers_question or seed_template_id)[:200],
                validated=validated,
            )
        )
    return declared, unusable


def connector_seed_catalogue(
    module_names: list[str], *, root: Path | None = None
) -> tuple[list[DeclaredConnectorSeed], list[UnusableConnectorSeed]]:
    """The declarations of several connectors at once, in the order given."""
    declared: list[DeclaredConnectorSeed] = []
    unusable: list[UnusableConnectorSeed] = []
    for name in module_names:
        module_declared, module_unusable = declared_chart_templates(name, root=root)
        declared.extend(module_declared)
        unusable.extend(module_unusable)
    return declared, unusable


def seed_connector_chart_templates(
    conn,
    *,
    org_id: str,
    project_id: str,
    module_name: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Bring one connector's declared templates into one Project. Idempotent, additive.

    A head that already exists is LEFT ALONE -- including one a person has edited
    and thereby taken over. Nothing is updated and nothing is deleted, which is
    the same contract the platform-seed projection holds and for the same reason:
    a Report may already pin a version of it.
    """
    declared, unusable = declared_chart_templates(module_name, root=root)
    created: list[dict[str, Any]] = []
    already: list[str] = []
    with conn.cursor() as cur:
        for seed in declared:
            head_id = seed_head_id(module_name, seed.seed_template_id, project_id)
            cur.execute(
                "SELECT 1 FROM app.visualization_templates "
                "WHERE id = %s AND org_id = %s AND project_id = %s",
                (head_id, org_id, project_id),
            )
            if cur.fetchone() is not None:
                already.append(head_id)
                continue
            cur.execute(
                """
                INSERT INTO app.visualization_templates
                    (id, org_id, project_id, label, seed_origin, seed_module_name,
                     seed_template_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    head_id,
                    org_id,
                    project_id,
                    seed.label,
                    CONNECTOR_SEED_ORIGIN,
                    module_name,
                    seed.seed_template_id,
                    CONNECTOR_SEED_AUTHOR,
                ),
            )
            version_id = seed_version_id(module_name, seed.seed_template_id, project_id)
            insert_template_version(
                cur,
                version_id=version_id,
                template_id=head_id,
                org_id=org_id,
                project_id=project_id,
                version_number=1,
                validated=seed.validated,
                predecessor_version_id=None,
                #: A connector is not a person and is not a model. `person` is the
                #: value migration 333's CHECK admits for "not a model proposal",
                #: and the connector's authorship is carried by `seed_module_name`
                #: -- the column that exists to say it.
                proposed_by="person",
                actor=CONNECTOR_SEED_AUTHOR,
            )
            cur.execute(
                "UPDATE app.visualization_templates SET current_version_id = %s, "
                "updated_at = NOW() WHERE id = %s AND org_id = %s AND project_id = %s",
                (version_id, head_id, org_id, project_id),
            )
            created.append(
                {
                    "template_id": head_id,
                    "version_id": version_id,
                    "seed_template_id": seed.seed_template_id,
                    "label": seed.label,
                }
            )
    return {
        "module_name": module_name,
        "declared": len(declared),
        "created": created,
        "already_present": already,
        "unusable": [
            {"seed_template_id": entry.seed_template_id, "reason": entry.reason}
            for entry in unusable
        ],
    }
