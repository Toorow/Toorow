"""Story 50.6 -- the server-side budget on everything the model can see.

WHY THIS IS A MODULE AND NOT A RULE IN A DOCUMENT.
`docs/product-architecture/visualization-and-rendering.md:344` records the
downstream conflict this closes: *"Current server tools and delivery contracts can
expose oversized model-visible payloads"*. The repository already MEASURED that
number and threw it away -- `server/core/main.py` computes
`payload_bytes = len(json.dumps(envelope).encode("utf-8"))` at three tool return
sites and passes it to `log_tool_metrics`. The size was logged and never enforced.
This module turns the same measurement into a gate that a tool cannot opt out of.

TWO OPERATIONS, AND THE DIFFERENCE MATTERS.

  * `partition_envelope` is a CHANNEL SPLIT, not a truncation. Nothing is
    discarded: a row-shaped array leaves `structuredContent` and arrives whole in
    the app channel (`_meta`), and what stays behind is a stated descriptor
    carrying the exact count and the columns. A reader of the model-visible
    payload can always tell that data was routed elsewhere and how much of it
    there was. Silence is what AD-1 forbids -- a trimmed list that still reads as
    a complete answer -- and a descriptor is the opposite of silence.

  * `enforce_model_channel` REFUSES. When the non-movable core of a payload is
    still over budget after the split, the tool raises `payload_over_budget`
    naming itself, the measured byte count and the budget. It never quietly ships
    a smaller answer, because a smaller answer that looks complete is the failure
    mode the budget exists to prevent.

THE CONSTANTS ARE DERIVED, NOT INVENTED. Each carries the repository precedent it
comes from. Deliberately NOT reused: `MAX_INLINE_ROWS = 1_000`
(`server/core/query_execution.py`). That is the bound on what a Result STORES,
not on what one host message can carry; reusing it would put a storage ceiling
into a transport budget and guarantee an over-budget message on the first wide
Result.

ASCII-only, English copy, no dependency beyond the standard library so that
`core.main` can import it without dragging in an MCP adapter (D6).
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Budgets.
# ---------------------------------------------------------------------------

#: Serialized ceiling for `structuredContent`, the model-visible channel.
#: Derived from the bounded payloads this repository already ships: the 30-line
#: summary cap in `core.main` and `_MAX_MATRIX_ROWS = 60`
#: (`core/project_capabilities_mcp.py`) both target a payload a model can read in
#: full. 4 KiB is that order of magnitude expressed in bytes rather than rows, so
#: a wide row cannot smuggle a dataset past a row count.
MODEL_CHANNEL_MAX_BYTES = 4096

#: Line ceiling on the text channel. `core.main` already caps its tool summaries
#: at 30 lines (`adherence.append_pointer_within_cap` preserves exactly that cap);
#: this makes the same number checkable from outside the tool that produces it.
MODEL_CHANNEL_MAX_TEXT_LINES = 30

#: How many figures a bounded-evidence list may show before the remainder is
#: stated rather than sent. Derived from `_MAX_BOUNDED_FIGURES = 6`
#: (`core/first_report_render.py`), doubled because a Result summary carries both
#: metrics and dimensions where a starter report carried figures alone.
MAX_BOUNDED_EVIDENCE_FIGURES = 12

#: The single `_meta` key under which a legacy card/report tool's app-channel
#: payload travels. It is deliberately NOT `toorow.result`: that key belongs to
#: `core.result_slices.build_result_meta` alone (AC5), and a second builder
#: writing it would destroy the "exactly one construction site" property the
#: secret-shape tripwire depends on for its meaning.
APP_PAYLOAD_META_KEY = "toorow.app_payload"

#: Marker placed where a value was routed to the app channel. A model reading
#: `structuredContent` meets this dict instead of the data, and it says so.
WITHHELD_MARKER = "moved_to_app_channel"


class ModelChannelOverBudget(Exception):
    """A tool tried to put more in front of the model than the budget allows.

    Carries the tool name and BOTH numbers, because "too big" without the
    measurement is not actionable -- and a story about payload size that cannot
    quote a byte count has proved nothing.
    """

    code = "payload_over_budget"

    def __init__(self, tool_name: str, measured: int, budget: int, *, unit: str = "bytes"):
        self.tool_name = tool_name
        self.measured = measured
        self.budget = budget
        self.unit = unit
        super().__init__(
            f"{tool_name}: model-visible payload is {measured} {unit}, "
            f"over the {budget} {unit} budget"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "tool": self.tool_name,
            "measured": self.measured,
            "budget": self.budget,
            "unit": self.unit,
        }


class ModelChannelEvidenceMissing(Exception):
    """A deep link was offered instead of evidence, not alongside it.

    `visualization-and-rendering.md:250-251` and AD-1 both require that a host
    without any UI still receives bounded evidence. A payload that carries a
    `deep_link` and an empty `evidence` is exactly the deep-link-only answer the
    compliant reference tool refuses to produce
    (`core/first_report_render_mcp.py:20-24`).
    """

    code = "evidence_required_with_deep_link"

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(
            f"{tool_name}: a deep link may accompany bounded evidence, never replace it"
        )

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "tool": self.tool_name}


# ---------------------------------------------------------------------------
# Measurement.
# ---------------------------------------------------------------------------


def serialized_bytes(value: Any) -> int:
    """Exact UTF-8 byte length of *value* as JSON -- the number a host carries.

    Same expression the three legacy tool sites already compute, so the gate and
    the metric measure the same thing.
    """
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def text_line_count(content: Any) -> int:
    """Line count of the text channel, whatever shape the tool passes.

    Accepts a plain string or a list of MCP content blocks, so a caller does not
    have to normalize before it can be measured.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.splitlines())
    total = 0
    for block in content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            total += len(text.splitlines())
    return total


