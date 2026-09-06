"""Story 50.4 -- the Visualization Spec: one grammar, validated before it is stored.

WHAT THIS OWNS. Presentation intent over an already governed Result, and nothing
else. A Visualization Spec says which member goes in which well, which family
draws it, how the axes and legend read, which thresholds matter and which fields
carry evidence. It says nothing about what was asked -- no measure definition, no
filter, no grain, no comparison, no limit. Those belong to the Query Spec
(`server/core/query_specs.py`), and a presentation edit that touched one of them
would silently change the claimed answer.

WHY THE DENY-LIST IS CODE AND NOT A PARAGRAPH.
`docs/product-architecture/visualization-and-rendering.md:113-119` lists five
things a persisted contract may never contain. A comment saying so is honoured by
everyone who reads it and by nobody who does not -- including a model proposing a
spec. So the grammar below is a CLOSED allow-list: a key the grammar does not
declare is refused with its exact JSON pointer, at every depth, and it is never
stripped. Stripping makes a rejected instruction invisible, and the next reader
cannot tell a rejected spec from an accepted one.

The five clauses and where each is actually enforced:

  raw ECharts/D3 options       -> `_walk`: `series`, `xAxis`, `option`, `encode`,
                                  `dataset`, `selectAll` are not declared keys
  JavaScript functions         -> `_string_leaf`: no string leaf is ever
                                  interpreted, and every one must pass a leaf
                                  type plus the hostile-content scan
  HTML / CSS / URLs / handlers -> same scan: `<`, `>`, `{`, `}`, `$`, backtick and
                                  backslash cannot appear in ANY string leaf, and
                                  there is no url/href/style/class/on* key
  client aggregation / joins / -> there is no `aggregate`, `join`, `formula`,
  calculated measures / tz        `calculate`, `expression` or `timezone` key; a
                                  binding value is a member ID, never a computation
  connector field names        -> every binding value must resolve to a member of
                                  the PINNED Query Spec version; a connector column
                                  name simply is not in that set (`unknown_member`)
  credentials                  -> no free-text leaf exists to carry one, the scan
                                  refuses credential-shaped strings, and migration
                                  156 caps `pg_column_size(spec)`

TWO VERSION KEYS, TWO JOBS (decision D1). `spec_contract_version` is the literal
a database CHECK pins and the string that makes two hashes comparable -- it
mirrors `QUERY_SPEC_CONTRACT_VERSION` (`query_specs.py:43`). `schema_version` is
the integer Story 50.5's renderer registry range-compares. A range predicate over
a dotted string is a lexicographic comparison waiting to be wrong, and an integer
alone gives a database CHECK nothing distinctive to pin. Neither substitutes for
the other.

ONE HASH FUNCTION IN THIS EPIC. `canonical_hash` is imported from
`core.query_specs`, not reimplemented. Two hash functions in one epic is two
authorities, and they diverge the first time either is touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ulid import ULID

from core.query_specs import canonical_hash
from core.result_shapes import WATERFALL_FIELDS
from core.visualization_families import (
    AVAILABLE_ROLES,
    FAMILY_IDS,
    MAX_INLINE_ROWS,
    ROLE_DIMENSION,
    ROLE_MEASURE,
    ROLE_UNAVAILABLE_OWNER,
    ROLE_UNAVAILABLE_REASON,
    WELL_LABELS,
    WELL_ROLES,
    VisualFamily,
    get_family,
)

#: The literal a database CHECK pins. Travels INSIDE the hashed document, so a
#: stored `content_hash` is only comparable to one produced by the same contract.
VISUALIZATION_SPEC_CONTRACT_VERSION = "visualization-spec.v1"

#: The integer a runtime range-compares. See decision D1 in the story.
VISUALIZATION_SPEC_SCHEMA_VERSION = 1

#: THE single definition site of the responsive-profile enum (decision D6). It is
#: persisted grammar, so it is defined once, here; Story 50.5's runtime cites this
#: name rather than redeclaring the list. Two declarations of one enum are two
#: authorities and the second one drifts.
#:
#: `mcp-pip` WAS a named divergence and is one no longer, since 2026-08-24.
#: `visualization-and-rendering.md:346` gives the MCP layout as
#: "Inline/fullscreen/PiP profile when supported" -- "when supported" qualifies the
#: HOST's capability, never ours -- so picture-in-picture is a mode of the ratified
#: target, and an enum shipping two of the three promised a layout the product did
#: not have. The runtime now implements it (`ui/cards/shell/src/viz/responsive.ts`,
#: `profileLayout`), so the value enters the grammar beside the others.
#:
#: IT IS A WIDENING, WHICH IS WHY THERE IS NO `schema_version` BUMP. Every document
#: valid under `visualization-spec.v1` stays valid, byte for byte, and its
#: `content_hash` does not move; only documents that could not be written before can
#: be written now. A build that predates this value still meets a host asking for it
#: cleanly -- `resolveProfile` substitutes and states the substitution on screen --
#: so the older runtime degrades honestly rather than mis-drawing.
#:
#: The database CHECK that mirrors this tuple is
#: `ck_renderer_runtime_builds_profiles`, widened by migration 304;
#: `test_the_database_check_mirrors_this_enum` reads the SQL and asserts the two
#: are one list, so adding a sixth profile here without its migration is a red test
#: rather than a deploy-time refusal.
RESPONSIVE_PROFILES: tuple[str, ...] = (
    "console",
    "mcp-inline",
    "mcp-fullscreen",
    "mcp-pip",
    "share",
)

#: A whole document may not exceed this once canonicalized. Mirrored by a
#: `pg_column_size` CHECK in migration 156, so a direct SQL insert meets the same
#: ceiling as a request.
MAX_SPEC_BYTES = 32_768

MAX_LABEL_LENGTH = 120
MAX_MEMBER_ID_LENGTH = 200
MAX_THRESHOLDS = 10
MAX_REFERENCE_LINES = 10
MAX_ANNOTATIONS = 20


class VisualizationNotFound(LookupError):
    """Foreign, denied or absent -- one exception for all three.

    Which of the three it was belongs in audit, not in a response body. A caller
    that can tell "denied" from "absent" has a tenant-enumeration oracle.
    """


@dataclass(frozen=True)
class VisualizationRefusal:
    """One named, actionable reason, attached to the exact control it is about."""

    code: str
    message: str
    #: A JSON pointer into the proposed document, so a Builder can attach the
    #: refusal to the owning well instead of showing one page-level banner.
    subject: str | None = None
    #: One action the reader can take. `message` states the fact; this states the
    #: move. A refusal without a remedy makes the reader guess, and guessing is
    #: how a semantic substitution gets built in a browser.
    remedy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "subject": self.subject,
            "remedy": self.remedy,
        }


class VisualizationSpecRefused(ValueError):
    """Every reason at once, never just the first.

    Returning the first failure makes a caller fix one well, resubmit, and
    discover the next. Mirrors `QuerySpecRefused.as_dict()`
    (`server/core/query_specs.py:100-107`), extended with `remedy`.
    """

    def __init__(self, code: str, message: str, refusals: list[VisualizationRefusal] | None = None):
        super().__init__(message)
        self.code = code
        self.refusals = list(refusals or [])

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "refusals": [r.as_dict() for r in self.refusals],
        }


# ---------------------------------------------------------------------------
# The hostile-content scan, applied to EVERY string leaf.
#
# It runs before the leaf's own type check, so a value that would fail both is
# reported as the more specific problem it actually is.
# ---------------------------------------------------------------------------

#: Characters that cannot appear in any string leaf of this grammar. They are the
#: alphabet of HTML, CSS, template interpolation and escape sequences, and no
#: enum member, member id or governed label needs one.
_FORBIDDEN_CHARS = "<>{}$`\\"

#: Substrings that are code or a resource reference however they are spelled. The
#: JSON decoder has already turned `<script>` into `<script>` by the
#: time a value reaches here, so a unicode-escaped payload meets the same rule.
_CODE_MARKERS = (
    "javascript:",
    # Spelled with their media type rather than as a bare `data:`, so a governed
    # label like "Data: revenue" is not refused. A refusal a person cannot act on
    # is as bad as no refusal.
    "data:text",
    "data:image",
    "data:application",
    "data:;",
    "vbscript:",
    "http://",
    "https://",
    "//",
    "=>",
    "d3.",
    "echarts",
    "selectall",
    "appendchild",
    "<script",
)

#: Code markers that need a PATTERN rather than a substring, and the one deny-list
#: clause where this story's enforcement is deliberately narrower than AC3's table.
#:
#: AC3 row 2 reads "a leaf carrying `function`, `=>` or `javascript:` fails". Taken
#: literally, the BARE WORD would be a marker -- and it cannot be, because
#: `job_function` is a real governed member id (LinkedIn Ads targets on job
#: function) and "Job Function" is a real governed label. Refusing them would make
#: a legitimate binding unexpressible, and a refusal a person cannot act on is as
#: bad as no refusal. So the enforced rule is the CALL, with any whitespace between
#: the keyword and the parenthesis -- which closes the real gap the plain
#: `"function("` / `"function ("` substrings left open (`function\t(`, `function  (`)
#: without swallowing a governed noun. AC3's table is corrected to say so, and the
#: corpus pins both halves: `function\t(d){}` is refused, `Job Function` is accepted.
#:
#: Nothing is lost by the narrowing: a function BODY needs `{`, `}` or `=>`, and
#: `_FORBIDDEN_CHARS` already refuses those in every string leaf.
_CODE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("function(", re.compile(r"function\s*\(", re.IGNORECASE)),
    ("new Function", re.compile(r"\bnew\s+function\b", re.IGNORECASE)),
)

#: A colour literal is a renderer decision, not presentation intent. Colour in
#: this grammar is a ROLE and a semantic direction; a hex value would pin a hue
#: that the theme owns (`visualization-and-rendering.md:216`).
_COLOUR_LITERAL = re.compile(r"^#[0-9A-Fa-f]{3,8}$")

#: Credential shapes. Precise on purpose: a broad "contains the word token" rule
#: would refuse a legitimate governed label, and a refusal a person cannot act on
#: is as bad as no refusal.
_CREDENTIAL_SHAPES = (
    re.compile(r"^(?:bearer|basic)\s", re.IGNORECASE),
    re.compile(r"^ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"^(?:sk|pk|rk)_(?:live|test)_", re.IGNORECASE),
    re.compile(r"^(?:api[_-]?key|secret|password|authorization)\s*[:=]", re.IGNORECASE),
)

_MEMBER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
#: A label may carry ordinary punctuation and accents; it may not carry the
#: alphabet of markup. This is the deny-list holding at the PERSISTENCE boundary
#: rather than at render time (decision D10) -- escaping is a renderer promise,
#: and a share page or an MCP resource must not be the surface that forgets.
_LABEL = re.compile(r"^[^\x00-\x1f<>{}$`\\]{1,120}$")


def _scan_string(
    value: str,
    pointer: str,
    refusals: list[VisualizationRefusal],
    noun: str = "Visualization Spec",
) -> bool:
    """Return True when the string is clean. Append every reason when it is not.

    `noun` is the product word of the document being walked. The scan is shared
    by the Visualization Spec and the Chart Template (story 72.2) and each is
    refused in its own word: a person editing a Chart Template must never be
    told what a Visualization Spec may not contain.
    """
    ok = True
    if len(value) > MAX_MEMBER_ID_LENGTH:
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"this value is {len(value)} characters; "
                f"no value in a {noun} may exceed {MAX_MEMBER_ID_LENGTH}",
                pointer,
                "Shorten the value, or leave it out and let the governed definition supply it.",
            )
        )
        ok = False
    bad = sorted({c for c in value if c in _FORBIDDEN_CHARS})
    if bad:
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"the character(s) {' '.join(bad)} cannot appear in a {noun}: "
                "markup, style, template and escape syntax are not presentation intent",
                pointer,
                "Remove the markup. A label is plain text; a colour is a role, not a value.",
            )
        )
        ok = False
    lowered = value.lower()
    hit = [m for m in _CODE_MARKERS if m in lowered]
    hit += [name for name, pattern in _CODE_PATTERNS if pattern.search(value)]
    if hit:
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"`{hit[0]}` is code or a resource reference; a {noun} carries "
                "neither. Generated renderer code is not a visualization contract",
                pointer,
                "Express the intent as a family, a binding and a presentation property.",
            )
        )
        ok = False
    if _COLOUR_LITERAL.match(value):
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                "a colour literal is a renderer decision, not presentation intent",
                pointer,
                "Set `color.role` and `color.semantic_direction`; the theme owns the hue.",
            )
        )
        ok = False
    if any(shape.search(value) for shape in _CREDENTIAL_SHAPES):
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                "this value has the shape of authorization material, which a persisted "
                "presentation contract never carries",
                pointer,
                "Remove it. Authorization is resolved server-side, per request.",
            )
        )
        ok = False
    return ok


# ---------------------------------------------------------------------------
# The grammar, as data.
#
# One machine-readable definition. The AC2 key-set test reads THIS, so adding a
# key without updating the contract turns a test red rather than widening the
# surface silently.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Leaf:
    kind: str
    values: frozenset[str] = frozenset()
    literal: Any = None
    default: Any = None
    max_items: int | None = None
    #: True when the array's order carries no meaning, so it is sorted before
    #: hashing. Two documents that mean the same thing must hash the same.
    unordered: bool = False
    #: What ONE entry of an `enum_list` is called, in the words of the reader.
    #: The refusal of a malformed list says "a list of responsive profiles" or
    #: "a list of wells" because of this, and never a generic "a list" -- a
    #: refusal that does not name what was expected cannot be acted on.
    item_noun: str = "values"


@dataclass(frozen=True)
class Node:
    keys: dict[str, Any]
    #: `None` when the whole node may be absent (`top_n`).
    default_absent: Any = None
    nullable: bool = False


@dataclass(frozen=True)
class ArrayOf:
    item: Any
    max_items: int


@dataclass(frozen=True)
class MapOf:
    """A map whose KEYS are validated too -- `labels.override` is keyed by member id."""

    key_kind: str
    value: Any
    max_items: int
    #: The accepted keys when `key_kind` is `enum`. Empty for the open key kinds
    #: (`member_id`), which are validated by shape rather than by membership.
    key_values: frozenset[str] = frozenset()


_AXIS = Node(
    {
        "scale": Leaf(
            "enum", frozenset({"linear", "log", "categorical", "ordinal"}), default="linear"
        ),
        "zero_baseline": Leaf("bool", default=True),
        "tick_density": Leaf("enum", frozenset({"sparse", "normal", "dense"}), default="normal"),
    }
)

#: THE grammar. Every top-level key of AC2, and nothing else.
GRAMMAR: dict[str, Any] = {
    "spec_contract_version": Leaf("literal_str", literal=VISUALIZATION_SPEC_CONTRACT_VERSION),
    "schema_version": Leaf("literal_int", literal=VISUALIZATION_SPEC_SCHEMA_VERSION),
    "family": Leaf("enum", frozenset(FAMILY_IDS)),
    "bindings": Node({role: Leaf("member_id_list", default=[]) for role in WELL_ROLES}),
    # Server-ranked only. There is no client sort: a sort that changes the
    # claimed answer belongs to the Query Spec (`visualization-and-rendering.md:76`).
    "order": Node({"source": Leaf("literal_str", literal="result", default="result")}),
    "top_n": Node(
        {
            "n": Leaf("positive_int", default=None),
            # Not a boolean: the ONLY accepted value is `true`. A display-only
            # subset presented as the whole answer is the failure this clause
            # exists to prevent, so `false` is refused rather than honoured.
            "display_only": Leaf("true_only", default=True),
        },
        nullable=True,
    ),
    "axes": Node({"x": _AXIS, "y": _AXIS}),
    "legend": Node(
        {
            "position": Leaf(
                "enum", frozenset({"top", "right", "bottom", "left", "none"}), default="right"
            ),
            "visible": Leaf("bool", default=True),
        }
    ),
    "formatting": Node(
        {
            "number_style": Leaf(
                "enum",
                frozenset({"auto", "integer", "decimal", "percent", "currency", "compact"}),
                default="auto",
            ),
            "date_style": Leaf(
                "enum", frozenset({"auto", "iso", "short", "long", "month", "year"}), default="auto"
            ),
            # Units come from the governed member definition or from nowhere.
            # A typed unit here would be a second unit authority.
            "unit_source": Leaf(
                "enum", frozenset({"semantic_view", "none"}), default="semantic_view"
            ),
        }
    ),
    "color": Node(
        {
            "role": Leaf(
                "enum",
                frozenset({"none", "single", "categorical", "sequential", "diverging"}),
                default="none",
            ),
            "semantic_direction": Leaf(
                "enum",
                frozenset({"none", "higher_is_better", "lower_is_better"}),
                default="none",
            ),
        }
    ),
    "thresholds": ArrayOf(
        Node(
            {
                "member_id": Leaf("member_id"),
                "comparator": Leaf("enum", frozenset({"gt", "gte", "lt", "lte", "eq"})),
                "value": Leaf("number"),
                "severity": Leaf(
                    "enum", frozenset({"info", "success", "warning", "critical"}), default="info"
                ),
            }
        ),
        MAX_THRESHOLDS,
    ),
    "reference_lines": ArrayOf(
        Node(
            {
                "member_id": Leaf("member_id"),
                "kind": Leaf("enum", frozenset({"average", "median", "target", "zero"})),
                "value": Leaf("number", default=None),
            }
        ),
        MAX_REFERENCE_LINES,
    ),
    # Evidence-backed only: an annotation names an evidence id, never free text.
    # Free annotation text would be the arbitrary HTML the deny-list forbids,
    # arriving through the one door a reader would not think to check.
    "annotations": ArrayOf(
        Node(
            {
                "evidence_id": Leaf("evidence_id"),
                "anchor": Leaf("enum", frozenset({"datum", "mark", "axis", "plot"})),
            }
        ),
        MAX_ANNOTATIONS,
    ),
    "interactions": Node(
        {
            "hover": Leaf("bool", default=True),
            "select": Leaf("bool", default=True),
            "zoom": Leaf("bool", default=False),
            "legend_toggle": Leaf("bool", default=True),
            # Local only, and local means "hide or select rows already in the
            # Result" (`visualization-and-rendering.md:82-87`). It never
            # reaggregates and never invents completeness.
            "local_filter": Leaf("bool", default=False),
        }
    ),
    "evidence": Node(
        {
            "datum_fields": Leaf("member_id_list", default=[], unordered=True),
            "mark_binding": Leaf(
                "enum", frozenset({"none", "datum", "series", "category"}), default="datum"
            ),
        }
    ),
    "responsive": Node(
        {
            "profiles": Leaf(
                "enum_list",
                frozenset(RESPONSIVE_PROFILES),
                default=["console"],
                unordered=True,
                item_noun="responsive profiles",
            )
        }
    ),
    "accessibility": Node(
        {
            "summary_source": Leaf(
                "enum", frozenset({"result_manifest", "family_default"}), default="result_manifest"
            ),
            # AC8: the literal, and there is no value that turns it off.
            "table_fallback": Leaf("literal_str", literal="required", default="required"),
        }
    ),
    "labels": Node({"override": MapOf("member_id", Leaf("label"), 40)}),
}

#: The exact top-level key set. The AC2 test asserts this equals the story's list.
GRAMMAR_KEYS: tuple[str, ...] = tuple(GRAMMAR.keys())

#: Keys that exist, but belong to the Query Spec. Checked at EVERY depth before
#: the unknown-key verdict, so a caller trying to change the question through the
#: presentation door is told who owns it rather than told the key is unknown --
#: which would read as "spell it differently".
_QUERY_OWNED: dict[str, str] = {
    "measures": "Measure selection belongs to the Query Spec.",
    "dimensions": "Dimension selection belongs to the Query Spec.",
    "filters": "A business filter belongs to the Query Spec.",
    "filter": "A business filter belongs to the Query Spec.",
    "time_range": "The time range belongs to the Query Spec.",
    "time": "The time window belongs to the Query Spec.",
    "grain": "Grain belongs to the Query Spec.",
    "comparison": "Comparison belongs to the Query Spec.",
    "sort": "A sort that changes the claimed answer belongs to the Query Spec.",
    "limit": "An analytical limit belongs to the Query Spec.",
    "row_limit": "An analytical limit belongs to the Query Spec.",
    # Not "belongs to the Query Spec" any more: the Query Spec refuses it too
    # (`query_specs._refuse_unexecuted_time_controls`), and sending a person from
    # one door to another that also refuses is a loop, not an answer.
    "timezone": (
        "A time zone is applied nowhere on this path: governed grains are dates, and the "
        "reporting time zone is declared on the Datastream and signalled there."
    ),
    "semantic_view_id": "The Semantic View pin belongs to the Query Spec.",
    "semantic_view_version_id": "The Semantic View pin belongs to the Query Spec.",
    "aggregate": "Aggregation belongs to the Semantic View and the Query Spec.",
    "aggregation": "Aggregation belongs to the Semantic View and the Query Spec.",
    "join": "A join belongs to the Semantic View.",
    "formula": "A calculated measure belongs to the Semantic View.",
    "calculate": "A calculated measure belongs to the Semantic View.",
    "calculated_measure": "A calculated measure belongs to the Semantic View.",
    "expression": "An expression belongs to the Semantic View.",
}

_QUERY_OWNED_REMEDY = (
    "Changing it produces a new Result; open Explore to run it, then re-pin this "
    "Visualization to the new Query Spec version."
)


def _pointer(parent: str, key: str) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{token}"


#: What a walk carries besides the grammar: who the document is FOR, and which
#: foreign keys have a named owner in this walk. Passed down instead of read from
#: a module global, so one walker can serve two documents without either of them
#: learning the other's vocabulary.
@dataclass(frozen=True)
class WalkContext:
    #: The product noun of the document being walked, as a reader would say it.
    noun: str = "Visualization Spec"
    #: `{key: (code, message, remedy)}` -- keys this document does not declare but
    #: whose owner is known, so the refusal says who owns them rather than
    #: "unknown", which reads as "spell it differently".
    owned: Mapping[str, tuple[str, str, str]] = field(default_factory=dict)


_DEFAULT_CONTEXT = WalkContext()


def _unknown(
    pointer: str,
    key: str,
    refusals: list[VisualizationRefusal],
    context: WalkContext = _DEFAULT_CONTEXT,
) -> None:
    foreign = context.owned.get(str(key))
    if foreign is not None:
        refusals.append(VisualizationRefusal(foreign[0], foreign[1], pointer, foreign[2]))
        return
    owner = _QUERY_OWNED.get(str(key))
    if owner is not None:
        refusals.append(
            VisualizationRefusal("query_owned_field", owner, pointer, _QUERY_OWNED_REMEDY)
        )
        return
    refusals.append(
        VisualizationRefusal(
            "unknown_field",
            f"`{key}` is not part of the {context.noun} grammar",
            pointer,
            "Remove it. The grammar is closed, and an undeclared key is never stored.",
        )
    )


# ---------------------------------------------------------------------------
# The walker.
#
# It returns the NORMALIZED document: every declared key present, every default
# materialized, every order-irrelevant array sorted. Absent-vs-default and key
# order therefore cannot change a hash, which is what makes two documents that
# mean the same thing hash identically.
# ---------------------------------------------------------------------------


def _string_leaf(
    value: Any,
    kind: str,
    spec: Leaf,
    pointer: str,
    refusals: list[VisualizationRefusal],
    context: WalkContext = _DEFAULT_CONTEXT,
) -> Any:
    if not isinstance(value, str):
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"this value must be text, not {type(value).__name__}",
                pointer,
                "Send the declared kind of value for this field.",
            )
        )
        return None
    if not _scan_string(value, pointer, refusals, context.noun):
        return None
    if kind == "member_id":
        if not _MEMBER_ID.match(value):
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    "this is not the shape of a Semantic View member id",
                    pointer,
                    "Bind a member the pinned Query Spec version selected.",
                )
            )
            return None
        return value
    if kind == "evidence_id":
        if not _EVIDENCE_ID.match(value):
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    "this is not the shape of an evidence id",
                    pointer,
                    "An annotation must name evidence the Result carries.",
                )
            )
            return None
        return value
    if kind == "label":
        if not _LABEL.match(value):
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"this is plain text of at most {MAX_LABEL_LENGTH} characters",
                    pointer,
                    "Shorten it, or leave it out and use the governed definition.",
                )
            )
            return None
        return value
    if kind == "enum":
        if value not in spec.values:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"`{value}` is not one of {', '.join(sorted(spec.values))}",
                    pointer,
                    "Choose one of the declared values.",
                )
            )
            return None
        return value
    if kind == "literal_str":
        if value != spec.literal:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"this field must be exactly `{spec.literal}`",
                    pointer,
                    f"Set it to `{spec.literal}`.",
                )
            )
            return None
        return value
    raise AssertionError(f"unhandled string leaf kind {kind}")  # pragma: no cover


def _walk_leaf(
    value: Any,
    spec: Leaf,
    pointer: str,
    refusals: list[VisualizationRefusal],
    context: WalkContext = _DEFAULT_CONTEXT,
) -> Any:
    kind = spec.kind
    if kind in {"member_id", "evidence_id", "label", "enum", "literal_str"}:
        return _string_leaf(value, kind, spec, pointer, refusals, context)
    if kind == "literal_int":
        if value is not True and isinstance(value, int) and value == spec.literal:
            return value
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"this field must be the integer {spec.literal}",
                pointer,
                f"Set it to {spec.literal}.",
            )
        )
        return None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                "this field is true or false",
                pointer,
                "Send a boolean.",
            )
        )
        return None
    if kind == "true_only":
        # AC4 clause 6, enforced as a TYPE rather than as a rule applied later:
        # there is no shape of this document in which truncation is undisclosed.
        if value is True:
            return True
        refusals.append(
            VisualizationRefusal(
                "truncation_not_disclosed",
                "a top-N is always display-only: it hides rows the Result returned, and a "
                "display-only subset may never be presented as the whole answer",
                pointer,
                "Set `display_only` to true, or remove `top_n` and show every returned row.",
            )
        )
        return None
    if kind == "positive_int":
        if isinstance(value, bool) or not isinstance(value, int):
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    "this field is a whole number",
                    pointer,
                    "Send an integer.",
                )
            )
            return None
        if value < 1 or value > MAX_INLINE_ROWS:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"this field is between 1 and {MAX_INLINE_ROWS}, the inline row bound",
                    pointer,
                    f"Choose a number between 1 and {MAX_INLINE_ROWS}.",
                )
            )
            return None
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    "this field is a number",
                    pointer,
                    "Send a number.",
                )
            )
            return None
        return value
    if kind == "member_id_list":
        if not isinstance(value, list):
            # An OBJECT here is the interesting case: it is how a caller smuggles
            # `{"expression": "sum(x)"}` or `{"encode": {...}}` into a well. Each
            # of its keys gets the same verdict it would get anywhere else, so the
            # refusal names the offending word instead of only the shape.
            if isinstance(value, dict):
                for key in value:
                    _unknown(_pointer(pointer, key), key, refusals, context)
            refusals.append(
                VisualizationRefusal(
                    "invalid_shape",
                    "this field is a list of member ids",
                    pointer,
                    "Send a list of member ids, even for a single member. A binding value is "
                    "an id, never a computation.",
                )
            )
            return None
        out: list[str] = []
        for index, entry in enumerate(value):
            resolved = _string_leaf(
                entry, "member_id", spec, _pointer(pointer, str(index)), refusals, context
            )
            if resolved is not None:
                out.append(resolved)
        return sorted(out) if spec.unordered else out
    if kind == "enum_list":
        if not isinstance(value, list):
            refusals.append(
                VisualizationRefusal(
                    "invalid_shape",
                    f"this field is a list of {spec.item_noun}",
                    pointer,
                    f"Choose from {', '.join(sorted(spec.values))}.",
                )
            )
            return None
        seen: list[str] = []
        for index, entry in enumerate(value):
            resolved = _string_leaf(
                entry, "enum", spec, _pointer(pointer, str(index)), refusals, context
            )
            if resolved is not None and resolved not in seen:
                seen.append(resolved)
        return sorted(seen) if spec.unordered else seen
    raise AssertionError(f"unhandled leaf kind {kind}")  # pragma: no cover


def _default_for(spec: Any) -> Any:
    if isinstance(spec, Leaf):
        if spec.kind == "literal_str" and spec.default is None:
            return spec.literal
        if spec.kind == "literal_int" and spec.default is None:
            return spec.literal
        if isinstance(spec.default, list):
            return list(spec.default)
        return spec.default
    if isinstance(spec, Node):
        if spec.nullable:
            return None
        return {key: _default_for(child) for key, child in spec.keys.items()}
    if isinstance(spec, ArrayOf):
        return []
    if isinstance(spec, MapOf):
        return {}
    raise AssertionError("unhandled spec")  # pragma: no cover


def _walk(
    value: Any,
    spec: Any,
    pointer: str,
    refusals: list[VisualizationRefusal],
    context: WalkContext = _DEFAULT_CONTEXT,
) -> Any:
    if isinstance(spec, Leaf):
        return _walk_leaf(value, spec, pointer, refusals, context)

    if isinstance(spec, Node):
        if value is None and spec.nullable:
            return None
        if not isinstance(value, dict):
            refusals.append(
                VisualizationRefusal(
                    "invalid_shape",
                    "this field is an object",
                    pointer,
                    "Send an object.",
                )
            )
            return _default_for(spec)
        out: dict[str, Any] = {}
        for key in value:
            if key not in spec.keys:
                _unknown(_pointer(pointer, key), key, refusals, context)
        for key, child in spec.keys.items():
            if key in value:
                # A NORMALIZED DOCUMENT MUST READ BACK. Normalization materializes
                # every declared key, so an optional leaf whose default is absence
                # is written `null` -- `top_n.n`, `reference_lines[].value`, and a
                # Chart Template's `requires[well].max`. Re-validating that stored
                # document (72.6 reads one to materialize a Spec; a version is
                # re-hashed to prove it did not move) must not refuse the very
                # shape this function wrote. An explicit `null` is therefore the
                # same statement as an absent key, and ONLY where the declared
                # default is itself absence: a literal, a list, a map and any leaf
                # with a real default stay strict, so `null` cannot erase one.
                if value[key] is None and not isinstance(child, Node):
                    if _default_for(child) is None:
                        out[key] = None
                        continue
                out[key] = _walk(value[key], child, _pointer(pointer, key), refusals, context)
                if out[key] is None and not isinstance(child, Node):
                    out[key] = _default_for(child)
            else:
                out[key] = _default_for(child)
        return out

    if isinstance(spec, ArrayOf):
        if not isinstance(value, list):
            refusals.append(
                VisualizationRefusal(
                    "invalid_shape",
                    "this field is a list",
                    pointer,
                    "Send a list.",
                )
            )
            return []
        if len(value) > spec.max_items:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"at most {spec.max_items} entries are allowed here",
                    pointer,
                    f"Keep at most {spec.max_items}.",
                )
            )
            return []
        return [
            _walk(entry, spec.item, _pointer(pointer, str(index)), refusals, context)
            for index, entry in enumerate(value)
        ]

    if isinstance(spec, MapOf):
        if not isinstance(value, dict):
            refusals.append(
                VisualizationRefusal(
                    "invalid_shape",
                    "this field is an object",
                    pointer,
                    "Send an object.",
                )
            )
            return {}
        if len(value) > spec.max_items:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"at most {spec.max_items} overrides are allowed",
                    pointer,
                    f"Keep at most {spec.max_items}.",
                )
            )
            return {}
        out_map: dict[str, Any] = {}
        for key in sorted(value):
            key_pointer = _pointer(pointer, key)
            # The KEY is validated too. A map is the one place an unchecked
            # string can enter a closed grammar, and `__proto__` arrives as a key.
            checked_key = _string_leaf(
                key,
                spec.key_kind,
                Leaf(spec.key_kind, spec.key_values),
                key_pointer,
                refusals,
                context,
            )
            resolved = _walk(value[key], spec.value, key_pointer, refusals, context)
            if checked_key is not None and resolved is not None:
                out_map[checked_key] = resolved
        return out_map

    raise AssertionError("unhandled spec")  # pragma: no cover


def walk_document(
    payload: Any,
    grammar: Mapping[str, Any],
    *,
    noun: str,
    required: tuple[str, ...],
    contract_literal: str,
    owned: Mapping[str, tuple[str, str, str]] | None = None,
) -> tuple[dict[str, Any], list[VisualizationRefusal]]:
    """THE walker, for any document whose grammar is written in this vocabulary.

    ONE WALKER, TWO DOCUMENTS (story 72.2). The Chart Template document is the
    Visualization Spec grammar minus its concrete `member_id` anchors plus a
    `requires` block, and `core.visualization_templates` derives that grammar
    from `GRAMMAR` rather than restating it. Deriving the grammar and then
    copying the walker would have moved the second authority one file to the
    left: the closed key set, the JSON pointers, the hostile-content scan, the
    defaults and the ordering that make two equal documents hash equal all live
    here, once.

    `noun` is the product word the refusals use, so a Chart Template is never
    refused in the words of a Visualization Spec. `owned` names the keys THIS
    document does not declare but whose owner is known -- for the template, the
    Spec keys it subtracts -- so the reader is told who owns a key instead of
    being told to spell it differently.

    Never raises for content: the caller collects these refusals together with
    the compatibility ones and refuses ONCE, with every reason.
    """
    context = WalkContext(noun=noun, owned=dict(owned or {}))
    refusals: list[VisualizationRefusal] = []
    if not isinstance(payload, dict):
        return {}, [
            VisualizationRefusal("invalid_shape", f"a {noun} is an object", "", "Send an object.")
        ]

    # The identity keys are REQUIRED. A document missing one is refused rather
    # than defaulted: defaulting a contract version makes an unversioned document
    # indistinguishable from a correct one the day the contract moves.
    for key in required:
        if key not in payload:
            refusals.append(
                VisualizationRefusal(
                    "missing_field",
                    f"`{key}` is required in every {noun}",
                    _pointer("", key),
                    (
                        f"Set `{key}` to `{contract_literal}`."
                        if key == "spec_contract_version"
                        else f"Set `{key}`."
                    ),
                )
            )

    normalized = _walk(payload, Node(dict(grammar)), "", refusals, context)
    if not isinstance(normalized, dict):  # pragma: no cover - _walk returns a dict for Node
        normalized = {}
    return normalized, refusals


def normalize_document(payload: Any) -> tuple[dict[str, Any], list[VisualizationRefusal]]:
    """Walk one proposed Visualization Spec. Returns (normalized, refusals)."""
    return walk_document(
        payload,
        GRAMMAR,
        noun="Visualization Spec",
        required=("spec_contract_version", "schema_version", "family"),
        contract_literal=VISUALIZATION_SPEC_CONTRACT_VERSION,
    )


# ---------------------------------------------------------------------------
# Compatibility against the PINNED Query Spec version (tier one, decision D4).
# ---------------------------------------------------------------------------


#: The cross-source plan contract, named here rather than imported, because
#: importing `multi_source_plan` from the presentation layer would make a
#: presentation module depend on an analytical one for one string.
_MULTI_SOURCE_PLAN_CONTRACT = "multi-source-plan.v1"


@dataclass(frozen=True)
class PinnedMembers:
    """The members the pinned Query Spec version actually selected, with roles.

    `measure` and `dimension` come from the Query Spec's own `measures[]` /
    `dimensions[]`, which the compiled artifact's `queryability_matrix` produced
    (`server/core/semantic_compiler.py:431-450`). `time` and `classification` are
    absent because no server source carries them -- and are reported as absent
    rather than guessed.
    """

    roles: dict[str, str]
    labels: dict[str, str]
    grain: str | None
    comparison: str
    row_limit: int
    query_spec_id: str
    semantic_view_id: str
    semantic_view_version_id: str
    result_shape: str | None = None

    def role_of(self, member_id: str) -> str | None:
        return self.roles.get(member_id)


def resolve_multi_source_plan_members(
    conn, *, project_id: str, spec: Mapping[str, Any]
) -> list[dict[str, str]]:
    """The bindable members of a `multi-source-plan.v1` version, NAMED.

    ONE PRODUCER, because this defect came back through a second one. The rail
    (`visualization_specs_api._visualization_options`) and the binding check
    (`load_pinned_query_spec_version`) both read the same plan for the same
    members; while each parsed it on its own, the fix landed on 2026-08-12 for
    `query-spec.v1` (commit `b96cd6a9`) could not reach the cross-source branch
    added afterwards, and the rail printed `mdm_01KZ...` again. Story 66.8:
    "The label is the canonical name, the id is the column. A legend reading
    `mdm_01KZ...` is a legend nobody can use."

    The id is the Result column -- `m_<field>` for a measure, `r_<field>` for a
    recomputed ratio, `k_<field>` for a conformed key -- and the label is the
    canonical name read from the vocabulary registry. Never the reverse: a
    binding that resolved on the label would break the day two fields share one.

    `version_id` is empty on purpose. A cross-source plan pins no semantic
    concept version, so there is nothing for `load_member_labels` or the five
    presentation facets to read; stating the absence is honest, inventing a
    version id would not be.

    When the vocabulary has no row for a pinned field, the identifier remains the
    label -- ugly and true, the same fallback `query-spec.v1` has carried since
    `b96cd6a9`. The alternative would be to manufacture a name.
    """
    from core.canonical_field_registry import load_canonical_names  # noqa: PLC0415

    measures: list[tuple[str, str]] = []
    for member in spec.get("members") or []:
        for measure in (member or {}).get("measures") or []:
            field_id = str((measure or {}).get("canonical_field_id") or "")
            if field_id:
                measures.append(
                    (str((measure or {}).get("result_field") or f"m_{field_id}"), field_id)
                )
    for derived in spec.get("derived_measures") or []:
        field_id = str((derived or {}).get("canonical_field_id") or "")
        if field_id:
            measures.append(
                (str((derived or {}).get("result_field") or f"r_{field_id}"), field_id)
            )

    #: A conformed key already travels with its `canonical_name`, frozen into the
    #: plan when it compiled (`multi_source_plan._compile_edge`). That frozen word
    #: wins over today's registry: the plan is immutable, and a later rename must
    #: not restate what it pinned.
    dimensions: list[tuple[str, str, str]] = []
    seen_keys: set[str] = set()
    for edge in spec.get("edges") or []:
        for component in (edge or {}).get("components") or []:
            field_id = str((component or {}).get("canonical_field_id") or "")
            if field_id and f"k_{field_id}" not in seen_keys:
                seen_keys.add(f"k_{field_id}")
                dimensions.append(
                    (
                        f"k_{field_id}",
                        field_id,
                        str((component or {}).get("canonical_name") or ""),
                    )
                )

    names = load_canonical_names(
        conn,
        project_id=project_id,
        field_ids=[field_id for _, field_id in measures]
        + [field_id for _, field_id, frozen in dimensions if not frozen],
    )

    resolved: list[dict[str, str]] = [
        {
            "id": result_field,
            "version_id": "",
            "label": names.get(field_id) or field_id,
            "role": ROLE_MEASURE,
            "canonical_field_id": field_id,
        }
        for result_field, field_id in measures
    ]
    resolved.extend(
        {
            "id": result_field,
            "version_id": "",
            "label": frozen or names.get(field_id) or field_id,
            "role": ROLE_DIMENSION,
            "canonical_field_id": field_id,
        }
        for result_field, field_id, frozen in dimensions
    )
    return resolved


def load_pinned_query_spec_version(
    conn, *, project_id: str, query_spec_version_id: str
) -> PinnedMembers:
    """The members, roles, grain and comparison of ONE pinned Query Spec version.

    PUBLIC SINCE 2026-09-01 (story 72.3), and for one reason: a Chart Template's
    compatibility verdict must read a Result's roles from the same place the
    Visualization Spec validator reads them. A second reader of `app.
    query_spec_versions` would be a second opinion on what a member's role is,
    and the two would disagree the first time either learned a new contract --
    which is exactly what happened between this function and the rail before
    `resolve_multi_source_plan_members` became the one producer.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT query_spec_id, semantic_view_id, semantic_view_version_id, spec
            FROM app.query_spec_versions
            WHERE id = %s AND project_id = %s
            """,
            (query_spec_version_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise VisualizationNotFound("query spec version not found in this Project")

    spec = row[3] if isinstance(row[3], dict) else {}
    roles: dict[str, str] = {}
    labels: dict[str, str] = {}

    # STORY 66.8 -- TWO CONTRACTS ANSWER "WHICH MEMBERS MAY BE BOUND", and this
    # read is where the second one had to be learned. A cross-source plan
    # (`multi-source-plan.v1`, story 66.4) does not carry `measures[].id`: its
    # members each contribute canonical fields, and the columns the Result
    # actually lands are the ones `multi_source_execution.result_schema` names --
    # `k_<field>` for a conformed key, `m_<field>` for a measure, `r_<field>` for
    # a recomputed ratio.
    #
    # Without this branch a Visualization Spec pinned to a cross-source plan
    # resolved ZERO members, so every binding was refused `unknown_member` and no
    # chart of a two-source analysis could ever be saved. Reading the plan under
    # the v1 shape would have been worse: it would have found nothing and said
    # nothing.
    if str(spec.get("contract_version") or "") == _MULTI_SOURCE_PLAN_CONTRACT:
        for entry in resolve_multi_source_plan_members(
            conn, project_id=project_id, spec=spec
        ):
            roles[entry["id"]] = entry["role"]
            labels[entry["id"]] = entry["label"]
        return PinnedMembers(
            roles=roles,
            labels=labels,
            grain=(str(spec.get("grain")) if spec.get("grain") else None),
            # A cross-source plan declares no period comparison and no result
            # shape today. Stated as the defaults rather than invented: a
            # comparison nobody asked for would change what the chart claims.
            comparison="none",
            row_limit=int((spec.get("bounds") or {}).get("row_limit") or 0),
            query_spec_id=str(row[0]),
            semantic_view_id=str(row[1]),
            semantic_view_version_id=str(row[2]),
            result_shape=None,
        )

    members: list[dict[str, str]] = []
    for entry in spec.get("measures") or []:
        member_id = str((entry or {}).get("id") or "")
        if member_id:
            roles[member_id] = ROLE_MEASURE
            version_id = str((entry or {}).get("version_id") or "")
            members.append({"id": member_id, "version_id": version_id})
    for entry in spec.get("dimensions") or []:
        member_id = str((entry or {}).get("id") or "")
        if member_id:
            roles[member_id] = ROLE_DIMENSION
            version_id = str((entry or {}).get("version_id") or "")
            members.append({"id": member_id, "version_id": version_id})

    #  THE LABEL IS THE CANONICAL NAME, THE ID IS THE COLUMN -- story 66.8, and it
    #  had been honoured on the cross-source branch above and nowhere else. This
    #  branch wrote `labels[member_id] = member_id`, so every sentence that names
    #  a member through `_member_label` -- a `role_mismatch` on a Visualization
    #  Spec, a Chart Template's compatibility verdict (story 72.3) -- printed a
    #  `sc_01KZ...` where a person expects "Channel". An absent label stays the
    #  identifier: ugly and true, the fallback `load_member_labels` documents.
    named = load_member_labels(conn, project_id=project_id, members=members)
    labels = {member["id"]: named.get(member["id"]) or member["id"] for member in members}

    return PinnedMembers(
        roles=roles,
        labels=labels,
        grain=(str(spec.get("grain")) if spec.get("grain") else None),
        comparison=str(spec.get("comparison") or "none"),
        row_limit=int(spec.get("row_limit") or 0),
        query_spec_id=str(row[0]),
        semantic_view_id=str(row[1]),
        semantic_view_version_id=str(row[2]),
        result_shape=(str(spec.get("result_shape")) if spec.get("result_shape") else None),
    )


def _member_label(pinned: PinnedMembers, member_id: str) -> str:
    return pinned.labels.get(member_id, member_id)


def member_columns(result_schema: Mapping[str, Any] | None) -> dict[str, str]:
    """`{member id: the Result column it landed in}`, from the map the server ships.

    THE SERVER-SIDE TWIN OF AI-337. A binding names a MEMBER -- that is what
    `check_shape_compatibility` validates a document against -- while
    `query_execution.shape_result_payload` keys each row by the member's COLUMN
    NAME and declares both halves on the schema (`{id, name}`). Reading
    `row[member_id]` therefore finds nothing on any Result a warehouse actually
    produced. The runtime learned this on 2026-08-31
    (`ui/cards/shell/src/viz/compile/dataset.ts:173`); this side had the same
    defect in `evaluate_result_disclosures`, where every cardinality count came
    out zero because the rows are keyed by column and the bindings by member --
    so a dimension with 900 distinct values disclosed nothing at all.

    A field with no `id` maps to itself, so an envelope that keys its rows by
    member id reads exactly as before.
    """
    columns: dict[str, str] = {}
    for declared in (result_schema or {}).get("fields") or []:
        if not isinstance(declared, Mapping):
            continue
        name = declared.get("name")
        if not isinstance(name, str) or not name:
            continue
        member_id = declared.get("id")
        columns[member_id if isinstance(member_id, str) and member_id else name] = name
    return columns


def count_distinct_values(rows: Sequence[Mapping[str, Any]], column: str) -> int:
    """How many distinct values one column holds in the rows that were READ.

    ONE COUNTER FOR TWO OBJECTS. The Visualization Spec's disclosures and the
    Chart Template's compatibility verdict (story 72.3) ask the same question of
    the same rows, and a second implementation would answer it differently the
    first time either learned about nulls. It is a count over the rows given and
    claims nothing about the rows a truncated Result did not carry.
    """
    return len({str(row.get(column)) for row in rows if column in row})


def _check_specialized_result_shape(
    normalized: dict[str, Any],
    family: VisualFamily,
    pinned: PinnedMembers,
) -> list[VisualizationRefusal]:
    refusals: list[VisualizationRefusal] = []
    if pinned.result_shape != family.result_shape:
        refusals.append(
            VisualizationRefusal(
                "incompatible_result_shape",
                f"the {family.label} family requires a pinned {family.result_shape} Result",
                "/family",
                "Execute a Query Spec whose published Semantic View policy declares "
                f"{family.result_shape}.",
            )
        )
    expected = {well: list(fields) for well, fields in family.result_field_bindings}
    actual = normalized.get("bindings") or {}
    changed = [
        well
        for well in WELL_ROLES
        if list(actual.get(well) or []) != list(expected.get(well) or [])
    ]
    if changed:
        refusals.append(
            VisualizationRefusal(
                "incompatible_result_binding",
                f"the {family.label} family requires its exact stable Result field bindings; "
                f"{', '.join(changed)} differed",
                "/bindings",
                "Use the field bindings published by the visual-family registry.",
            )
        )
    return refusals


def check_shape_compatibility(
    normalized: dict[str, Any], family: VisualFamily, pinned: PinnedMembers
) -> list[VisualizationRefusal]:
    """AC4 clauses 1, 2, 5 -- field existence, semantic role, grain and comparison.

    Cardinality and volume are NOT here: they are properties of one exact Result,
    not of the saved spec (decision D4). Freezing them into a version would make a
    Visualization a cached analytical answer, which the object model forbids.
    """
    refusals: list[VisualizationRefusal] = []
    bindings = normalized.get("bindings") or {}

    if family.result_shape is not None:
        return [
            *_check_specialized_result_shape(normalized, family, pinned),
            *check_grain_and_comparison(family, pinned),
        ]

    for well_name in WELL_ROLES:
        bound = bindings.get(well_name) or []
        pointer = f"/bindings/{well_name}"
        well = family.well(well_name)

        if bound and well is None:
            refusals.append(
                VisualizationRefusal(
                    "role_mismatch",
                    f"the {family.label} family has no {WELL_LABELS[well_name]} well",
                    pointer,
                    f"Remove this binding, or choose a family that declares "
                    f"{WELL_LABELS[well_name]}.",
                )
            )
            continue

        if well is None:
            continue

        if bound and not well.available:
            # The Time well, and any family well that accepts only
            # classification. Refusing here is the point: an enabled well that
            # accepted any member because the validator cannot tell the roles
            # apart is the invented role AC4 clause 2 forbids.
            refusals.append(
                VisualizationRefusal(
                    "role_unavailable",
                    f"the {WELL_LABELS[well_name]} well cannot be validated: "
                    f"{ROLE_UNAVAILABLE_REASON}",
                    pointer,
                    f"Leave it empty until {ROLE_UNAVAILABLE_OWNER} carries the role.",
                )
            )
            continue

        if well.required and not bound:
            refusals.append(
                VisualizationRefusal(
                    "missing_binding",
                    f"the {family.label} family needs a member in {WELL_LABELS[well_name]}",
                    pointer,
                    f"Bind one of the members the Query Spec selected into "
                    f"{WELL_LABELS[well_name]}.",
                )
            )

        if len(bound) > well.max_members:
            refusals.append(
                VisualizationRefusal(
                    "too_many_members",
                    f"{WELL_LABELS[well_name]} accepts at most {well.max_members} member(s) "
                    f"on the {family.label} family; {len(bound)} were bound",
                    pointer,
                    f"Keep at most {well.max_members}.",
                )
            )

        for index, member_id in enumerate(bound):
            member_pointer = f"{pointer}/{index}"
            role = pinned.role_of(member_id)
            if role is None:
                # AC3's connector-field clause lands HERE: a connector column name
                # is simply not in the pinned version's member set. Deliberately no
                # "did you mean" -- Story 50.1 established that rule
                # (`50-1-...md:320-321`) and a suggestion invites a caller to accept
                # a member they did not ask for.
                refusals.append(
                    VisualizationRefusal(
                        "unknown_member",
                        f"`{member_id}` is not a member of the pinned Query Spec version",
                        member_pointer,
                        "Bind a member this query selected, or add it in Explore -- which "
                        "produces a new Result.",
                    )
                )
                continue
            if role not in well.accepts:
                refusals.append(
                    VisualizationRefusal(
                        "role_mismatch",
                        f"`{_member_label(pinned, member_id)}` is a {role}; the "
                        f"{WELL_LABELS[well_name]} well accepts "
                        f"{' or '.join(sorted(well.accepts))}",
                        member_pointer,
                        f"Bind a {' or '.join(sorted(well.accepts & AVAILABLE_ROLES)) or 'member'} "
                        f"here, or choose a family whose wells match this member.",
                    )
                )

    # Members named outside a well -- thresholds, reference lines, evidence,
    # label overrides -- resolve against the same set. There is no second door.
    for index, entry in enumerate(normalized.get("thresholds") or []):
        member_id = (entry or {}).get("member_id")
        if member_id and pinned.role_of(member_id) is None:
            refusals.append(
                VisualizationRefusal(
                    "unknown_member",
                    f"`{member_id}` is not a member of the pinned Query Spec version",
                    f"/thresholds/{index}/member_id",
                    "Threshold a member this query selected.",
                )
            )
    for index, entry in enumerate(normalized.get("reference_lines") or []):
        member_id = (entry or {}).get("member_id")
        if member_id and pinned.role_of(member_id) is None:
            refusals.append(
                VisualizationRefusal(
                    "unknown_member",
                    f"`{member_id}` is not a member of the pinned Query Spec version",
                    f"/reference_lines/{index}/member_id",
                    "Reference a member this query selected.",
                )
            )
    for index, member_id in enumerate((normalized.get("evidence") or {}).get("datum_fields") or []):
        if pinned.role_of(member_id) is None:
            refusals.append(
                VisualizationRefusal(
                    "unknown_member",
                    f"`{member_id}` is not a member of the pinned Query Spec version",
                    f"/evidence/datum_fields/{index}",
                    "Bind evidence to a member this query selected.",
                )
            )
    for member_id in sorted(((normalized.get("labels") or {}).get("override") or {})):
        if pinned.role_of(member_id) is None:
            refusals.append(
                VisualizationRefusal(
                    "unknown_member",
                    f"`{member_id}` is not a member of the pinned Query Spec version",
                    f"/labels/override/{member_id}",
                    "Override the label of a member this query selected.",
                )
            )

    refusals.extend(check_grain_and_comparison(family, pinned))
    return refusals


def check_grain_and_comparison(
    family: VisualFamily, pinned: PinnedMembers
) -> list[VisualizationRefusal]:
    """AC4 clause 5 -- required grain and comparison metadata, read off the QUERY.

    Both live in the Query Spec, which is the whole point: a family that needs a
    time grain cannot manufacture one from a presentation edit.
    """
    refusals: list[VisualizationRefusal] = []
    if family.requires_time_grain and not pinned.grain:
        refusals.append(
            VisualizationRefusal(
                "missing_grain",
                f"the {family.label} family needs a time grain, and the pinned Query Spec "
                "version carries none",
                "/family",
                "Set a grain in Explore -- which produces a new Result -- or choose a family "
                "that needs no grain, such as Bar or Table.",
            )
        )
    if family.requires_comparison and pinned.comparison in {"", "none"}:
        refusals.append(
            VisualizationRefusal(
                "missing_comparison",
                f"the {family.label} family needs a comparison, and the pinned Query Spec "
                "version carries none",
                "/family",
                "Set a comparison in Explore -- which produces a new Result -- or choose a "
                "family that needs none.",
            )
        )
    return refusals


# ---------------------------------------------------------------------------
# Tier two: cardinality and volume against ONE exact Result (decision D4).
#
# Returned as DISCLOSURES, never written into the spec. A spec that pinned a row
# count would be a cached analytical answer (`visualization-and-rendering.md:43`)
# and would need a new version every time the data moved.
# ---------------------------------------------------------------------------


def evaluate_result_disclosures(
    normalized: dict[str, Any],
    family: VisualFamily,
    *,
    rows: list[dict[str, Any]],
    row_count: int,
    truncated: bool,
    outcome: str,
    result_schema: dict[str, Any] | None = None,
    result_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AC4 clauses 3, 4 and 6, evaluated against one Result. Never persisted."""
    refusals: list[VisualizationRefusal] = []
    bindings = normalized.get("bindings") or {}
    cardinalities: dict[str, int] = {}

    if family.result_shape is not None:
        expected_fields = [{"id": field_id, "name": name} for field_id, name in WATERFALL_FIELDS]
        expected_bindings = {name: field_id for field_id, name in WATERFALL_FIELDS}
        schema = result_schema or {}
        manifest = result_manifest or {}
        if (
            schema.get("contract") != family.result_shape
            or schema.get("fields") != expected_fields
            or manifest.get("result_shape") != family.result_shape
            or manifest.get("field_bindings") != expected_bindings
        ):
            refusals.append(
                VisualizationRefusal(
                    "incompatible_result_shape",
                    f"this Result does not carry the exact {family.result_shape} schema "
                    "and stable field bindings",
                    "/result",
                    "Execute the pinned Query Spec again through the governed Result path.",
                )
            )

    #  MEMBER -> COLUMN, from the schema the Result declares. Counting `row[member_id]`
    #  read nothing on every Result a warehouse produced (see `member_columns`).
    columns = member_columns(result_schema)

    for well_name in WELL_ROLES:
        well = family.well(well_name)
        if well is None or well.max_cardinality is None:
            continue

        for index, member_id in enumerate(bindings.get(well_name) or []):
            distinct = count_distinct_values(rows, columns.get(member_id, member_id))
            cardinalities[f"{well_name}/{member_id}"] = distinct
            if distinct > well.max_cardinality:
                refusals.append(
                    VisualizationRefusal(
                        "cardinality_over_limit",
                        f"`{member_id}` has {distinct} distinct values in this Result; the "
                        f"{WELL_LABELS[well_name]} well of the {family.label} family reads at "
                        f"most {well.max_cardinality}",
                        f"/bindings/{well_name}/{index}",
                        f"Filter the query in Explore, or choose a family that reads more "
                        f"than {well.max_cardinality} values, such as Table.",
                    )
                )

    series_count = 1 if family.result_shape else max(1, len(bindings.get("measure") or []))
    marks = row_count * series_count
    if marks > family.max_marks:
        refusals.append(
            VisualizationRefusal(
                "volume_over_limit",
                f"this Result would draw {marks} marks; the {family.label} family reads at "
                f"most {family.max_marks}",
                "/family",
                f"Reduce the query's rows in Explore, or choose a family that reads more "
                f"than {family.max_marks} marks.",
            )
        )

    top_n = normalized.get("top_n")
    if isinstance(top_n, dict) and isinstance(top_n.get("n"), int) and row_count:
        if top_n["n"] > row_count:
            refusals.append(
                VisualizationRefusal(
                    "truncation_not_disclosed",
                    f"the top-N asks for {top_n['n']} rows and this Result returned "
                    f"{row_count}; a display-only subset may not claim rows the Result "
                    "never carried",
                    "/top_n/n",
                    f"Lower the top-N to at most {row_count}, or remove it.",
                )
            )

    return {
        "compatible": not refusals,
        "outcome": outcome,
        "returned_row_count": row_count,
        "truncated": truncated,
        "marks": marks,
        "cardinalities": cardinalities,
        "family_max_marks": family.max_marks,
        "refusals": [r.as_dict() for r in refusals],
        # Restated with the disclosures so a caller reading only this block still
        # sees the obligation. AC8 has no off switch.
        "table_fallback": "required",
        "table_fallback_columns": list(family.table_fallback_wells),
    }


