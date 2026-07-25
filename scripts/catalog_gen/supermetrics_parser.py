"""Parse a Supermetrics fields markdown snapshot into enrichment entries.

Source structure (verified 2026-07-21 on facebook-ads-fields.md):
  - Sections are ``### NAME`` headings (e.g. ``### AD``, ``### DEPRECATED``).
  - Each field is represented by a *clean* run of markdown table lines:
      | <field_id> |
      | Field label | <label> |
      | Description | <desc> |        <- optional
      | Field type | dimension |      <- exactly one per field, dimension or metric
      | Data type | [<token>](...) |
    Clean lines start with a literal ``|`` and do NOT contain ``\\|``
    (escaped-pipe rows are duplicated display rows that must be ignored).
  - ``### DEPRECATED`` section sets ``deprecated: True`` on contained fields.

The parser is format-generic: no provider names are hardcoded here.
Provider-specific paths live in per-module catalog_sources.json (story 25.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class SupermetricsEntry:
    """One field entry from a Supermetrics markdown snapshot."""

    field_id: str
    label: str
    description: str | None
    kind: str  # "metric" | "dimension"
    data_type: str
    section: str
    deprecated: bool = False


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_RE_SECTION = re.compile(r"^###\s+(.+)$")
# Clean line: starts with |, no escaped pipe (\|) in the content
_RE_CLEAN_PIPE = re.compile(r"^\|(?!.*\\\|)")
# | <field_id> |  — two-cell row where second cell is empty (field_id row)
_RE_FIELD_ID_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*$")
# | Key | Value |
_RE_KV_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
# Extract data type token from [token](url) or plain token
_RE_DATA_TYPE_TOKEN = re.compile(r"^\[([^\]]+)\]")


def _extract_data_type(raw: str) -> str:
    """Extract the token part from ``[token](url)`` or return raw value."""
    raw = raw.strip()
    m = _RE_DATA_TYPE_TOKEN.match(raw)
    return m.group(1) if m else raw


def parse_supermetrics_markdown(text: str) -> list[SupermetricsEntry]:
    """Parse a Supermetrics fields markdown into a list of enrichment entries.

    Only clean table rows (no ``\\|``) are processed.
    Returns entries in document order.
    """
    entries: list[SupermetricsEntry] = []

    current_section = "UNKNOWN"
    is_deprecated_section = False

    # State machine buffers
    pending_field_id: str | None = None
    pending_label: str | None = None
    pending_description: str | None = None
    pending_kind: str | None = None
    pending_data_type: str | None = None

    def _flush() -> None:
        nonlocal pending_field_id, pending_label, pending_description
        nonlocal pending_kind, pending_data_type
        if (
            pending_field_id is not None
            and pending_label is not None
            and pending_kind is not None
            and pending_data_type is not None
        ):
            entries.append(
                SupermetricsEntry(
                    field_id=pending_field_id,
                    label=pending_label,
                    description=pending_description,
                    kind=pending_kind,
                    data_type=pending_data_type,
                    section=current_section,
                    deprecated=is_deprecated_section,
                )
            )
        pending_field_id = None
        pending_label = None
        pending_description = None
        pending_kind = None
        pending_data_type = None

    for line in text.splitlines():
        # --- Section heading ---
        m_section = _RE_SECTION.match(line)
        if m_section:
            # Flush any in-progress field when section changes
            _flush()
            current_section = m_section.group(1).strip()
            is_deprecated_section = current_section.upper() == "DEPRECATED"
            continue

        # --- Skip non-clean or non-pipe lines ---
        if not _RE_CLEAN_PIPE.match(line):
            continue

        # Separator rows like |---|---|
        if re.match(r"^\|[\s\-|]+\|$", line):
            continue

        # --- Try field_id row: | <value> |  (single-value, no second |key|val|) ---
        m_field = _RE_FIELD_ID_ROW.match(line)
        if m_field:
            cell = m_field.group(1).strip()
            # Must not look like a key-value row (key would have a pipe-separated value)
            # single-cell rows are field_id rows — start a new field
            _flush()
            pending_field_id = cell
            continue

        # --- Key-Value row ---
        m_kv = _RE_KV_ROW.match(line)
        if m_kv and pending_field_id is not None:
            key = m_kv.group(1).strip().lower()
            value = m_kv.group(2).strip()
            if key == "field label":
                pending_label = value
            elif key == "description":
                pending_description = value
            elif key == "field type":
                val_lower = value.lower()
                if val_lower in ("dimension", "metric"):
                    pending_kind = val_lower
            elif key == "data type":
                pending_data_type = _extract_data_type(value)

    _flush()
    return entries