def bounded_head(seq: Any, cap: int = MAX_BOUNDED_EVIDENCE_FIGURES) -> tuple[list, int]:
    """Return ``(head, withheld_count)`` -- ALWAYS both.

    The withheld count is the whole point. A caller that only wanted the head
    could slice; what a bounded list owes its reader is the size of what it is
    not showing, so "12 metrics" never reads as "all the metrics".
    """
    if seq is None:
        return [], 0
    items = list(seq)
    if cap < 0:
        cap = 0
    head = items[:cap]
    return head, max(0, len(items) - len(head))


# ---------------------------------------------------------------------------
# The channel split.
# ---------------------------------------------------------------------------


def _is_row_shaped(value: Any) -> bool:
    """True for a list of records -- the shape a warehouse projection takes."""
    return isinstance(value, list) and bool(value) and all(isinstance(x, dict) for x in value)


def _row_descriptor(rows: list[dict]) -> dict[str, Any]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows[:20]:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(str(key))
    head, withheld = bounded_head(columns, MAX_BOUNDED_EVIDENCE_FIGURES)
    return {
        "withheld": WITHHELD_MARKER,
        "row_count": len(rows),
        "columns": head,
        "columns_withheld": withheld,
    }


#: Keys whose value is a warehouse projection and is therefore routed WHOLE, at any
#: size. Every producer this story inventoried spells it the same way: `core.envelope`,
#: `core.reports` and all eight `core.cards` builders write `"rows"` into the envelope
#: `data`. Routing by that name -- rather than by "looks like a list of dicts" -- is
#: what keeps a small, genuinely model-useful catalog (`get_card_capabilities`)
#: model-visible while a two-row warehouse projection still travels in the app
#: channel. A dataset is a dataset at any length; a catalog is an answer.
_DATASET_KEYS = frozenset({"rows"})