# ---------------------------------------------------------------------------
# Validation, and the immutable version it produces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedVisualizationSpec:
    """A presentation proven legal against one exact Query Spec version."""

    spec: dict[str, Any]
    content_hash: str
    family: str
    query_spec_id: str
    query_spec_version_id: str
    #: Carried so a caller does not re-read the pin to display the members.
    pinned: PinnedMembers = field(repr=False, default=None)  # type: ignore[assignment]


def validate_visualization_spec(
    conn, *, project_id: str, query_spec_version_id: str, payload: dict[str, Any]
) -> ValidatedVisualizationSpec:
    """Validate one proposed presentation against its exact pinned Query Spec version.

    Raises `VisualizationNotFound` for an unresolvable pin and
    `VisualizationSpecRefused` carrying EVERY reason for a resolvable but illegal
    document. Returns the normalized, hashable spec on success. Never repairs,
    never substitutes, never strips.
    """
    pinned = load_pinned_query_spec_version(
        conn, project_id=project_id, query_spec_version_id=query_spec_version_id
    )

    normalized, refusals = normalize_document(payload)

    family_id = normalized.get("family")
    family = get_family(family_id) if isinstance(family_id, str) else None
    if family is None:
        # A family the walker already refused (unknown enum member, missing key)
        # produces no second refusal at the same pointer: one control, one reason.
        if not any(r.subject == "/family" for r in refusals):
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"a visual family is required; the declared families are "
                    f"{', '.join(FAMILY_IDS)}",
                    "/family",
                    "Choose one of the declared families.",
                )
            )
    else:
        refusals.extend(check_shape_compatibility(normalized, family, pinned))

    size = len(canonical_bytes(normalized))
    if size > MAX_SPEC_BYTES:
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"this document is {size} bytes; a Visualization Spec may not exceed "
                f"{MAX_SPEC_BYTES}",
                "",
                "Remove annotations, thresholds or label overrides.",
            )
        )

    if refusals:
        raise VisualizationSpecRefused(
            "visualization_spec_refused",
            f"the presentation was refused on {len(refusals)} point(s)",
            refusals,
        )

    return ValidatedVisualizationSpec(
        spec=normalized,
        content_hash=canonical_hash(normalized),
        family=str(family_id),
        query_spec_id=pinned.query_spec_id,
        query_spec_version_id=query_spec_version_id,
        pinned=pinned,
    )


