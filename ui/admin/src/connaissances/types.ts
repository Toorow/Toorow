/**
 * Types for the Connaissances editor (Story 11.4).
 *
 * Maps the 11.1 REST contract shapes for topics, procedures, and the
 * schema_context read-only documents shown with a « généré automatiquement » badge.
 */

// ---------------------------------------------------------------------------
// Topics
// ---------------------------------------------------------------------------

export interface Topic {
  id: string;
  project_id: string | null;
  title: string;
  body_md: string;
  status: "active" | "archived";
  created_by: string;
  created_at: string;
  updated_at: string;
  version_number?: number;
}

export interface TopicsResponse {
  topics: Topic[];
}

// ---------------------------------------------------------------------------
// Procedures
// ---------------------------------------------------------------------------

export interface Procedure {
  id: string;
  project_id: string | null;
  frontmatter_yaml: string;
  body_md: string;
  status: "active" | "archived";
  created_by: string;
  created_at: string;
  updated_at: string;
  version_number?: number;
  /** Parsed 'name' from frontmatter (returned by 11.1 store). */
  name?: string;
}

export interface ProceduresResponse {
  procedures: Procedure[];
}

// ---------------------------------------------------------------------------
// Schema context (read-only, generated — AD-17)
// ---------------------------------------------------------------------------

export interface SchemaDoc {
  id: string;
  relation_name: string;
  doc_kind: "columns" | "description" | "preview";
  body_md: string;
  generated_at: string;
  version_number?: number;
}

export interface SchemaDocsResponse {
  docs: SchemaDoc[];
}

// ---------------------------------------------------------------------------
// API error shapes
// ---------------------------------------------------------------------------

export interface ApiError {
  code: string;
  message: string;
}

// ---------------------------------------------------------------------------
// Union type for the three document kinds the page displays
// ---------------------------------------------------------------------------

export type DocKind = "topic" | "procedure" | "schema";

export interface DocRef {
  kind: DocKind;
  id: string;
  label: string;
}

// ---------------------------------------------------------------------------
// Graph edges (Story 11.4)
// ---------------------------------------------------------------------------

/** Valid node types as per migration 031 CHECK constraint. */
export type GraphNodeType = "topic" | "procedure" | "schema_doc";

export interface GraphEdge {
  id: string;
  from_id: string;
  from_type: GraphNodeType;
  to_id: string;
  to_type: GraphNodeType;
  edge_type: string;
  project_id: string | null;
  created_by: string;
  created_at: string;
}

export interface GraphEdgesResponse {
  edges: GraphEdge[];
}

export interface CreateGraphEdgePayload {
  project_id: string | null;
  from_id: string;
  from_type: GraphNodeType;
  to_id: string;
  to_type: GraphNodeType;
  edge_type: string;
}
