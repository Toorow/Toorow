"""Where a Master Data consumer LIVES, and how a reader opens it.

WHY THIS MODULE EXISTS. ``app.master_data_used_by`` deliberately carries no
owner column: migration 140 says so in as many words -- *"enumerating consumers
here would make the platform decide which surfaces are allowed to depend on
Master Data"*. That refusal is right for the STORE, and it left the READ with a
question it could not answer. Story 49.2 AC7 requires every used-by reference to
carry *"owner workspace, stable consumer ID, pinned version when applicable,
evidence time and an exact owner link"*, and a workspace is exactly what a bare
``consumer_kind`` does not say.

SO THE ROUTE IS DERIVED, NEVER STORED, AND IT IS NOT AN ALLOW-LIST. A kind
absent from the tables below is still a consumer: it is reported with
``workspace=None`` and ``owner_href=None``, and the workbench files it under
*Other recorded consumers*. Nothing is refused here, nothing is invented here,
and no writer has to register itself before its dependency can be counted --
which is the property migration 140 protected.

THE RULE FOR A MISSING LINK is the one ``evidence_index._OWNER_ROUTES`` already
states: *"An owner type absent from this table produces NO link at all -- an
unroutable reference is reported as unavailable rather than emitted as a
guess."* A consumer whose kind has a workspace but no addressable screen (an
observed-entity attachment) therefore gets the workspace and no href. Guessing a
route would put a dead address in front of a person about to archive an object.
"""

from __future__ import annotations

from typing import Any

#: The workspace keys of `ui/admin/src/shell/navigation.ts`. Written here so a
#: composed reference can never name a workspace the console does not mount;
#: `test_used_by_workspaces_match_the_console` holds the two lists equal.
WORKSPACE_LABELS: dict[str, str] = {
    "overview": "Overview",
    "analyze": "Analyze",
    "test": "Test",
    "data": "Data",
    "governance": "Governance",
    "context-hub": "Context Hub",
}

#: What the used-by store's own `consumer_kind` means, for the kinds this
#: repository writes today. Tuple is (workspace, section, object type, tab); the
#: last three are ``None`` together when the kind has no screen of its own.
_CONSUMER_ROUTES: dict[str, tuple[str, str | None, str | None, str | None]] = {
    # `core.semantic_model_used_by` -- a published Semantic Model version that
    # names a Business Domain.
    "semantic-concept": ("governance", "semantic-model", "semantic-concept", "used-by"),
    "semantic-view": ("governance", "semantic-model", "semantic-view", "used-by"),
    # `core.observed_entities` -- an attachment has no workbench of its own; the
    # Datastream that produced it is what a reader opens, and the attachment id
    # is not that Datastream. Workspace without a link, deliberately.
    "observed_entity_attachment": ("data", None, None, None),
}

#: Every Rule Set profile key routes to the same workbench, because every one of
#: them IS a Rule Set (`core.governance_rule_sets` registers the dependency under
#: `profile.key`). Listed rather than resolved through `get_profile` so a read
#: never imports five profile modules to render one panel.
_RULE_SET_PROFILE_KINDS = (
    "metric_reconciliation_v1",
    "dq_policy_v1",
    "entity_derivation_v1",
    "money_policy_v1",
    "fx_ingestion_v1",
    "timezone_policy_v1",
    "source_currency_binding_v1",
    "tax_fee_ladder_v1",
)
for _key in _RULE_SET_PROFILE_KINDS:
    _CONSUMER_ROUTES[_key] = ("governance", "controls-quality", "rule-set", "overview")

#: `app.mdm_business_links.target_type` -- the second consumer store, and the
#: only one that answers for a Business Domain or a classification. The nine
#: values are the CHECK constraint of migration 289.
_LINK_TARGET_ROUTES: dict[str, tuple[str, str | None, str | None, str | None]] = {
    "topic": ("context-hub", "knowledge-library", "context-topic", "usage"),
    "procedure": ("context-hub", "skills-registry", "context-procedure", "usage"),
    "datastream": ("data", "datastreams", "datastream", "overview"),
    "report_view": ("analyze", "reports", "report", "overview"),
    "semantic_view": ("governance", "semantic-model", "semantic-view", "used-by"),
    "semantic_concept": ("governance", "semantic-model", "semantic-concept", "used-by"),
    "canonical_field": ("governance", "semantic-model", "canonical-field", "definition"),
    # `app.target_fields` is a Data-owned catalogue with no Level-3 route, and
    # `app.schema_context` has none either. Workspace, no link.
    "target_field": ("data", None, None, None),
    "schema_doc": ("data", None, None, None),
}

#: What a Project association IS: the Project reusing an organization identity.
#: It is read on the Project's own settings, which is the Overview workspace's
#: configuration section -- a real screen, but not an object route.
_ASSOCIATION_WORKSPACE = "overview"


def consumer_workspace(consumer_kind: str) -> str | None:
    """The workspace a consumer kind lives in, or None when it is not known.

    None is an answer: the reference is real, and the reader is told that its
    owner could not be named rather than being shown an invented one.
    """

    route = _CONSUMER_ROUTES.get(str(consumer_kind or "").strip())
    return None if route is None else route[0]


def link_target_workspace(target_type: str) -> str | None:
    route = _LINK_TARGET_ROUTES.get(str(target_type or "").strip())
    return None if route is None else route[0]


def _href(
    route: tuple[str, str | None, str | None, str | None] | None,
    object_id: str,
    version_id: str | None,
) -> dict[str, Any] | None:
    if route is None or not object_id:
        return None
    workspace, section, object_type, tab = route
    if not section or not object_type:
        return None
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    return owner_reference(
        workspace,
        section,
        object_type=object_type,
        object_id=object_id,
        tab=tab,
        version_id=version_id,
    )


def consumer_owner_href(
    consumer_kind: str, consumer_id: str, version_id: str | None = None
) -> dict[str, Any] | None:
    """An exact semantic owner reference, or None when none can be proven."""

    return _href(_CONSUMER_ROUTES.get(str(consumer_kind or "").strip()), consumer_id, version_id)


def link_target_owner_href(target_type: str, target_id: str) -> dict[str, Any] | None:
    return _href(_LINK_TARGET_ROUTES.get(str(target_type or "").strip()), target_id, None)


def workspace_label(workspace: str | None) -> str | None:
    return None if workspace is None else WORKSPACE_LABELS.get(workspace)


__all__ = [
    "WORKSPACE_LABELS",
    "consumer_owner_href",
    "consumer_workspace",
    "link_target_owner_href",
    "link_target_workspace",
    "workspace_label",
    "_ASSOCIATION_WORKSPACE",
]