def canonical_bytes(value: Any) -> bytes:
    """The one byte-form a document is measured and stored in.

    Public because the Chart Template is measured against the SAME ceiling by
    the same function (story 72.2, AC8). A second serializer would make two
    documents of equal meaning differ in size, and the `pg_column_size` CHECK
    that mirrors the ceiling would then disagree with the service.
    """
    import json  # noqa: PLC0415

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


#: Who proposed a version. Evidence, never a permission: both values travel the
#: same route, the same validator and the same deny-list corpus (AC10).
PROPOSED_BY = frozenset({"person", "model"})


def create_visualization_spec_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    validated: ValidatedVisualizationSpec,
    actor: str,
    proposed_by: str = "person",
    visualization_id: str | None = None,
    name: str | None = None,
    materialized_from_template_version_id: str | None = None,
) -> dict[str, Any]:
    """Append the next immutable version, creating the stable head when absent.

    Both writes happen in the CALLER's transaction, exactly as
    `create_query_spec_version` does, which is what makes migration 156's deferred
    head foreign key safe.

    `materialized_from_template_version_id` is PROVENANCE and nothing else (story
    72.6, AC23): it is written to the column migration 335 added, it is not part
    of `spec`, so it does not enter `content_hash`, and no validator above reads
    it. A version materialised from a Chart Template therefore takes this exact
    route, meets this exact validator and produces the exact hash a person
    composing the same presentation by hand would produce. `None` -- every
    Builder path -- writes NULL.
    """
    if proposed_by not in PROPOSED_BY:
        raise VisualizationSpecRefused(
            "visualization_spec_refused",
            "a proposal is made by a person or by a model",
            [
                VisualizationRefusal(
                    "invalid_value",
                    f"`{proposed_by}` is not one of {', '.join(sorted(PROPOSED_BY))}",
                    "/proposed_by",
                    "Send `person` or `model`.",
                )
            ],
        )

    with conn.cursor() as cur:
        if visualization_id is None:
            visualization_id = f"vis_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.visualizations
                    (id, org_id, project_id, query_spec_id, name, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (visualization_id, org_id, project_id, validated.query_spec_id, name, actor),
            )
            predecessor = None
            version_number = 1
        else:
            cur.execute(
                """
                SELECT current_version_id, query_spec_id FROM app.visualizations
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (visualization_id, org_id, project_id),
            )
            head = cur.fetchone()
            if head is None:
                raise VisualizationNotFound("visualization not found in this Project")
            predecessor, head_query_spec_id = head[0], head[1]
            # The pin may move to a NEW VERSION of the same Query Spec -- that is
            # AC5's re-pin, and it is revalidated in full above. It may NOT cross
            # to another Query Spec: version N+1 would then answer a different
            # question while `app.visualizations.query_spec_id` still advertised
            # the old one, and every consumer that resolves the head (a Report's
            # default presentation, a Render pin, Story 50.5's runtime) would get
            # the wrong query. Migration 164 makes the same statement in the
            # database, for the callers that never reach this line.
            if head_query_spec_id != validated.query_spec_id:
                raise VisualizationSpecRefused(
                    "visualization_spec_refused",
                    "this revision would move the Visualization to a different Query Spec",
                    [
                        VisualizationRefusal(
                            "query_spec_mismatch",
                            "this Visualization presents Query Spec "
                            f"`{head_query_spec_id}`; the pinned version "
                            f"`{validated.query_spec_version_id}` belongs to "
                            f"`{validated.query_spec_id}`",
                            "/query_spec_version_id",
                            "Pin a version of the same Query Spec, or save this "
                            "presentation as a new Visualization.",
                        )
                    ],
                )
            if predecessor is None:
                raise VisualizationSpecRefused(
                    "visualization_spec_refused",
                    "this Visualization has no current version to revise",
                    [
                        VisualizationRefusal(
                            "head_without_version",
                            "the head carries no version to succeed",
                            "",
                            "Create the Visualization again; version 1 was never written.",
                        )
                    ],
                )
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM app.visualization_spec_versions
                WHERE visualization_id = %s AND project_id = %s
                """,
                (visualization_id, project_id),
            )
            version_number = int(cur.fetchone()[0])

        version_id = f"vsv_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family, spec,
                 content_hash, predecessor_version_id, proposed_by, created_by,
                 materialized_from_template_version_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING id, version_number, content_hash, created_at
            """,
            (
                version_id,
                visualization_id,
                org_id,
                project_id,
                version_number,
                validated.query_spec_id,
                validated.query_spec_version_id,
                VISUALIZATION_SPEC_CONTRACT_VERSION,
                VISUALIZATION_SPEC_SCHEMA_VERSION,
                validated.family,
                canonical_bytes(validated.spec).decode("utf-8"),
                validated.content_hash,
                predecessor,
                proposed_by,
                actor,
                materialized_from_template_version_id,
            ),
        )
        row = cur.fetchone()
        cur.execute(
            """
            UPDATE app.visualizations
            SET current_version_id = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, visualization_id, org_id, project_id),
        )

    return {
        "visualization_id": visualization_id,
        "id": row[0],
        "version_number": row[1],
        "content_hash": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
        "family": validated.family,
        "query_spec_id": validated.query_spec_id,
        "query_spec_version_id": validated.query_spec_version_id,
        "proposed_by": proposed_by,
        "spec": validated.spec,
        "materialized_from_template_version_id": materialized_from_template_version_id,
    }