def _split_value(
    value: Any,
    app_sink: dict,
    path: str,
    *,
    keep_head: bool = False,
    datasets_only: bool = False,
) -> Any:
    """Recursively route dataset-shaped values into *app_sink*, keyed by *path*.

    Two flags, one per walk, and the difference is the whole design.

    ``datasets_only`` governs the ``data`` walk. Only a value under a
    ``_DATASET_KEYS`` name is routed, and it is routed WHATEVER its size -- that
    is the warehouse projection AD-1 keeps out of the model channel entirely.
    Nothing else moves here, even when it is long.

    That restraint was learned: an earlier version also routed any list of more
    than twelve records, by shape. It relocated `search_context`'s own hits --
    the answer the model was asking for -- into a channel the model cannot read,
    and it would have done the same to every list-shaped ANSWER in the catalog
    the moment the guard became catalog-wide. Length is not what makes a value
    unfit for the model channel; being a warehouse dataset is, and being over
    budget is. The second is handled by `partition_envelope`'s over-budget pass,
    which moves the largest subtree first and states what it moved -- so a value
    that is genuinely too big still leaves, and one that is merely long does not.

    ``keep_head`` governs the ``meta`` walk, where the opposite is right. There a
    routed collection leaves a bounded HEAD behind, because ``provenance`` is
    what a model cites to qualify an answer: at 37 connectors those per-entity
    collections were measured at 5250 bytes -- over the whole budget by
    themselves -- while saying nothing actionable past the first dozen. Twelve
    entries plus a stated remainder is more honest than "37 entries, withheld".
    """
    leaf = path.rsplit(".", 1)[-1]
    if leaf in _DATASET_KEYS and isinstance(value, list):
        app_sink[path] = value
        return _row_descriptor([r for r in value if isinstance(r, dict)])
    if datasets_only:
        if isinstance(value, dict):
            return {
                k: _split_value(v, app_sink, f"{path}.{k}", datasets_only=True)
                for k, v in value.items()
            }
        return value
    if _is_row_shaped(value) and len(value) > MAX_BOUNDED_EVIDENCE_FIGURES and not keep_head:
        app_sink[path] = value
        return _row_descriptor(value)
    if isinstance(value, list) and len(value) > MAX_BOUNDED_EVIDENCE_FIGURES:
        head, withheld = bounded_head(value, MAX_BOUNDED_EVIDENCE_FIGURES)
        app_sink[path] = value
        return {
            "withheld": WITHHELD_MARKER,
            "head": head,
            "item_count": len(value),
            "items_withheld": withheld,
        }
    if isinstance(value, dict):
        # A per-entity mapping (one key per connector, per market, per account) is
        # the same unbounded shape as a list, wearing a dict. Bound it the same
        # way and state the remainder.
        if len(value) > MAX_BOUNDED_EVIDENCE_FIGURES and all(
            not isinstance(v, (dict, list)) for v in value.values()
        ):
            keys = sorted(value)[:MAX_BOUNDED_EVIDENCE_FIGURES]
            app_sink[path] = value
            return {
                "withheld": WITHHELD_MARKER,
                "head": {k: value[k] for k in keys},
                "key_count": len(value),
                "keys_withheld": len(value) - len(keys),
            }
        return {
            k: _split_value(v, app_sink, f"{path}.{k}", keep_head=keep_head)
            for k, v in value.items()
        }
    return value


