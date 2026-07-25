"""toorow -- Reserved string codes and platform constants (Story 2.5).

Reserved error/alert codes defined in ARCHITECTURE-SPINE.md Consistency Conventions.
Do NOT add business logic here -- this module is intentionally thin.

AD-2: source-agnostic -- no module-specific strings.
Windows/CI note (L-3): ASCII-only strings only.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Reserved alert codes (ARCHITECTURE-SPINE.md § Consistency Conventions)
# ---------------------------------------------------------------------------

AUTH_EXPIRED_CODE = "auth_expired"
"""OAuth token is invalid; the WidgetShell renders a reconnect affordance (AD-15)."""
