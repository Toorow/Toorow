/**
 * GraphEdges — graph edge linking UI for a document (Story 11.4, F-3).
 *
 * Lists, creates and deletes context_graph edges for the selected node via:
 *   GET    /api/context/graph/edges?project_id=…
 *   POST   /api/context/graph/edges
 *   DELETE /api/context/graph/edges/{id}
 *
 * AD-15: all writes go through contextApi helpers.
 * AD-17: edges TO a schema_doc are allowed here; writing schema_context docs is not.
 * Non-colour status cues (chip shapes + labels), MD3 tokens only.
 * Copy: French accented (UX-DR10).
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Select,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { createGraphEdge, deleteGraphEdge, listGraphEdges } from "./contextApi";
import type { ApiError, GraphEdge, GraphNodeType } from "./types";

// ---------------------------------------------------------------------------
// Inline icon helpers (no @mui/icons-material dependency)
// ---------------------------------------------------------------------------

function IconTrash() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </svg>
  );
}

function IconLink() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Node type labels (French)
// ---------------------------------------------------------------------------

const NODE_TYPE_LABELS: Record<GraphNodeType, string> = {
  topic: "Topic",
  procedure: "Procédure",
  schema_doc: "Doc schéma",
};

const EDGE_TYPE_OPTIONS = [
  "related",
  "depends_on",
  "refines",
  "example_of",
  "contradicts",
];

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface GraphEdgesProps {
  /** ID of the document node whose edges are managed. */
  nodeId: string;
  /** ID of the current project (used for AD-5 scoping on writes). */
  projectId: string | null;
  /**
   * Type of the current node ('topic' | 'procedure').
   * schema_doc nodes are read-only (AD-17), so GraphEdges is only rendered
   * from TopicsTab and ProceduresTab.
   */
  nodeType: GraphNodeType;
}

// ---------------------------------------------------------------------------
// GraphEdges component
// ---------------------------------------------------------------------------

