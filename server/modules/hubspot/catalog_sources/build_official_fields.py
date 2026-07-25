"""Deterministic builder for hubspot official_fields.json (Story 25.7).

This script is the AUDITABLE SOURCE for the curated official snapshot. It is
committed alongside its output (official_fields.json) so a reviewer can see
exactly how the curated list was derived from the official HubSpot CRM v3
references recorded in catalog_sources.json.

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

Scope (honest coverage of the module's actual API surface):
  The hubspot module extracts daily CRM aggregates from the CRM v3 *Search* API
  on two objects (POST /crm/v3/objects/contacts/search and .../deals/search),
  filtering on the `createdate` / `closedate` date properties and counting the
  paginated results client-side (HubSpot exposes NO native daily aggregate).

  The catalog therefore curates:
    1. The DEFAULT (standard) CRM properties of the four main CRM objects the
       Search / Properties API exposes: contacts, companies, deals, tickets.
       These are the real, provider-listed fields any Search response can carry.
       Sources: HubSpot CRM Properties API + the per-object "default properties"
       knowledge-base references (see catalog_sources.json).
    2. The MODULE-DERIVED daily metrics the connector computes on top of that
       surface (new_contacts, deals_created, deals_closed, deal_amount) and the
       canonical `date` dimension (mapped to the createdate/closedate props).
       These carry an explicit source_field matching the manifest verbatim so
       the manifest<->catalog gate does not report source_field_mismatch.

field_id uniqueness:
  A HubSpot property internal name (createdate, hubspot_owner_id, hs_object_id,
  city, ...) legitimately exists on several objects. The merge engine keys the
  catalog by a GLOBALLY UNIQUE field_id (duplicates are dropped + reported), so
  each object's property is namespaced `<object>_<property>` (e.g.
  contact_createdate, deal_createdate) while `source_field` preserves the raw
  provider token (createdate). The 5 module-derived fields keep their bare ids
  (new_contacts, deals_created, deals_closed, deal_amount, date) with the exact
  source_field the manifest declares.

Count honesty (see catalog_sources.json._derivation):
  This is intentionally the DEFAULT-property surface, not the full HubSpot
  property universe (accounts define unbounded custom properties, and other
  hubs -- Marketing Email, Analytics events, Workflows, Forms, Engagements --
  are NOT reached by this module's connector.py). Supermetrics' 174 metrics /
  403 dimensions across 45 sections span every hub AND every custom/bucketed
  column; those extra fields are enrichment SUSPECTS, never emitted, because
  the module does not query those surfaces. Emitting them would be dishonest
  drift, not coverage.

Run (orchestrator, local only):
    uv run python server/modules/hubspot/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Sections used by the section_tier_map.
# ---------------------------------------------------------------------------
S_CONTACT = "CONTACT"
S_COMPANY = "COMPANY"
S_DEAL = "DEAL"
S_TICKET = "TICKET"
S_ANALYTICS = "ANALYTICS"
S_TIME = "TIME"

# ---------------------------------------------------------------------------
# 0. MODULE-DERIVED fields (what connector.py actually emits today).
#    These must match the manifest source_capabilities.fields verbatim on
#    field_id, kind and source_field (manifest<->catalog gate). Bare ids.
#    (field_id, kind, data_type, section, description, source_field)
# ---------------------------------------------------------------------------
DERIVED_FIELDS: list[tuple] = [
    (
        "new_contacts", "metric", "integer", S_CONTACT,
        "Count of contacts created within the day (client-side aggregate of "
        "the contacts Search API filtered on createdate BETWEEN).",
        "new_contacts",
    ),
    (
        "deals_created", "metric", "integer", S_DEAL,
        "Count of deals created within the day (client-side aggregate of the "
        "deals Search API filtered on createdate BETWEEN).",
        "deals_created",
    ),
    (
        "deals_closed", "metric", "integer", S_DEAL,
        "Count of deals closed-won within the day (deals Search filtered on "
        "closedate BETWEEN and dealstage = closed-won).",
        "deals_closed",
    ),
    (
        "deal_amount", "metric", "decimal", S_DEAL,
        "Sum of the `amount` property over the day's closed-won deals.",
        "deal_amount",
    ),
    (
        "date", "dimension", "date", S_TIME,
        "Reporting day (derived from the CRM createdate/closedate timestamp "
        "properties, bucketed in the account time zone).",
        "createdate/closedate",
    ),
]

# ---------------------------------------------------------------------------
# Per-object default properties. Each tuple is (property_internal_name,
# data_type, description). kind is set per list (dimension vs metric).
# The emitted field_id is "<prefix>_<property>"; source_field is the raw
# property internal name.
# ---------------------------------------------------------------------------

CONTACT_DIMS: list[tuple] = [
    ("email", "string", "Primary email address of the contact."),
    ("firstname", "string", "First name of the contact."),
    ("lastname", "string", "Last name of the contact."),
    ("phone", "string", "Primary phone number of the contact."),
    ("mobilephone", "string", "Mobile phone number of the contact."),
    ("jobtitle", "string", "Job title of the contact."),
    ("company", "string", "Company name associated with the contact."),
    ("website", "string", "Company website of the contact."),
    ("city", "string", "City of residence of the contact."),
    ("state", "string", "State/region of residence of the contact."),
    ("country", "string", "Country of residence of the contact."),
    ("zip", "string", "Postal code of the contact."),
    ("address", "string", "Street address of the contact."),
    ("salutation", "string", "Title used to address the contact."),
    ("lifecyclestage", "string", "Contact's position in the marketing/sales lifecycle."),
    ("hs_lead_status", "string", "Contact's status within the buying cycle."),
    ("hubspot_owner_id", "string", "ID of the HubSpot owner of the contact."),
    ("hs_object_id", "id", "Unique HubSpot ID of the contact record."),
    ("createdate", "datetime", "Timestamp the contact was created."),
    ("lastmodifieddate", "datetime", "Timestamp of the most recent property update."),
    ("hs_analytics_source", "string", "Original traffic source of the contact."),
    (
        "hs_analytics_last_touch_converting_campaign", "string",
        "Most recent campaign that converted the contact.",
    ),
]
CONTACT_METS: list[tuple] = [
    ("num_conversion_events", "integer", "Total number of form submissions by the contact."),
    ("num_associated_deals", "integer", "Number of deals associated with the contact."),
]

COMPANY_DIMS: list[tuple] = [
    ("name", "string", "Name of the company."),
    ("domain", "string", "Primary domain name of the company."),
    ("website", "string", "Website URL of the company."),
    ("industry", "string", "Industry the company operates in."),
    ("type", "string", "Type/classification of the company."),
    ("description", "string", "Description of the company."),
    ("phone", "string", "Primary phone number of the company."),
    ("city", "string", "City where the company is located."),
    ("state", "string", "State/region where the company is located."),
    ("country", "string", "Country where the company is located."),
    ("zip", "string", "Postal code of the company."),
    ("lifecyclestage", "string", "Company's position in the marketing/sales lifecycle."),
    ("hubspot_owner_id", "string", "ID of the HubSpot owner of the company."),
    ("hs_object_id", "id", "Unique HubSpot ID of the company record."),
    ("createdate", "datetime", "Timestamp the company was created."),
    ("hs_lastmodifieddate", "datetime", "Timestamp of the most recent property update."),
    ("hs_analytics_source", "string", "Original traffic source of the company."),
]
COMPANY_METS: list[tuple] = [
    ("numberofemployees", "integer", "Number of employees at the company."),
    ("annualrevenue", "decimal", "Annual revenue of the company."),
    ("total_revenue", "decimal", "Total revenue from closed-won deals for the company."),
    ("hs_num_open_deals", "integer", "Number of open deals associated with the company."),
    ("num_associated_contacts", "integer", "Number of contacts associated with the company."),
]

DEAL_DIMS: list[tuple] = [
    ("dealname", "string", "Name of the deal."),
    ("dealstage", "string", "Current pipeline stage of the deal."),
    ("pipeline", "string", "Pipeline the deal belongs to."),
    ("dealtype", "string", "Type/classification of the deal."),
    ("description", "string", "Description of the deal."),
    ("closedate", "datetime", "Date the deal is expected to close (or closed)."),
    ("createdate", "datetime", "Timestamp the deal was created."),
    ("hs_lastmodifieddate", "datetime", "Timestamp of the most recent property update."),
    ("hubspot_owner_id", "string", "ID of the HubSpot owner of the deal."),
    ("hs_object_id", "id", "Unique HubSpot ID of the deal record."),
    ("hs_is_closed", "boolean", "Whether the deal is in a closed stage."),
    ("hs_is_closed_won", "boolean", "Whether the deal is in a closed-won stage."),
]
DEAL_METS: list[tuple] = [
    ("amount", "decimal", "Monetary value of the deal."),
    ("amount_in_home_currency", "decimal", "Deal amount converted to the account home currency."),
    ("hs_forecast_amount", "decimal", "Forecast (projected) revenue for the deal."),
    ("hs_deal_stage_probability", "decimal", "Probability of the deal reaching closed-won."),
    ("days_to_close", "integer", "Number of days between deal creation and closing."),
    ("num_associated_contacts", "integer", "Number of contacts associated with the deal."),
]

TICKET_DIMS: list[tuple] = [
    ("subject", "string", "Short summary/name of the ticket."),
    ("content", "string", "Full description of the ticket issue."),
    ("hs_pipeline", "string", "Pipeline the ticket belongs to."),
    ("hs_pipeline_stage", "string", "Pipeline status/stage of the ticket."),
    ("hs_ticket_priority", "string", "Priority level of the ticket."),
    ("hs_ticket_category", "string", "Main reason the customer reached out."),
    ("source_type", "string", "Channel where the ticket was submitted."),
    ("hubspot_owner_id", "string", "ID of the HubSpot owner of the ticket."),
    ("hs_object_id", "id", "Unique HubSpot ID of the ticket record."),
    ("createdate", "datetime", "Timestamp the ticket was created."),
    ("hs_lastmodifieddate", "datetime", "Timestamp of the most recent property update."),
    ("closed_date", "datetime", "Date the ticket was closed."),
    ("hs_resolution", "string", "Action taken to resolve the ticket."),
]
TICKET_METS: list[tuple] = [
    ("time_to_close", "integer", "Elapsed time (ms) from ticket creation to closure."),
    (
        "time_to_first_agent_reply", "integer",
        "Elapsed time (ms) from ticket creation to the first agent reply.",
    ),
    ("num_notes", "integer", "Number of notes/activities logged on the ticket."),
]

# Analytics aggregate metrics the module derives from CRM object dates. Honest,
# module-relevant slice of Supermetrics' ANALYTICS section (bare ids).
ANALYTICS_FIELDS: list[tuple] = [
    ("contacts_created", "metric", "integer", "Total contacts created in the period."),
    ("deals_created_total", "metric", "integer", "Total deals created in the period."),
    ("deals_closed_won", "metric", "integer", "Total deals closed-won in the period."),
    ("closed_won_amount", "metric", "decimal", "Total closed-won deal amount in the period."),
]

# (prefix, section, dim_list, met_list) per object.
OBJECTS: list[tuple] = [
    ("contact", S_CONTACT, CONTACT_DIMS, CONTACT_METS),
    ("company", S_COMPANY, COMPANY_DIMS, COMPANY_METS),
    ("deal", S_DEAL, DEAL_DIMS, DEAL_METS),
    ("ticket", S_TICKET, TICKET_DIMS, TICKET_METS),
]


def _load_sensitive_dimensions() -> frozenset[str]:
    """Read sensitive_dimensions from catalog_sources.json so the flag propagates
    automatically on every rebuild — no manual sync required."""
    catalog_sources_path = Path(__file__).with_name("catalog_sources.json")
    data = json.loads(catalog_sources_path.read_text(encoding="utf-8"))
    return frozenset(data.get("sensitive_dimensions", []))


def build() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    sensitive_ids = _load_sensitive_dimensions()

    # field_ids owned by the module-derived layer. A namespaced object property
    # that would collapse onto one of these (e.g. deal `amount` -> deal_amount)
    # is intentionally NOT re-emitted: the derived field already represents that
    # family and is the manifest-matching authority. Silent-skip, not error.
    derived_ids = {row[0] for row in DERIVED_FIELDS}

    def add(field_id, kind, data_type, section, description, source_field=None):
        if field_id in seen:
            raise ValueError(f"duplicate field_id in builder: {field_id!r}")
        seen.add(field_id)
        entry = {
            "field_id": field_id,
            "kind": kind,
            "data_type": data_type,
            "description": description,
            "section": section,
        }
        if source_field is not None:
            entry["source_field"] = source_field
        if field_id in sensitive_ids:
            entry["sensitive"] = True
        out.append(entry)

    # 0. Module-derived fields (bare ids, manifest-matching source_field).
    for field_id, kind, data_type, section, description, source_field in DERIVED_FIELDS:
        add(field_id, kind, data_type, section, description, source_field)

    # 1-4. Default CRM object properties, namespaced <prefix>_<property>.
    for prefix, section, dim_list, met_list in OBJECTS:
        for prop, data_type, description in dim_list:
            fid = f"{prefix}_{prop}"
            if fid in derived_ids:
                continue  # represented by the module-derived field
            add(fid, "dimension", data_type, section, description, prop)
        for prop, data_type, description in met_list:
            fid = f"{prefix}_{prop}"
            if fid in derived_ids:
                continue  # e.g. deal `amount` -> deal_amount (derived owns it)
            add(fid, "metric", data_type, section, description, prop)

    # 5. Analytics aggregates (bare ids).
    for field_id, kind, data_type, description in ANALYTICS_FIELDS:
        add(field_id, kind, data_type, S_ANALYTICS, description)

    # Deterministic order: sort by field_id.
    out.sort(key=lambda e: e["field_id"])
    return out


def main() -> None:
    fields = build()
    metrics = sum(1 for f in fields if f["kind"] == "metric")
    dims = sum(1 for f in fields if f["kind"] == "dimension")
    out_path = Path(__file__).with_name("official_fields.json")
    out_path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {len(fields)} fields ({metrics} metrics / {dims} dimensions) to {out_path}")


if __name__ == "__main__":
    main()