# ---------------------------------------------------------------------------
# AC7's left-rail metadata, SERVED -- or declared absent with its owner.
#
# AC7 requires each rail entry to show its definition, grain, additivity, quality
# state and provenance hint, "served by the server". Serving `{id, version_id,
# label, role}` and letting the rail print `role · pinned version ...` met none of
# it, and said nothing about the four facets it was not carrying -- which is the
# failure mode this story spends AC7 forbidding for the `Time` and
# `Classifications` groups.
#
# Four facets have a real source: `app.semantic_concept_versions` carries
# `definition`, `allowed_grains`, `additivity_class` and `provenance` for exactly
# the concept version a Query Spec member pins (`query_specs.py:300-314` resolves
# `version_id` off the compiled matrix). The fifth, quality state, comes from the
# Data Quality monitors that target the concept
# (`app.dq_monitors.target_kind = 'semantic_concept'`, `runtime_state`).
#
# NOTHING IS INFERRED. When a facet has no recorded value for a member, the
# ABSENCE travels with the surface that owns writing it, so the rail states it
# the same way it states the missing time and classification roles. A "grain:
# daily" invented from a member's name would be the manufactured semantic
# authority AC4 clause 2 forbids, one facet down.
# ---------------------------------------------------------------------------

#: The five facets AC7 names, in AC7's order.
MEMBER_METADATA_FACETS: tuple[str, ...] = (
    "definition",
    "grain",
    "additivity",
    "quality_state",
    "provenance_hint",
)