export default function GraphEdges({ nodeId, projectId, nodeType }: GraphEdgesProps) {
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Add-edge form state
  const [showAddForm, setShowAddForm] = useState(false);
  const [toId, setToId] = useState("");
  const [toType, setToType] = useState<GraphNodeType>("topic");
  const [edgeType, setEdgeType] = useState("related");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const loadEdges = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const all = await listGraphEdges(projectId);
      // Filter to edges where this node appears as from_id or to_id
      setEdges(all.filter((e) => e.from_id === nodeId || e.to_id === nodeId));
    } catch (err) {
      const e = err as ApiError;
      setLoadError(e.message ?? "Erreur lors du chargement des liaisons.");
    } finally {
      setLoading(false);
    }
  }, [nodeId, projectId]);

  useEffect(() => {
    void loadEdges();
  }, [loadEdges]);

  async function handleDelete(edgeId: string) {
    setDeleting(edgeId);
    setDeleteError(null);
    try {
      await deleteGraphEdge(edgeId);
      setEdges((prev) => prev.filter((e) => e.id !== edgeId));
    } catch (err) {
      const e = err as ApiError;
      setDeleteError(e.message ?? "Erreur lors de la suppression.");
    } finally {
      setDeleting(null);
    }
  }

  async function handleCreate() {
    if (!toId.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const newEdge = await createGraphEdge({
        project_id: projectId,
        from_id: nodeId,
        from_type: nodeType,
        to_id: toId.trim(),
        to_type: toType,
        edge_type: edgeType,
      });
      setEdges((prev) => [newEdge, ...prev]);
      setToId("");
      setToType("topic");
      setEdgeType("related");
      setShowAddForm(false);
    } catch (err) {
      const e = err as ApiError;
      setCreateError(e.message ?? "Erreur lors de la création de la liaison.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <Box
      sx={{
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 2,
        p: 2,
        bgcolor: "background.paper",
      }}
      data-testid="graph-edges-panel"
      aria-label="Liaisons du graphe de connaissances"
    >
      {/* Header */}
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", mb: 1 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <IconLink />
          <Typography
            variant="caption"
            sx={{
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: ".04em",
              fontSize: "10px",
              color: "text.secondary",
            }}
          >
            Liaisons (graphe de connaissances)
          </Typography>
        </Box>
        <Button
          size="small"
          variant="outlined"
          onClick={() => {
            setShowAddForm((v) => !v);
            setCreateError(null);
          }}
          data-testid="btn-add-graph-edge"
          aria-label="Ajouter une liaison"
          sx={{ height: 24, fontSize: "11px", px: 1 }}
        >
          {showAddForm ? "Annuler" : "+ Ajouter"}
        </Button>
      </Box>

      {/* Add-edge form */}
      {showAddForm && (
        <Box
          sx={{ display: "flex", flexDirection: "column", gap: 1.5, mb: 2, mt: 1 }}
          data-testid="graph-edge-add-form"
        >
          <TextField
            label="ID du nœud cible"
            placeholder="ex : top_01ABCDEF ou proc_01ABCDEF"
            value={toId}
            onChange={(e) => setToId(e.target.value)}
            size="small"
            fullWidth
            slotProps={{ input: { "aria-label": "ID du nœud cible" } }}
            data-testid="graph-edge-to-id-input"
          />
          <Box sx={{ display: "flex", gap: 1 }}>
            <FormControl size="small" sx={{ flex: 1 }}>
              <InputLabel id="graph-edge-to-type-label">Type cible</InputLabel>
              <Select
                labelId="graph-edge-to-type-label"
                value={toType}
                label="Type cible"
                onChange={(e) => setToType(e.target.value as GraphNodeType)}
                data-testid="graph-edge-to-type-select"
              >
                <MenuItem value="topic">{NODE_TYPE_LABELS.topic}</MenuItem>
                <MenuItem value="procedure">{NODE_TYPE_LABELS.procedure}</MenuItem>
                <MenuItem value="schema_doc">{NODE_TYPE_LABELS.schema_doc}</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ flex: 1 }}>
              <InputLabel id="graph-edge-type-label">Type de liaison</InputLabel>
              <Select
                labelId="graph-edge-type-label"
                value={edgeType}
                label="Type de liaison"
                onChange={(e) => setEdgeType(e.target.value)}
                data-testid="graph-edge-type-select"
              >
                {EDGE_TYPE_OPTIONS.map((opt) => (
                  <MenuItem key={opt} value={opt}>
                    {opt}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Box>

          {createError && (
            <Alert severity="error" sx={{ fontSize: "12px", py: 0.5 }} data-testid="graph-edge-create-error">
              {createError}
            </Alert>
          )}

          <Button
            variant="contained"
            size="small"
            onClick={() => { void handleCreate(); }}
            disabled={creating || !toId.trim()}
            data-testid="btn-save-graph-edge"
            aria-label="Créer la liaison"
          >
            {creating ? <CircularProgress size={14} /> : "Créer la liaison"}
          </Button>
        </Box>
      )}

      {/* Load error */}
      {loadError && (
        <Alert severity="error" sx={{ mb: 1, fontSize: "12px" }}>
          {loadError}
        </Alert>
      )}

      {/* Delete error */}
      {deleteError && (
        <Alert severity="error" sx={{ mb: 1, fontSize: "12px" }} data-testid="graph-edge-delete-error">
          {deleteError}
        </Alert>
      )}

      {/* Edge list */}
      {loading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 1 }}>
          <CircularProgress size={18} />
        </Box>
      ) : edges.length === 0 ? (
        <Typography
          variant="caption"
          sx={{ color: "text.disabled", display: "block", textAlign: "center", py: 1 }}
          data-testid="graph-edges-empty"
        >
          Aucune liaison définie pour ce document.
        </Typography>
      ) : (
        <List dense disablePadding data-testid="graph-edges-list">
          {edges.map((edge, idx) => {
            const isSource = edge.from_id === nodeId;
            const peerId = isSource ? edge.to_id : edge.from_id;
            const peerType = isSource ? edge.to_type : edge.from_type;

            return (
              <Box key={edge.id}>
                {idx > 0 && <Divider />}
                <ListItem
                  disablePadding
                  sx={{ py: 0.5 }}
                  secondaryAction={
                    <Tooltip title="Supprimer cette liaison">
                      <span>
                        <IconButton
                          size="small"
                          onClick={() => { void handleDelete(edge.id); }}
                          disabled={deleting === edge.id}
                          aria-label={`Supprimer la liaison ${edge.id}`}
                          data-testid={`btn-delete-edge-${edge.id}`}
                        >
                          {deleting === edge.id ? (
                            <CircularProgress size={12} />
                          ) : (
                            <IconTrash />
                          )}
                        </IconButton>
                      </span>
                    </Tooltip>
                  }
                >
                  <ListItemText
                    primary={
                      <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, flexWrap: "wrap" }}>
                        <Chip
                          label={isSource ? "→" : "←"}
                          size="small"
                          variant="outlined"
                          sx={{ height: 16, fontSize: "10px", minWidth: 24 }}
                          aria-label={isSource ? "liaison sortante" : "liaison entrante"}
                        />
                        <Chip
                          label={edge.edge_type}
                          size="small"
                          variant="filled"
                          sx={{ height: 16, fontSize: "10px" }}
                        />
                        <Chip
                          label={NODE_TYPE_LABELS[peerType] ?? peerType}
                          size="small"
                          variant="outlined"
                          sx={{ height: 16, fontSize: "10px" }}
                        />
                        {edge.project_id === null && (
                          <Chip
                            label="plateforme"
                            size="small"
                            variant="outlined"
                            sx={{ height: 16, fontSize: "10px" }}
                          />
                        )}
                      </Box>
                    }
                    secondary={
                      <Typography
                        component="span"
                        variant="caption"
                        sx={{ fontFamily: "var(--font-mono, monospace)", fontSize: "11px", color: "text.secondary" }}
                      >
                        {peerId}
                      </Typography>
                    }
                    data-testid={`graph-edge-row-${edge.id}`}
                  />
                </ListItem>
              </Box>
            );
          })}
        </List>
      )}
    </Box>
  );
}
