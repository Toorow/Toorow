import { useMemo, useState, type FormEvent } from "react";
import { apiFetch } from "../../lib/apiFetch";
import type { OrgRef } from "../scope";
import "../application.css";
import "./create-org.css";

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

export interface CreateProjectProps {
  org: OrgRef;
  onCreated?: (projectId: string) => void;
}

export default function CreateProject({ org, onCreated }: CreateProjectProps) {
  const [name, setName] = useState("First project");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slug = useMemo(() => slugify(name), [name]);
  const trimmedName = name.trim();
  const canSubmit =
    trimmedName.length > 0 && trimmedName.length <= 100 && slug.length > 0 && !submitting;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await apiFetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: trimmedName,
          slug,
          org_id: org.id,
          currency: "EUR",
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        id?: string;
        message?: string;
      };
      if (response.status !== 201 || !payload.id) {
        throw new Error(payload.message ?? `The project could not be created (HTTP ${response.status}).`);
      }
      if (onCreated) onCreated(payload.id);
      else window.location.assign(`/p/${encodeURIComponent(payload.id)}/overview/getting-started`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The project could not be created.");
      setSubmitting(false);
    }
  }

  return (
    <div className="createorg-stage">
      <div className="createorg-scrim">
        <form
          className="createorg-dialog"
          role="dialog"
          aria-labelledby="create-project-title"
          onSubmit={submit}
          noValidate
        >
          <header className="createorg-header">
            <div>
              <h1 id="create-project-title">Create your first project</h1>
              <p className="createorg-subtitle">
                {org.name} already exists. Add its first real project before opening the workspace.
              </p>
            </div>
          </header>
          <div className="createorg-body">
            <div className="scope-summary" aria-label="Project organization">
              <div className="scope-row">
                <span className="scope-key">Organization</span>
                <span className="scope-val">{org.name}</span>
              </div>
            </div>
            <div className="field">
              <label htmlFor="create-project-name">Project name</label>
              <input
                id="create-project-name"
                className="text-input"
                value={name}
                maxLength={100}
                required
                onChange={(event) => {
                  setName(event.target.value);
                  setError(null);
                }}
              />
              <p className="field-hint">
                Project slug: <span className="mono">{slug || "?"}</span>
              </p>
            </div>
            {error && (
              <div className="createorg-error" role="alert">
                {error}
              </div>
            )}
          </div>
          <footer className="createorg-footer">
            <span>This project will belong to {org.name}.</span>
            <button className="primary-button" type="submit" disabled={!canSubmit}>
              {submitting ? "Creating..." : "Create project"}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
