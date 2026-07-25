/**
 * ProjectSwitcher — top-bar project selector for the admin console (Story 7.1, AC6).
 *
 * - Populates a dropdown from GET /api/projects (active projects only).
 * - Persists the selected project id in localStorage ('connector_active_project').
 * - Falls back to the first project (alphabetical) when localStorage is empty or
 *   the stored id is no longer active.
 * - "Nouveau projet" opens a create dialog that POSTs /api/projects; on success
 *   the new project becomes active.
 *
 * UX-DR10: French-first UI copy.
 */
import { useEffect, useState, useCallback } from "react";
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  type SelectChangeEvent,
  TextField,
  Typography,
} from "@mui/material";

export const ACTIVE_PROJECT_STORAGE_KEY = "connector_active_project";

export interface Project {
  id: string;
  name: string;
  slug: string;
  status?: string;
  currency?: string;
  timezone?: string;
}

export interface ProjectSwitcherProps {
  /** Called whenever the active project changes (id). */
  onProjectChange?: (projectId: string) => void;
}

function readStoredProjectId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStoredProjectId(projectId: string): void {
  try {
    localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, projectId);
  } catch {
    /* localStorage unavailable — non-fatal */
  }
}

export default function ProjectSwitcher({ onProjectChange }: ProjectSwitcherProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const selectActive = useCallback(
    (projectId: string) => {
      setActiveId(projectId);
      writeStoredProjectId(projectId);
      onProjectChange?.(projectId);
    },
    [onProjectChange],
  );

  const loadProjects = useCallback(async () => {
    try {
      const resp = await fetch("/api/projects");
      if (!resp.ok) return;
      const body = await resp.json();
      const list: Project[] = body.projects ?? [];
      setProjects(list);

      if (list.length === 0) return;
      // Resolve the active project: stored id if still active, else 'default'
      // when present (polish: never land on an arbitrary alphabetical project),
      // else first of the name-ASC list.
      const stored = readStoredProjectId();
      const storedStillActive = stored && list.some((p) => p.id === stored);
      const fallback = list.find((p) => p.id === "default")?.id ?? list[0].id;
      selectActive(storedStillActive ? (stored as string) : fallback);
    } catch {
      /* network error — leave dropdown empty */
    }
  }, [selectActive]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  function handleSelectChange(event: SelectChangeEvent) {
    selectActive(event.target.value);
  }

  async function handleCreate() {
    const name = newName.trim();
    if (!name) {
      setCreateError("Le nom est requis.");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const resp = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!resp.ok) {
        setCreateError("Échec de la création du projet.");
        setCreating(false);
        return;
      }
      const created: Project = await resp.json();
      // Refresh the list and make the new project active.
      const listResp = await fetch("/api/projects");
      if (listResp.ok) {
        const body = await listResp.json();
        setProjects(body.projects ?? []);
      }
      selectActive(created.id);
      setDialogOpen(false);
      setNewName("");
    } catch {
      setCreateError("Échec de la création du projet.");
    } finally {
      setCreating(false);
    }
  }

  return (
    // Story 8.1: lives in the 240px sidebar header -- stack vertically,
    // full-width controls (the old horizontal AppBar layout clipped the button).
    <Box sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
      <FormControl size="small" fullWidth>
        <InputLabel id="project-switcher-label">Projet</InputLabel>
        <Select
          labelId="project-switcher-label"
          id="project-switcher"
          value={activeId}
          label="Projet"
          onChange={handleSelectChange}
          aria-label="Sélecteur de projet"
        >
          {projects.map((p) => (
            <MenuItem key={p.id} value={p.id}>
              {p.name} <Typography component="span" variant="caption" sx={{ ml: 1, opacity: 0.7 }}>({p.slug})</Typography>
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      <Button
        size="small"
        variant="text"
        fullWidth
        onClick={() => setDialogOpen(true)}
        data-testid="new-project-button"
        sx={{ justifyContent: "flex-start", px: 1 }}
      >
        + Nouveau projet
      </Button>

      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)}>
        <DialogTitle>Nouveau projet</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            margin="dense"
            label="Nom du projet"
            fullWidth
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            slotProps={{ htmlInput: { "aria-label": "Nom du projet" } }}
            error={Boolean(createError)}
            helperText={createError ?? ""}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)} disabled={creating}>
            Annuler
          </Button>
          <Button onClick={handleCreate} disabled={creating} variant="contained">
            Créer
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