#: Which surface owns writing each facet, named so a reader can act on an absence
#: rather than file a bug against the Builder.
MEMBER_METADATA_OWNERS: dict[str, str] = {
    "definition": "Semantic Model - concept definition (Epics 47 / 49)",
    "grain": "Semantic Model - allowed grains (Epics 47 / 49)",
    "additivity": "Semantic Model - additivity class (Epics 47 / 49)",
    "quality_state": "Data Quality monitors (Epic 33)",
    "provenance_hint": "Semantic Model - concept provenance (Epics 47 / 49)",
}

#: Worst-first, so a member watched by several monitors reports the state that
#: matters. `healthy` is last on purpose: one healthy monitor never cancels a
#: failing one.
_RUNTIME_STATE_SEVERITY: tuple[str, ...] = (
    "failing",
    "degraded",
    "unavailable",
    "paused",
    "not_applicable",
    "healthy",
)


def _provenance_hint(value: Any) -> str | None:
    """A faithful, bounded projection of the recorded provenance -- not a reading.

    `app.semantic_concept_versions.provenance` is free-form JSONB with no schema,
    so this surface refuses to interpret it: it prints the scalar entries it finds
    and nothing else. Naming a key this surface expects (`source_system`, say)
    would be inventing a contract the semantic model never declared.
    """
    if not isinstance(value, dict) or not value:
        return None
    parts = [
        f"{key}: {entry}"
        for key, entry in sorted(value.items())
        if isinstance(entry, str | int | float | bool)
    ]
    if not parts:
        return None
    hint = "; ".join(parts)
    return hint if len(hint) <= 240 else f"{hint[:237]}..."


