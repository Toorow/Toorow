"""Story 50.4 -- the visual-family registry: what a family NEEDS, stated as data.

WHY A REGISTRY AND NOT A SWITCH. Compatibility has to be answerable before a
renderer exists, by a server that never draws anything. A family is therefore a
record of constraints -- which wells it has, which semantic roles each well
accepts, how many distinct values it survives, how many marks it survives, and
whether it needs a time grain or a comparison the Query Spec must already carry.
Everything the validator does is a read of these records; nothing is a literal
buried in a branch.

WHICH FAMILIES ARE IN v1, AND WHICH ARE DELIBERATELY ABSENT.
`docs/product-architecture/visualization-and-rendering.md:160-169` names eleven
ordinary families and six specialized ones. This release declares eight:

    table  kpi  line  area  bar  stacked_bar  scatter  ai_path

The absent ones are recorded here rather than silently dropped, because an
absence leaves almost no trace and the next reader cannot tell "not yet" from
"not wanted" (CLAUDE.md anti-drift rule 3):

  * ordinary, NOT in v1 (`:162-164`): distribution, heatmap, calendar heatmap,
    gauge/progress, waterfall, small multiples. Each needs a Result shape the
    platform cannot produce yet -- a binned distribution, a two-dimensional
    cell grid, a date spine, a target value, a running signed delta, or a facet
    role. Declaring them now would advertise a family whose compatibility
    predicate cannot be written, which is worse than not offering it.
  * specialized, NOT in v1 (`:166-169`): timeline, Sankey, geographic map,
    knowledge graph, mindmap. Each needs a runtime capability that Story 50.5
    owns and a governed shape (a place hierarchy, a flow, an edge list) that no
    Semantic View publishes today.

WHY `ai_path` IS DECLARED AND WHY IT STILL CANNOT BE PICKED FOR A RESULT
(Story 55.1). `:167` puts the AI Path in this registry, "under the same registry
and evidence rules" as the knowledge graph and the mindmap. Its drawing now
exists, once, in the shared rendering runtime
(`ui/cards/shell/src/viz/renderers/aiPath.tsx`), so the deferral reason that
used to sit here -- "the AI Path lens is Story 50.2's, not a family yet" -- is
no longer true and keeping it would be an absence claimed about something
present.

What the declaration deliberately does NOT do is make the family selectable for
an ordinary Result. Its structuring well is the RUNG a step sits on (JOB /
SKILL / PROCEDURE / CONTEXT / TOOL, `core.ai_path_recorder.LEVELS`), which is a
`classification` of the step -- and `classification` has no server source, so
`check_shape_compatibility` refuses the family with `missing_binding` (nothing
bound) or `role_unavailable` (something bound), with the reason and the owning
surface attached. That is the SAME mechanism the Time well already uses, and it
is why this family is declared here yet is absent from the database CHECK on
`app.visualization_spec_versions.family`: no Visualization Spec version can
carry it, so listing it in the CHECK would widen a direct-SQL door the
validator keeps shut. The family's data is the AI Path record itself
(`core.ai_paths`), which the runtime receives from the screen that holds it --
never from a Query Spec projection.

WHAT THE REGISTRY REFUSES TO CONTAIN. No renderer option, no library name, no
colour, no pixel. A family says what data it can honestly present; how it is
drawn is Story 50.5's renderer registry and is not decided here.

THE TWO ROLES THAT HAVE NO SOURCE. `time` and `classification` are declared in
the well vocabulary -- the vocabulary does not shrink because a source is late --
but they are marked unavailable, with the reason and the owning surface named.
The compiled artifact's `queryability_matrix` is built as `metrics[]` /
`dimensions[]` / `cells[]` (`server/core/semantic_compiler.py:431-450`) and the
Result schema carries `name` alone (`server/core/query_execution.py:510`), so
there is nowhere to read a time or classification role from. Deriving one from a
member's name would manufacture a semantic authority this surface is forbidden
to have.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The inline row bound the execution service actually enforces
#: (`server/core/query_execution.py:57`). No family may claim to survive more
#: marks than the platform can return, or a "compatible" verdict would be a
#: promise about rows that never arrive.
MAX_INLINE_ROWS = 1_000

# ---------------------------------------------------------------------------
# Semantic roles.
# ---------------------------------------------------------------------------

ROLE_MEASURE = "measure"
ROLE_DIMENSION = "dimension"
ROLE_TIME = "time"
ROLE_CLASSIFICATION = "classification"

#: Every semantic role the grammar knows. Closed.
SEMANTIC_ROLES: tuple[str, ...] = (ROLE_MEASURE, ROLE_DIMENSION, ROLE_TIME, ROLE_CLASSIFICATION)

#: The roles a server source can supply TODAY, read off the compiled artifact's
#: `metrics[]` / `dimensions[]` split.
AVAILABLE_ROLES: frozenset[str] = frozenset({ROLE_MEASURE, ROLE_DIMENSION})

#: The roles with no server source. Kept in the vocabulary, marked unavailable.
UNAVAILABLE_ROLES: frozenset[str] = frozenset({ROLE_TIME, ROLE_CLASSIFICATION})

#: The one sentence every unavailable group, well and refusal repeats, so the
#: reader meets the same explanation everywhere instead of three paraphrases.
ROLE_UNAVAILABLE_REASON = (
    "Time and classification roles are not yet carried by the compiled Semantic View, "
    "so this group cannot be populated. The semantic compiler owns that field."
)

#: Named so a reader can act on it rather than file a bug against this screen.
ROLE_UNAVAILABLE_OWNER = "Semantic compiler (Epics 47 / 49)"


def role_availability() -> dict[str, dict[str, object]]:
    """Per-role availability, with the reason attached to every unavailable one."""
    return {
        role: {
            "role": role,
            "available": role in AVAILABLE_ROLES,
            "reason": None if role in AVAILABLE_ROLES else ROLE_UNAVAILABLE_REASON,
            "owner": None if role in AVAILABLE_ROLES else ROLE_UNAVAILABLE_OWNER,
        }
        for role in SEMANTIC_ROLES
    }


# ---------------------------------------------------------------------------
# Wells.
# ---------------------------------------------------------------------------

#: The ten binding wells, named by semantic role exactly as
#: `visualization-and-rendering.md:98-99` names them. This tuple IS the labelling
#: contract the Builder renders; no renderer-library word may enter it.
WELL_ROLES: tuple[str, ...] = (
    "measure",
    "dimension",
    "time",
    "series",
    "breakdown",
    "facet",
    "color",
    "size",
    "label",
    "detail",
)

#: Human labels, capitalized once here so the Builder cannot invent a synonym.
WELL_LABELS: dict[str, str] = {
    "measure": "Measure",
    "dimension": "Dimension",
    "time": "Time",
    "series": "Series",
    "breakdown": "Breakdown",
    "facet": "Facet",
    "color": "Color",
    "size": "Size",
    "label": "Label",
    "detail": "Detail",
}


@dataclass(frozen=True)
class Well:
    """One typed binding slot on one family."""

    #: The well name, one of WELL_ROLES. It is also the key inside `bindings`.
    name: str
    #: The semantic roles this well accepts. A member whose role is not in here
    #: is refused with `role_mismatch`; it is never coerced.
    accepts: frozenset[str]
    required: bool = False
    #: How many members may be bound here at once.
    max_members: int = 1
    #: Maximum distinct values this well survives, when it carries a dimension.
    #: `None` means the well carries no cardinality risk (a measure well).
    max_cardinality: int | None = None

    @property
    def available(self) -> bool:
        """False when every accepted role has no server source."""
        return bool(self.accepts & AVAILABLE_ROLES)

    @property
    def unavailable_reason(self) -> str | None:
        return None if self.available else ROLE_UNAVAILABLE_REASON

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": WELL_LABELS[self.name],
            "accepts": sorted(self.accepts),
            "required": self.required,
            "max_members": self.max_members,
            "max_cardinality": self.max_cardinality,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "unavailable_owner": None if self.available else ROLE_UNAVAILABLE_OWNER,
        }


@dataclass(frozen=True)
class VisualFamily:
    """One declared family and every constraint the validator reads off it."""

    id: str
    label: str
    #: One English sentence a person can choose from.
    description: str
    wells: tuple[Well, ...]
    #: Refused with `missing_grain` unless the pinned Query Spec version carries
    #: a grain. The grain lives in the QUERY spec, never in the presentation.
    requires_time_grain: bool
    #: Refused with `missing_comparison` unless the pinned Query Spec version
    #: carries a comparison other than `none`.
    requires_comparison: bool
    #: Maximum marks/points/cells this family survives, evaluated against ONE
    #: exact Result at validate time and returned as a disclosure (decision D4).
    max_marks: int
    #: What a renderer must be able to do. Declared here, honoured by Story 50.5.
    capabilities: tuple[str, ...]
    #: The wells whose bindings the accessible table fallback's columns derive
    #: from. AC8: every family declares this, always, and it is never empty --
    #: a family that cannot name its fallback columns is a registry defect.
    table_fallback_wells: tuple[str, ...]
    #: Specialized Result shape this family requires. Ordinary families bind
    #: Semantic View members and keep this absent.
    result_shape: str | None = None
    #: Exact stable Result field ids by well. They are Result bindings, not new
    #: members of the closed Semantic View role vocabulary.
    result_field_bindings: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def well(self, name: str) -> Well | None:
        for w in self.wells:
            if w.name == name:
                return w
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "wells": [w.as_dict() for w in self.wells],
            "requires_time_grain": self.requires_time_grain,
            "requires_comparison": self.requires_comparison,
            "max_marks": self.max_marks,
            "capabilities": list(self.capabilities),
            "table_fallback_wells": list(self.table_fallback_wells),
            # AC8 restated as data the client can assert on: the obligation is
            # not a value anyone can turn off.
            "result_shape": self.result_shape,
            "result_field_bindings": {
                well: list(fields) for well, fields in self.result_field_bindings
            },
            "table_fallback": "required",
        }


def _measure(required: bool = False, max_members: int = 1) -> Well:
    return Well("measure", frozenset({ROLE_MEASURE}), required, max_members, None)


def _dimension(required: bool, max_members: int, cardinality: int) -> Well:
    return Well("dimension", frozenset({ROLE_DIMENSION}), required, max_members, cardinality)


def _time_well() -> Well:
    """The Time well, declared and unavailable.

    It accepts ONLY the `time` role, which has no server source, so it is
    rendered disabled with its reason. An enabled Time well that silently
    accepted any member -- because the validator cannot tell a time dimension
    from any other -- is the invented role AC4 clause 2 forbids.
    """
    return Well("time", frozenset({ROLE_TIME}), False, 1, None)


_TABLE = VisualFamily(
    id="table",
    label="Table",
    description="Every returned row and column, exactly as the server ranked them.",
    wells=(
        _dimension(True, 8, MAX_INLINE_ROWS),
        _measure(True, 12),
        _time_well(),
        Well("label", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), False, 4, MAX_INLINE_ROWS),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=MAX_INLINE_ROWS,
    capabilities=("row_scroll", "column_header", "evidence_per_cell"),
    table_fallback_wells=("dimension", "measure"),
)

_KPI = VisualFamily(
    id="kpi",
    label="KPI",
    description="One governed measure as a single figure, with its disclosures.",
    wells=(
        _measure(True, 1),
        _time_well(),
        Well("detail", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), False, 2, 1),
        Well("label", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), False, 1, 1),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=1,
    capabilities=("single_value", "evidence_per_value"),
    table_fallback_wells=("measure",),
)

_LINE = VisualFamily(
    id="line",
    label="Line",
    description="A measure across an ordered dimension, at the grain the query pinned.",
    wells=(
        _measure(True, 5),
        _dimension(True, 1, 400),
        _time_well(),
        Well("series", frozenset({ROLE_DIMENSION}), False, 1, 12),
        Well("color", frozenset({ROLE_DIMENSION}), False, 1, 12),
        Well("detail", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), False, 2, 400),
    ),
    requires_time_grain=True,
    requires_comparison=False,
    max_marks=MAX_INLINE_ROWS,
    capabilities=("continuous_axis", "series_legend", "evidence_per_point"),
    table_fallback_wells=("dimension", "series", "measure"),
)

_AREA = VisualFamily(
    id="area",
    label="Area",
    description="A line family with a filled band; the same grain requirement applies.",
    wells=(
        _measure(True, 5),
        _dimension(True, 1, 400),
        _time_well(),
        Well("series", frozenset({ROLE_DIMENSION}), False, 1, 8),
        Well("color", frozenset({ROLE_DIMENSION}), False, 1, 8),
    ),
    requires_time_grain=True,
    requires_comparison=False,
    max_marks=MAX_INLINE_ROWS,
    capabilities=("continuous_axis", "stacking", "series_legend", "evidence_per_point"),
    table_fallback_wells=("dimension", "series", "measure"),
)

_BAR = VisualFamily(
    id="bar",
    label="Bar",
    description="A measure compared across the members of one dimension.",
    wells=(
        _measure(True, 3),
        _dimension(True, 1, 50),
        _time_well(),
        Well("series", frozenset({ROLE_DIMENSION}), False, 1, 8),
        Well("color", frozenset({ROLE_DIMENSION}), False, 1, 8),
        Well("label", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), False, 2, 50),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=400,
    capabilities=("categorical_axis", "series_legend", "evidence_per_bar"),
    table_fallback_wells=("dimension", "series", "measure"),
)

_STACKED_BAR = VisualFamily(
    id="stacked_bar",
    label="Stacked bar",
    description="A measure split by a second dimension within each category.",
    wells=(
        _measure(True, 1),
        _dimension(True, 1, 50),
        _time_well(),
        # The split IS the family. Without it a stacked bar is a bar wearing a
        # different name, and the fallback columns would be wrong.
        Well("breakdown", frozenset({ROLE_DIMENSION}), True, 1, 12),
        Well("color", frozenset({ROLE_DIMENSION}), False, 1, 12),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=600,
    capabilities=("categorical_axis", "stacking", "series_legend", "evidence_per_segment"),
    table_fallback_wells=("dimension", "breakdown", "measure"),
)

_SCATTER = VisualFamily(
    id="scatter",
    label="Scatter",
    description="Two measures against each other, one point per dimension member.",
    wells=(
        # Exactly two, and both required: a scatter with one measure has no
        # second axis to be a scatter on.
        Well("measure", frozenset({ROLE_MEASURE}), True, 2, None),
        _dimension(True, 1, MAX_INLINE_ROWS),
        _time_well(),
        Well("size", frozenset({ROLE_MEASURE}), False, 1, None),
        Well("color", frozenset({ROLE_DIMENSION}), False, 1, 12),
        Well("facet", frozenset({ROLE_DIMENSION}), False, 1, 8),
        Well("detail", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), False, 2, MAX_INLINE_ROWS),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=MAX_INLINE_ROWS,
    capabilities=("dual_measure_axis", "point_size", "evidence_per_point"),
    table_fallback_wells=("dimension", "measure"),
)

WATERFALL_RESULT_BINDINGS: dict[str, tuple[str, ...]] = {
    "dimension": (
        "wf_sequence",
        "wf_waterfall_role",
        "wf_component",
        "wf_label",
        "wf_currency",
        "wf_tax_basis",
        "wf_is_complete",
    ),
    "measure": (
        "wf_value_micros",
        "wf_start_total_micros",
        "wf_running_total_micros",
        "wf_covered_row_count",
        "wf_total_row_count",
    ),
    "detail": (
        "wf_datum_key",
        "wf_gap_codes",
        "wf_evidence_key",
    ),
}

_WATERFALL = VisualFamily(
    id="waterfall",
    label="Waterfall",
    description=(
        "A server-authored signed bridge whose running totals and evidence "
        "come from one immutable waterfall_v1 Result."
    ),
    wells=(
        Well("dimension", frozenset({ROLE_DIMENSION}), True, 7, 8),
        Well("measure", frozenset({ROLE_MEASURE}), True, 5, None),
        Well("detail", frozenset({ROLE_DIMENSION, ROLE_MEASURE}), True, 3, 8),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=8,
    capabilities=(
        "server_authored_running_total",
        "signed_delta",
        "evidence_per_datum",
        "null_coverage_state",
    ),
    table_fallback_wells=("dimension", "measure", "detail"),
    result_shape="waterfall_v1",
    result_field_bindings=tuple(WATERFALL_RESULT_BINDINGS.items()),
)


#: The five rungs of the reading grid, restated here as a CARDINALITY only. The
#: grid itself has one definition site (`core.ai_path_recorder.LEVELS`); importing
#: the recorder from the registry would make a presentation module depend on a
#: middleware, so the count is asserted against it by test instead.
_AI_PATH_RUNG_COUNT = 5

#: `core.ai_paths.OUTCOMES` -- succeeded / failed / refused / unavailable. Same
#: rule as above: a cardinality, asserted against the owner by test.
_AI_PATH_OUTCOME_COUNT = 4

_AI_PATH = VisualFamily(
    id="ai_path",
    label="AI Path",
    description="The walk an answer took through the knowledge tree, rung by rung.",
    wells=(
        # THE RUNG, and it is the whole reason this family cannot be picked for a
        # Query Result. A step's level (JOB / SKILL / PROCEDURE / CONTEXT / TOOL)
        # is a CLASSIFICATION of the step, derived by `core.ai_path_recorder.
        # level_of` from what was observed. `classification` has no server source,
        # so this required well is unavailable: the family stays visible with its
        # reason and its owner attached, and every attempt to validate it against
        # a Result is refused by name. An available well here would let the
        # Builder offer "AI Path" as a chart type for any two columns, which is
        # the invented role AC4 clause 2 forbids.
        Well("dimension", frozenset({ROLE_CLASSIFICATION}), True, 1, _AI_PATH_RUNG_COUNT),
        # The step's own name on its rung: the tool that was called.
        Well("label", frozenset({ROLE_DIMENSION, ROLE_CLASSIFICATION}), False, 2, MAX_INLINE_ROWS),
        # The governed object the step reached. A step that reached nothing keeps
        # this empty and stays drawn -- `core.ai_paths` refuses to invent one.
        Well("detail", frozenset({ROLE_DIMENSION}), False, 4, MAX_INLINE_ROWS),
        # The observed outcome, which is what a node is coloured by. Two node
        # states only -- see `AI_PATH_NODE_STATES` in the runtime.
        Well("color", frozenset({ROLE_CLASSIFICATION}), False, 1, _AI_PATH_OUTCOME_COUNT),
    ),
    requires_time_grain=False,
    requires_comparison=False,
    max_marks=MAX_INLINE_ROWS,
    # `branch_subtree` and `inspection_recorded` are Story 55.2: one node expands
    # into the branches the walk considered and did not take, and every expansion
    # is recorded in `app.evidence_inspections` (migration 175). Declared here
    # rather than only in the runtime because a capability nobody can read from
    # the server is a capability no evaluator can check.
    capabilities=(
        "level_ladder",
        "node_per_level",
        "evidence_per_node",
        "unreached_node",
        "branch_subtree",
        "inspection_recorded",
    ),
    table_fallback_wells=("dimension", "label", "detail", "color"),
)

#: Ordered, and the order is the order the Builder offers them in.
VISUAL_FAMILIES: tuple[VisualFamily, ...] = (
    _TABLE,
    _KPI,
    _LINE,
    _AREA,
    _BAR,
    _STACKED_BAR,
    _SCATTER,
    _WATERFALL,
    _AI_PATH,
)

#: The closed family enum. The database CHECK in migration 156 mirrors the SEVEN
#: families a Visualization Spec version may be persisted with; `ai_path` is
#: declared here and deliberately absent from that CHECK, because no spec version
#: can ever validate for it (see the module docstring). A direct SQL insert still
#: cannot invent a family the validator refuses.
FAMILY_IDS: tuple[str, ...] = tuple(f.id for f in VISUAL_FAMILIES)

#: The families a Visualization Spec version may carry, i.e. the ones whose wells
#: a pinned Query Spec can actually fill. This is the tuple migration 156's CHECK
#: mirrors, and `test_ai_path_visual_family.py` asserts the two agree.
SPEC_SELECTABLE_FAMILY_IDS: tuple[str, ...] = tuple(
    f.id for f in VISUAL_FAMILIES if not any(w.required and not w.available for w in f.wells)
)

_BY_ID: dict[str, VisualFamily] = {f.id: f for f in VISUAL_FAMILIES}

#: Families named by the target that this release does NOT declare, with the
#: reason. Served to the client so the absence is visible in the product, not
#: only in this docstring.
DEFERRED_FAMILIES: tuple[dict[str, str], ...] = (
    {
        "id": "distribution",
        "reason": "needs a binned distribution the Query Spec cannot express yet",
    },
    {"id": "heatmap", "reason": "needs a two-dimensional cell grid the Result does not carry"},
    {"id": "calendar_heatmap", "reason": "needs a date spine, which depends on the time role"},
    {"id": "gauge", "reason": "needs a governed target value; no surface authors one"},
    {"id": "small_multiples", "reason": "needs a facet role across families, deferred with it"},
    {
        "id": "timeline",
        "reason": "specialized; needs the time role and Story 50.5's runtime capability",
    },
    {"id": "sankey", "reason": "specialized; needs a governed flow shape no Semantic View emits"},
    {"id": "geographic_map", "reason": "specialized; needs a governed place hierarchy"},
    {"id": "knowledge_graph", "reason": "specialized; needs an edge list, not a tabular Result"},
    {"id": "mindmap", "reason": "specialized; needs an edge list, not a tabular Result"},
    # `ai_path` used to sit here. Story 55.1 moved it into VISUAL_FAMILIES: its
    # drawing exists once, in the shared rendering runtime, so the deferral had
    # become an absence claimed about something present. It is still not
    # selectable for a Result -- see the module docstring.
)


def get_family(family_id: str) -> VisualFamily | None:
    return _BY_ID.get(family_id)


def registry_payload() -> dict[str, object]:
    """The whole registry, as the Builder receives it.

    The client renders this; it never keeps a second copy of a rule. A browser
    that carried its own family table would be a second compatibility authority
    and would drift the first time a constraint moved here.
    """
    return {
        "families": [f.as_dict() for f in VISUAL_FAMILIES],
        "wells": [
            {
                "name": name,
                "label": WELL_LABELS[name],
                # Union of what any family accepts here, so the client can say
                # what the well is FOR even on a family that omits it.
                "accepts": sorted(
                    {
                        r
                        for f in VISUAL_FAMILIES
                        for w in f.wells
                        if w.name == name
                        for r in w.accepts
                    }
                ),
            }
            for name in WELL_ROLES
        ],
        "roles": list(role_availability().values()),
        "deferred_families": [dict(d) for d in DEFERRED_FAMILIES],
        "max_inline_rows": MAX_INLINE_ROWS,
    }