def partition_envelope(
    envelope: dict[str, Any], *, tool_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split one AD-1 envelope into ``(model_visible, app_payload)``.

    ``envelope["data"]`` and ``envelope["meta"]`` are both walked, but with
    different intent.

    In ``data``, a dataset -- and ONLY a dataset -- is routed whole; anything else
    that is too heavy is moved by the over-budget pass below, not by its shape.
    In ``meta`` -- freshness, provenance,
    alerts, branding -- the honesty fields STAY model-visible: those are what a
    model must be able to cite when it qualifies an answer, and hiding them to
    save a few hundred bytes would make every summary less honest. Only the
    per-entity collections there are bounded, because ``provenance`` is one entry
    per contributing connector and ``freshness.per_connector`` one key per
    connector: at 37 connectors those two alone were measured at 5250 bytes, past
    the whole budget, while saying nothing a model could act on beyond the first
    dozen. They get a bounded head with a stated remainder; the full lists travel
    in the app channel.

    After the walk, if the payload is still over budget, the largest remaining
    ``data`` subtrees are routed to the app channel one at a time, largest first,
    each replaced by a stated descriptor. That order is deterministic (size, then
    key name) so the same envelope always splits the same way. If nothing movable
    is left and the payload is still over budget, this returns anyway -- refusing
    is `enforce_model_channel`'s job, and separating the two keeps the split
    inspectable in a test that does not have to trigger an exception.
    """
    if not isinstance(envelope, dict):
        return envelope, {}
    app_payload: dict[str, Any] = {}
    model: dict[str, Any] = dict(envelope)
    data = envelope.get("data")
    if isinstance(data, dict):
        model["data"] = {
            k: _split_value(v, app_payload, k, datasets_only=True) for k, v in data.items()
        }
    meta = envelope.get("meta")
    if isinstance(meta, dict):
        model["meta"] = {
            k: _split_value(v, app_payload, f"meta.{k}", keep_head=True)
            for k, v in meta.items()
        }

    # Second pass: whatever survived the shape rules but is still too heavy.
    #
    # The container is `data` for an AD-1 envelope and the payload ITSELF for any
    # other shape. That generalization is what lets the one shared guard offer a
    # bounded path to every tool rather than only to the four that happen to build
    # an AD-1 envelope: a tool returning a bare dict used to have no recourse but a
    # hard refusal, which is how a payload ends up escaping the guard instead of
    # going through it.
    while serialized_bytes(model) > MODEL_CHANNEL_MAX_BYTES:
        container = model.get("data")
        top_level = not isinstance(container, dict)
        if top_level:
            container = model
        movable = [
            (serialized_bytes(v), k)
            for k, v in container.items()
            if k not in app_payload
            and k not in _NEVER_MOVED
            and not _already_withheld(v)
            # Moving a key smaller than the floor cannot bring a payload under
            # budget; it would only shred a small answer into descriptors. Below
            # the floor there is nothing useful left to move, and refusing is
            # `enforce_model_channel`'s job.
            and serialized_bytes(v) >= _MOVABLE_FLOOR_BYTES
        ]
        if not movable:
            break
        movable.sort(key=lambda pair: (-pair[0], pair[1]))
        size, key = movable[0]
        app_payload[key] = container[key]
        container = dict(container)
        container[key] = {"withheld": WITHHELD_MARKER, "bytes": size}
        if top_level:
            model = container
        else:
            model["data"] = container
    if app_payload:
        app_payload["__tool__"] = tool_name
    return model, app_payload


#: Keys the second pass never routes to the app channel, whatever the size.
#: `schema_version` is a named contract, not a payload: `ui/shell/src/mcpApp.ts`
#: gates on its presence before it notifies a widget, so moving it would silence
#: every widget. `code`/`message` are the canonical error shape a host reads when a
#: tool refuses; an error whose message travelled to the app channel is not an
#: error a model can act on.
_NEVER_MOVED = frozenset({"schema_version", "code", "message"})

#: Below this, moving a key cannot plausibly bring a payload under budget.
_MOVABLE_FLOOR_BYTES = 256


def _already_withheld(value: Any) -> bool:
    return isinstance(value, dict) and value.get("withheld") == WITHHELD_MARKER


def app_meta(app_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Wrap an app payload under the single namespaced `_meta` key, or None."""
    if not app_payload:
        return None
    return {APP_PAYLOAD_META_KEY: app_payload}


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


def enforce_model_channel(
    tool_name: str,
    content: Any,
    structured_content: Any,
    *,
    require_evidence_with_deep_link: bool = True,
) -> None:
    """Raise unless everything the model can see is inside the budget.

    Three checks, in the order a reader would ask them:

    1. the text channel is at most `MODEL_CHANNEL_MAX_TEXT_LINES` lines;
    2. `structuredContent` serializes to at most `MODEL_CHANNEL_MAX_BYTES`;
    3. a payload carrying a `deep_link` also carries non-empty `evidence`
       (AC9 -- never deep-link-only).

    Raises rather than returning a verdict, so a caller cannot forget to look at
    the answer. That is the difference between this and the `payload_bytes`
    measurement the repository already had.
    """
    lines = text_line_count(content)
    if lines > MODEL_CHANNEL_MAX_TEXT_LINES:
        raise ModelChannelOverBudget(
            tool_name, lines, MODEL_CHANNEL_MAX_TEXT_LINES, unit="text lines"
        )
    measured = serialized_bytes(structured_content)
    if measured > MODEL_CHANNEL_MAX_BYTES:
        raise ModelChannelOverBudget(tool_name, measured, MODEL_CHANNEL_MAX_BYTES)
    if require_evidence_with_deep_link and isinstance(structured_content, dict):
        if structured_content.get("deep_link") and not structured_content.get("evidence"):
            raise ModelChannelEvidenceMissing(tool_name)