def load_member_presentation_metadata(
    conn, *, project_id: str, members: list[dict[str, str]]
) -> dict[str, dict[str, Any]]:
    """AC7's five facets per member, each either a value or a named absence.

    `members` is `[{"id": concept_id, "version_id": concept_version_id}, ...]` as
    the pinned Query Spec version records them. Returns a map keyed by member id,
    every entry carrying all five facets, every facet carrying `{value, owner}`
    with `value = None` when nothing is recorded.
    """
    version_ids = [m["version_id"] for m in members if m.get("version_id")]
    concept_ids = [m["id"] for m in members if m.get("id")]

    by_version: dict[str, tuple] = {}
    if version_ids:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, definition, allowed_grains, additivity_class, provenance
                FROM app.semantic_concept_versions
                WHERE project_id = %s AND id = ANY(%s)
                """,
                (project_id, version_ids),
            )
            by_version = {str(r[0]): r for r in cur.fetchall()}

    quality: dict[str, str] = {}
    if concept_ids:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT target_id, runtime_state
                FROM app.dq_monitors
                WHERE project_id = %s
                  AND target_kind = 'semantic_concept'
                  AND target_id = ANY(%s)
                  AND lifecycle_status = 'published'
                """,
                (project_id, concept_ids),
            )
            for target_id, state in cur.fetchall():
                current = quality.get(str(target_id))
                candidate = str(state)
                if current is None or _RUNTIME_STATE_SEVERITY.index(
                    candidate
                ) < _RUNTIME_STATE_SEVERITY.index(current):
                    quality[str(target_id)] = candidate

    out: dict[str, dict[str, Any]] = {}
    for member in members:
        member_id = member.get("id") or ""
        if not member_id:
            continue
        row = by_version.get(member.get("version_id") or "")
        grains = list(row[2] or []) if row else []
        values = {
            "definition": (row[1] if row else None) or None,
            "grain": ", ".join(grains) if grains else None,
            "additivity": (row[3] if row else None) or None,
            "quality_state": quality.get(member_id),
            "provenance_hint": _provenance_hint(row[4]) if row else None,
        }
        out[member_id] = {
            facet: {"value": values[facet], "owner": MEMBER_METADATA_OWNERS[facet]}
            for facet in MEMBER_METADATA_FACETS
        }
    return out


