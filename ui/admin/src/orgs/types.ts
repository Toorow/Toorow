/**
 * Organization domain types — the canonical `Org` shape returned by
 * GET/PATCH /api/organizations. Lives here (not in a legacy page) so the v3
 * org surfaces (OrgSettings, OrgDetailPanel) own their contract with no
 * dependency on removed legacy screens.
 */
export interface Org {
  id: string;
  name: string;
  slug: string;
  status: string;
  billing_ref: string | null;
  brand_primary: string | null;
  brand_secondary: string | null;
  brand_accent: string | null;
  logo_url: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}
