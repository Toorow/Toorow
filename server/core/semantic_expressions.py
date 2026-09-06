"""The restricted semantic expression contract (Story 49.3, AC2 and AC3).

A formula is a typed expression TREE, never a string of SQL. Three properties
this module exists to guarantee, in this order:

1. **Nothing arbitrary executes.** Every node's ``op`` is drawn from one
   allowlist, versioned by :data:`EXPRESSION_CONTRACT_VERSION`. A browser, a
   connector manifest or an MCP client that submits ``{"op": "raw_sql"}`` — or
   any op minted after this contract — is refused at parse time, before a
   compiler ever sees it. There is no escape hatch, deliberately.
2. **Every reference is exact.** A leaf names a Concept *and* the Concept
   version it means. ``concept_name`` exists only as the UNRESOLVED shape the
   142 migration wrote for a ratio whose operands were carried by name; it can
   be parsed and displayed but never published.
3. **Types and units are inferred, never assumed.** ``clicks + revenue`` is not
   a number: adding a count to money is refused with the reason, not silently
   summed. Money in two currencies, durations in two units and a ratio whose
   denominator can be zero are all separate, named refusals.

The result of :func:`validate_expression` is an :class:`ExpressionAnalysis`, not
a boolean: the caller needs the inferred type, the unit, the exact dependency
list and the refusals to store or to show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Bumping this is a breaking change to every stored formula. It is recorded on
#: every compiled artifact so an artifact can never be silently reinterpreted
#: under a contract it was not written against.
EXPRESSION_CONTRACT_VERSION = "semantic-expression.v1"

#: Hard ceilings. A formula deeper or wider than this is refused rather than
#: walked: the parser runs on untrusted input on a request path.
MAX_EXPRESSION_DEPTH = 12
MAX_EXPRESSION_NODES = 200

VALUE_TYPES = frozenset(
    {
        "integer",
        "decimal",
        "money",
        "ratio",
        "percent",
        "duration",
        "string",
        "date",
        "timestamp",
        "boolean",
    }
)

#: Numeric families that may combine. `money` is deliberately NOT in the same
#: family as `integer`: a count of clicks and an amount of money are both
#: "numbers" and adding them is still meaningless.
_NUMERIC = frozenset({"integer", "decimal"})
_MONEY = frozenset({"money"})
_RATIOISH = frozenset({"ratio", "percent"})
_DURATION = frozenset({"duration"})

AGGREGATION_FUNCTIONS = frozenset(
    {"sum", "average", "min", "max", "count", "count_distinct", "median"}
)

#: Aggregations that stay correct when rolled up across ANY dimension. `average`
#: and `median` are not among them: averaging an average is not the average.
_ADDITIVE_AGGREGATIONS = frozenset({"sum", "count", "min", "max"})

ADDITIVITY_CLASSES = frozenset({"additive", "semi_additive", "non_additive"})

ZERO_DENOMINATOR_POLICIES = frozenset({"null", "zero", "error"})


class ExpressionError(ValueError):
    """The expression is not expressible in this contract at all."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """A named, actionable reason. Never a generic "invalid formula"."""

    code: str
    message: str
    path: str = "$"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass(frozen=True, slots=True)
class ConceptRef:
    """An EXACT reference: both ids, always."""

    concept_id: str
    version_id: str
    role: str

    def as_dict(self) -> dict[str, str]:
        return {
            "concept_id": self.concept_id,
            "version_id": self.version_id,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class TypedResult:
    value_type: str
    unit: str | None = None
    currency: str | None = None


@dataclass(slots=True)
class ExpressionAnalysis:
    """What the caller stores and shows. `ok` is derived, never set."""

    result: TypedResult | None = None
    dependencies: list[ConceptRef] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    node_count: int = 0
    unresolved_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals and self.result is not None

    @property
    def publishable(self) -> bool:
        """Parsing is not permission to publish: an unresolved `concept_name`
        parses fine and still may never reach a published version."""
        return self.ok and not self.unresolved_names

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": EXPRESSION_CONTRACT_VERSION,
            "ok": self.ok,
            "publishable": self.publishable,
            "result": (
                {
                    "value_type": self.result.value_type,
                    "unit": self.result.unit,
                    "currency": self.result.currency,
                }
                if self.result
                else None
            ),
            "dependencies": [ref.as_dict() for ref in self.dependencies],
            "refusals": [refusal.as_dict() for refusal in self.refusals],
            "unresolved_names": list(self.unresolved_names),
            "node_count": self.node_count,
        }


# ---------------------------------------------------------------------------
# Resolver contract
# ---------------------------------------------------------------------------


class ConceptResolver:
    """Resolves an exact Concept version to its declared type and unit.

    The only way a formula reaches the outside world. It never resolves a name
    to "whatever is current": the caller passes both ids and gets that exact
    version or nothing.
    """

    def __init__(self, versions: Mapping[tuple[str, str], Mapping[str, Any]]):
        self._versions = dict(versions)

    def resolve(self, concept_id: str, version_id: str) -> Mapping[str, Any] | None:
        return self._versions.get((concept_id, version_id))


# ---------------------------------------------------------------------------
# Parsing and type inference
# ---------------------------------------------------------------------------


def _numeric_family(value_type: str) -> frozenset[str] | None:
    for family in (_NUMERIC, _MONEY, _RATIOISH, _DURATION):
        if value_type in family:
            return family
    return None


class _Walker:
    def __init__(
        self,
        resolver: ConceptResolver,
        *,
        owning_concept_name: str | None,
        owning_value_type: str | None,
    ):
        self._resolver = resolver
        self._owning_concept_name = owning_concept_name
        self._owning_value_type = owning_value_type
        self.analysis = ExpressionAnalysis()
        self._seen_dependencies: set[tuple[str, str, str]] = set()

    def refuse(self, code: str, message: str, path: str) -> None:
        self.analysis.refusals.append(Refusal(code, message, path))

    def add_dependency(self, ref: ConceptRef) -> None:
        key = (ref.concept_id, ref.version_id, ref.role)
        if key in self._seen_dependencies:
            return
        self._seen_dependencies.add(key)
        self.analysis.dependencies.append(ref)

    def walk(self, node: Any, path: str, depth: int, role: str) -> TypedResult | None:
        self.analysis.node_count += 1
        if depth > MAX_EXPRESSION_DEPTH:
            self.refuse(
                "expression_too_deep",
                f"This formula nests more than {MAX_EXPRESSION_DEPTH} levels. "
                "Extract the inner part into its own Concept.",
                path,
            )
            return None
        if self.analysis.node_count > MAX_EXPRESSION_NODES:
            self.refuse(
                "expression_too_large",
                f"This formula has more than {MAX_EXPRESSION_NODES} nodes.",
                path,
            )
            return None
        if not isinstance(node, Mapping):
            self.refuse(
                "malformed_node",
                "Every formula node is an object carrying an allowlisted `op`.",
                path,
            )
            return None
        op = node.get("op")
        handler = _HANDLERS.get(op) if isinstance(op, str) else None
        if handler is None:
            self.refuse(
                "unknown_operation",
                f"{op!r} is not an operation of {EXPRESSION_CONTRACT_VERSION}. "
                "Executable SQL and connector-provided expressions are not "
                "accepted here; they enter through validation instead.",
                path,
            )
            return None
        return handler(self, node, path, depth, role)


# --- leaves ----------------------------------------------------------------


def _op_source_measure(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    """This Concept's own mapped physical measure. It has no dependency: the
    binding is owned by Data and projected by Mapping Coverage."""
    concept = node.get("concept")
    if not isinstance(concept, str) or not concept:
        walker.refuse("malformed_node", "`source_measure` names its own Concept.", path)
        return None
    if walker._owning_concept_name and concept != walker._owning_concept_name:
        walker.refuse(
            "foreign_source_measure",
            f"`source_measure` may only name its own Concept. {concept!r} is a "
            "different Concept; reference it with `concept_ref` and an exact version.",
            path,
        )
        return None
    return TypedResult(walker._owning_value_type or "decimal")


def _op_concept_ref(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    concept_id = node.get("concept_id")
    version_id = node.get("version_id")
    if not isinstance(concept_id, str) or not isinstance(version_id, str):
        walker.refuse(
            "malformed_node",
            "`concept_ref` carries both `concept_id` and `version_id`. A reference "
            "to a Concept without a version follows `latest` and is not a reference.",
            path,
        )
        return None
    resolved = walker._resolver.resolve(concept_id, version_id)
    if resolved is None:
        walker.refuse(
            "unknown_reference",
            f"Concept version {version_id} of {concept_id} is not readable in this "
            "Project. It may not exist, or it may belong elsewhere.",
            path,
        )
        return None
    walker.add_dependency(ConceptRef(concept_id, version_id, role))
    value_type = str(resolved.get("value_type") or "decimal")
    return TypedResult(
        value_type,
        unit=resolved.get("unit"),
        currency=(resolved.get("currency_behavior") or {}).get("scope"),
    )


def _op_concept_name(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    """The unresolved shape migration 142 wrote for a ratio operand carried by
    name. It parses so the workbench can SHOW the pending work; it can never be
    published, and :attr:`ExpressionAnalysis.publishable` is what enforces that."""
    name = node.get("name")
    if not isinstance(name, str) or not name:
        walker.refuse("malformed_node", "`concept_name` carries a name.", path)
        return None
    walker.analysis.unresolved_names.append(name)
    return TypedResult("decimal")


def _op_literal(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    value_type = node.get("value_type")
    value = node.get("value")
    if value_type not in VALUE_TYPES:
        walker.refuse(
            "malformed_node", "`literal` declares an allowlisted `value_type`.", path
        )
        return None
    if not isinstance(value, (int, float, str, bool)):
        walker.refuse("malformed_node", "`literal` carries a scalar value.", path)
        return None
    return TypedResult(str(value_type), unit=node.get("unit"))


# --- arithmetic ------------------------------------------------------------


def _combine_additive(
    walker: _Walker, parts: list[TypedResult], path: str, verb: str
) -> TypedResult | None:
    head = parts[0]
    family = _numeric_family(head.value_type)
    if family is None:
        walker.refuse(
            "non_numeric_operand",
            f"{head.value_type} cannot be {verb}.",
            path,
        )
        return None
    for other in parts[1:]:
        if _numeric_family(other.value_type) is not family:
            walker.refuse(
                "incompatible_types",
                f"{head.value_type} and {other.value_type} are different kinds of "
                f"quantity and cannot be {verb}. Equal storage does not make them "
                "the same measure.",
                path,
            )
            return None
        if family is _MONEY and head.currency != other.currency:
            walker.refuse(
                "incompatible_currency",
                f"Money in {head.currency or 'an unstated currency'} cannot be {verb} "
                f"money in {other.currency or 'an unstated currency'}. Convert at read "
                "with an as-of rate first.",
                path,
            )
            return None
        if family is _DURATION and head.unit != other.unit:
            walker.refuse(
                "incompatible_unit",
                f"Durations in {head.unit!r} and {other.unit!r} cannot be {verb}.",
                path,
            )
            return None
        if family is _RATIOISH:
            walker.refuse(
                "ratio_arithmetic",
                "Ratios and percentages cannot be added: the sum of two rates is not "
                "a rate. Combine their numerators and denominators instead.",
                path,
            )
            return None
    return TypedResult(head.value_type, unit=head.unit, currency=head.currency)


def _operands(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> list[TypedResult] | None:
    raw = node.get("operands")
    if not isinstance(raw, list) or len(raw) < 2:
        walker.refuse("malformed_node", "This operation takes at least two operands.", path)
        return None
    parts: list[TypedResult] = []
    for index, child in enumerate(raw):
        resolved = walker.walk(child, f"{path}.operands[{index}]", depth + 1, role)
        if resolved is None:
            return None
        parts.append(resolved)
    return parts


def _op_add(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    parts = _operands(walker, node, path, depth, role)
    return None if parts is None else _combine_additive(walker, parts, path, "added")


def _op_subtract(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    parts = _operands(walker, node, path, depth, role)
    return None if parts is None else _combine_additive(walker, parts, path, "subtracted")


def _op_multiply(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    """Multiplication is allowed only by a dimensionless factor. Money times
    money is an area, not an amount."""
    parts = _operands(walker, node, path, depth, role)
    if parts is None:
        return None
    dimensioned = [p for p in parts if _numeric_family(p.value_type) in (_MONEY, _DURATION)]
    if len(dimensioned) > 1:
        walker.refuse(
            "dimensioned_product",
            "Two dimensioned quantities cannot be multiplied together; the result has "
            "no unit anyone can name. Multiply by a dimensionless factor instead.",
            path,
        )
        return None
    if dimensioned:
        carrier = dimensioned[0]
        return TypedResult(carrier.value_type, unit=carrier.unit, currency=carrier.currency)
    for part in parts:
        if _numeric_family(part.value_type) is None:
            walker.refuse(
                "non_numeric_operand", f"{part.value_type} cannot be multiplied.", path
            )
            return None
    return TypedResult("decimal")


def _op_ratio(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    numerator = node.get("numerator")
    denominator = node.get("denominator")
    if numerator is None or denominator is None:
        walker.refuse(
            "malformed_node", "`ratio` carries a `numerator` and a `denominator`.", path
        )
        return None
    # Deliberately no default. A ratio that does not say what a zero denominator
    # means gets one chosen for it, and the chooser is never the person who has
    # to explain the number.
    policy = node.get("zero_denominator")
    if policy not in ZERO_DENOMINATOR_POLICIES:
        walker.refuse(
            "undeclared_zero_behavior",
            "A ratio declares what it means when its denominator is zero: one of "
            f"{sorted(ZERO_DENOMINATOR_POLICIES)}. Leaving it implicit makes an empty "
            "day and a zero rate indistinguishable.",
            path,
        )
        return None
    top = walker.walk(numerator, f"{path}.numerator", depth + 1, "numerator")
    bottom = walker.walk(denominator, f"{path}.denominator", depth + 1, "denominator")
    if top is None or bottom is None:
        return None
    for part, label in ((top, "numerator"), (bottom, "denominator")):
        if _numeric_family(part.value_type) is None:
            walker.refuse(
                "non_numeric_operand",
                f"The {label} of a ratio is a quantity, not {part.value_type}.",
                path,
            )
            return None
    same_family = _numeric_family(top.value_type) is _numeric_family(bottom.value_type)
    if same_family and _numeric_family(top.value_type) is _MONEY:
        if top.currency != bottom.currency:
            walker.refuse(
                "incompatible_currency",
                "A ratio of money in two different currencies is not a number.",
                path,
            )
            return None
    return TypedResult("percent" if node.get("as_percent") else "ratio")


def _op_conditional(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    branches = node.get("when")
    otherwise = node.get("otherwise")
    if not isinstance(branches, list) or not branches:
        walker.refuse("malformed_node", "`conditional` carries at least one `when` branch.", path)
        return None
    if otherwise is None:
        walker.refuse(
            "undeclared_default_branch",
            "A conditional declares its `otherwise` branch. An implicit NULL makes "
            "'no rule matched' and 'the value is unknown' indistinguishable.",
            path,
        )
        return None
    results: list[TypedResult] = []
    for index, branch in enumerate(branches):
        if not isinstance(branch, Mapping) or "condition" not in branch or "then" not in branch:
            walker.refuse(
                "malformed_node",
                "Each branch carries a `condition` and a `then`.",
                f"{path}.when[{index}]",
            )
            return None
        condition = walker.walk(
            branch["condition"], f"{path}.when[{index}].condition", depth + 1, "filter"
        )
        if condition is None:
            return None
        if condition.value_type != "boolean":
            walker.refuse(
                "non_boolean_condition",
                f"A branch condition is a boolean, not {condition.value_type}.",
                f"{path}.when[{index}].condition",
            )
            return None
        value = walker.walk(branch["then"], f"{path}.when[{index}].then", depth + 1, role)
        if value is None:
            return None
        results.append(value)
    fallback = walker.walk(otherwise, f"{path}.otherwise", depth + 1, role)
    if fallback is None:
        return None
    results.append(fallback)
    head = results[0]
    for other in results[1:]:
        if other.value_type != head.value_type or other.currency != head.currency:
            walker.refuse(
                "incompatible_branch_types",
                "Every branch of a conditional returns the same kind of quantity; "
                f"this one mixes {head.value_type} and {other.value_type}.",
                path,
            )
            return None
    return head


def _op_comparison(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    operator = node.get("operator")
    if operator not in {"eq", "ne", "lt", "lte", "gt", "gte"}:
        walker.refuse("malformed_node", "`comparison` uses an allowlisted operator.", path)
        return None
    left = walker.walk(node.get("left"), f"{path}.left", depth + 1, "filter")
    right = walker.walk(node.get("right"), f"{path}.right", depth + 1, "filter")
    if left is None or right is None:
        return None
    if _numeric_family(left.value_type) is not _numeric_family(right.value_type):
        walker.refuse(
            "incompatible_types",
            f"{left.value_type} and {right.value_type} are not comparable.",
            path,
        )
        return None
    if left.currency != right.currency and _numeric_family(left.value_type) is _MONEY:
        walker.refuse(
            "incompatible_currency",
            "Money in two currencies is not comparable without an as-of conversion.",
            path,
        )
        return None
    return TypedResult("boolean")


def _op_aggregate(
    walker: _Walker, node: Mapping[str, Any], path: str, depth: int, role: str
) -> TypedResult | None:
    function = node.get("function")
    if function not in AGGREGATION_FUNCTIONS:
        walker.refuse(
            "unknown_aggregation",
            f"{function!r} is not an allowlisted aggregation.",
            path,
        )
        return None
    operand = walker.walk(node.get("operand"), f"{path}.operand", depth + 1, role)
    if operand is None:
        return None
    if function in {"count", "count_distinct"}:
        return TypedResult("integer")
    if _numeric_family(operand.value_type) is None:
        walker.refuse(
            "non_numeric_operand",
            f"{function} needs a quantity, not {operand.value_type}.",
            path,
        )
        return None
    if function == "sum" and operand.value_type in _RATIOISH:
        walker.refuse(
            "unsafe_sum",
            "Summing a ratio adds rates together, which is never the combined rate. "
            "Sum the numerator and the denominator and divide once.",
            path,
        )
        return None
    return TypedResult(operand.value_type, unit=operand.unit, currency=operand.currency)


_HANDLERS = {
    "source_measure": _op_source_measure,
    "concept_ref": _op_concept_ref,
    "concept_name": _op_concept_name,
    "literal": _op_literal,
    "add": _op_add,
    "subtract": _op_subtract,
    "multiply": _op_multiply,
    "ratio": _op_ratio,
    "conditional": _op_conditional,
    "comparison": _op_comparison,
    "aggregate": _op_aggregate,
}

#: The public allowlist. Exported so the workbench builds its palette from the
#: SAME list the server enforces, instead of a hand-kept copy that drifts.
ALLOWED_OPERATIONS: tuple[str, ...] = tuple(sorted(_HANDLERS))


def validate_expression(
    expression: Any,
    resolver: ConceptResolver,
    *,
    owning_concept_name: str | None = None,
    owning_value_type: str | None = None,
) -> ExpressionAnalysis:
    """Parse, type-check and collect the exact dependencies of one formula."""
    walker = _Walker(
        resolver,
        owning_concept_name=owning_concept_name,
        owning_value_type=owning_value_type,
    )
    walker.analysis.result = walker.walk(expression, "$", 0, "operand")
    if walker.analysis.refusals:
        walker.analysis.result = None
    return walker.analysis


# ---------------------------------------------------------------------------
# Aggregation and additivity
# ---------------------------------------------------------------------------


def validate_aggregation(
    aggregation: Any,
    *,
    additivity_class: str | None,
    non_additive_dimensions: list[str] | None,
    value_type: str,
) -> list[Refusal]:
    """A measure that declares neither an aggregation nor non-additivity is a
    measure someone will sum by accident."""
    refusals: list[Refusal] = []
    if additivity_class is not None and additivity_class not in ADDITIVITY_CLASSES:
        refusals.append(
            Refusal(
                "unknown_additivity_class",
                f"{additivity_class!r} is not one of {sorted(ADDITIVITY_CLASSES)}.",
                "$.additivity_class",
            )
        )
    if aggregation is None:
        if additivity_class != "non_additive":
            refusals.append(
                Refusal(
                    "undeclared_aggregation",
                    "A metric declares how it aggregates, or declares itself "
                    "non-additive. Neither was declared, so nothing can safely roll it up.",
                    "$.aggregation",
                )
            )
        return refusals
    if not isinstance(aggregation, Mapping):
        return [Refusal("malformed_aggregation", "`aggregation` is an object.", "$.aggregation")]
    # Story 60.2. The half this function used to let through: an aggregation was
    # present, so it never asked for the ADDITIVITY class at all. The two answer
    # different questions -- `sum` says how to combine rows at one grain,
    # `additive` says across WHICH dimensions combining stays true -- and only the
    # second is what a render reads before adding two days together.
    #
    # It stopped being tolerable the moment migration 237 made the column NOT NULL
    # for a metric. Without this refusal the sequence was: `prepare` answers
    # `publishable: true`, `confirm` hits the CHECK, psycopg raises
    # `CheckViolation`, and `semantic_model_api.py:131-138` turns any unexpected
    # exception into **503 `semantic_model_unavailable`**. "The service is
    # unavailable" is the wrong sentence for "your metric does not say whether it
    # may be summed", and it is wrong in the direction that sends someone to look
    # at the infrastructure. The constraint stays, as the net UNDER the
    # application -- never as the thing a person hears from.
    #
    # `undeclared_aggregation` and not a new code: the fact is the one that code
    # already names -- the roll-up declaration is incomplete, so nothing can safely
    # roll this metric up. The `path` says which half is missing, which is what a
    # form needs in order to point at a field.
    if additivity_class is None:
        refusals.append(
            Refusal(
                "undeclared_aggregation",
                f"This metric declares {aggregation.get('function')!r} but not its "
                "additivity class. How to combine rows at one grain and across which "
                "dimensions combining stays true are two different answers, and the "
                "second is the one a roll-up reads. Declare one of "
                f"{sorted(ADDITIVITY_CLASSES)}.",
                "$.additivity_class",
            )
        )
    function = aggregation.get("function")
    if function not in AGGREGATION_FUNCTIONS:
        refusals.append(
            Refusal(
                "unknown_aggregation",
                f"{function!r} is not an allowlisted aggregation.",
                "$.aggregation.function",
            )
        )
        return refusals
    if function == "sum" and value_type in _RATIOISH:
        refusals.append(
            Refusal(
                "unsafe_sum",
                "A ratio declared with `sum` will be silently wrong at every grain "
                "above the one it was computed at.",
                "$.aggregation.function",
            )
        )
    if additivity_class == "additive" and function not in _ADDITIVE_AGGREGATIONS:
        refusals.append(
            Refusal(
                "additivity_contradiction",
                f"{function} is declared `additive`, but rolling up a {function} of "
                f"{function}s does not reproduce it.",
                "$.additivity_class",
            )
        )
    if additivity_class == "semi_additive" and not (non_additive_dimensions or []):
        refusals.append(
            Refusal(
                "undeclared_non_additive_dimensions",
                "A semi-additive metric names the dimensions it may NOT be summed "
                "across. Without them, 'semi-additive' warns nobody.",
                "$.non_additive_dimensions",
            )
        )
    if additivity_class == "additive" and (non_additive_dimensions or []):
        refusals.append(
            Refusal(
                "additivity_contradiction",
                "A metric cannot be fully additive and also name dimensions it must "
                "not be summed across.",
                "$.non_additive_dimensions",
            )
        )
    return refusals


# ---------------------------------------------------------------------------
# The dependency DAG
# ---------------------------------------------------------------------------


def detect_cycle(
    root_version_id: str,
    edges: Mapping[str, list[str]],
) -> list[str] | None:
    """Return the exact cycle path, or None. The path is the message: telling
    someone "there is a cycle" without naming it leaves them to find it."""
    seen: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in on_stack:
            start = stack.index(node)
            return [*stack[start:], node]
        if node in seen:
            return None
        seen.add(node)
        stack.append(node)
        on_stack.add(node)
        for child in edges.get(node, ()):
            found = visit(child)
            if found is not None:
                return found
        stack.pop()
        on_stack.discard(node)
        return None

    return visit(root_version_id)


def topological_order(edges: Mapping[str, list[str]]) -> list[str] | None:
    """Evaluation order, or None when the graph is not a DAG."""
    in_degree: dict[str, int] = {node: 0 for node in edges}
    for children in edges.values():
        for child in children:
            in_degree.setdefault(child, 0)
            in_degree[child] += 1
    ready = sorted(node for node, degree in in_degree.items() if degree == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in edges.get(node, ()):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
                ready.sort()
    return order if len(order) == len(in_degree) else None