def load_member_labels(
    conn, *, project_id: str, members: list[dict[str, str]]
) -> dict[str, str]:
    """The READABLE name of every pinned member: `{concept_id: label}`.

    WHY A SEPARATE READ AND NOT A SIXTH FACET. The five facets of
    `load_member_presentation_metadata` are a contract (AC7) that the rail renders
    as-is; a label is not one of them, it is the member's identity. Folding it into
    the facet table would make "Label" appear as a sixth metadata row in the rail.

    The label is read off the PINNED concept version, not off the concept: that is
    the version the query selected, and it is the same word `query-facets` serves,
    so the two surfaces cannot name one thing two ways. An absence stays an
    absence -- the caller then falls back to the identifier, which is ugly and
    true.
    """
    version_ids = [m["version_id"] for m in members if m.get("version_id")]
    if not version_ids:
        return {}
    by_version: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label FROM app.semantic_concept_versions
            WHERE project_id = %s AND id = ANY(%s)
            """,
            (project_id, version_ids),
        )
        by_version = {str(row[0]): str(row[1] or "") for row in cur.fetchall()}
    return {
        str(m["id"]): by_version[m["version_id"]]
        for m in members
        if m.get("id") and by_version.get(m.get("version_id") or "")
    }


def load_visualization(conn, *, org_id: str, project_id: str, visualization_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, query_spec_id, name, current_version_id, created_by, created_at, updated_at
            FROM app.visualizations
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (visualization_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise VisualizationNotFound("visualization not found in this Project")
        cur.execute(
            """
            SELECT id, version_number, family, content_hash, query_spec_version_id,
                   predecessor_version_id, proposed_by, created_by, created_at
            FROM app.visualization_spec_versions
            WHERE visualization_id = %s AND org_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (visualization_id, org_id, project_id),
        )
        versions = cur.fetchall()
    return {
        "id": head[0],
        "query_spec_id": head[1],
        "name": head[2],
        "current_version_id": head[3],
        "created_by": head[4],
        "created_at": head[5].isoformat() if head[5] else None,
        "updated_at": head[6].isoformat() if head[6] else None,
        "versions": [
            {
                "id": v[0],
                "version_number": v[1],
                "family": v[2],
                "content_hash": v[3],
                "query_spec_version_id": v[4],
                "predecessor_version_id": v[5],
                "proposed_by": v[6],
                "created_by": v[7],
                "created_at": v[8].isoformat() if v[8] else None,
            }
            for v in versions
        ],
    }


def load_visualization_spec_version(
    conn, *, org_id: str, project_id: str, visualization_spec_version_id: str
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, visualization_id, version_number, query_spec_id, query_spec_version_id,
                   spec_contract_version, schema_version, family, spec, content_hash,
                   predecessor_version_id, proposed_by, created_by, created_at
            FROM app.visualization_spec_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (visualization_spec_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise VisualizationNotFound("visualization spec version not found in this Project")
    return {
        "id": row[0],
        "visualization_id": row[1],
        "version_number": row[2],
        "query_spec_id": row[3],
        "query_spec_version_id": row[4],
        "spec_contract_version": row[5],
        "schema_version": row[6],
        "family": row[7],
        "spec": row[8],
        "content_hash": row[9],
        "predecessor_version_id": row[10],
        "proposed_by": row[11],
        "created_by": row[12],
        "created_at": row[13].isoformat() if row[13] else None,
    }
