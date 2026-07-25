/**
 * BusinessContextPanel — the Context / Events surface (mounted at
 * context/events by the shell ContentRouter).
 *
 * Restyled onto the validated v3 design system: the content lives inside the
 * shell <main>, opening with a page-header (title + muted subtitle), then a
 * two-column layout — an "Add event" form panel and the context-events table.
 * Styling comes from application.css (global, via the shell) for the shell
 * classes (panel, page-header, section-header, table, signal-label,
 * primary-button, sr-only) + events.css for the page-specific form and table
 * treatments. Colors resolve exclusively through the application.css CSS
 * variables (dark-safe); dates use the Geist tabular .number class.
 *
 * Behavior is unchanged: same /api/context-events GET/POST, same validation,
 * same loading/empty/error states, same props.
 */
import { useCallback, useEffect, useId, useState } from "react";
import "./events.css";
import { apiFetch } from "./lib/apiFetch";

export interface ContextEvent {
  id: string;
  project_id: string;
  event_date: string;
  type: string;
  label: string;
  created_by: string;
  created_at: string;
}

interface ContextEventsResponse {
  events: ContextEvent[];
}

interface BusinessContextPanelProps {
  projectId?: string;
}

const EVENT_TYPE_OPTIONS = [
  { value: "business", label: "Business activity" },
  { value: "incident", label: "Technical incident" },
  { value: "deployment", label: "Deployment" },
  { value: "other", label: "Other" },
] as const;

export default function BusinessContextPanel({ projectId = "default" }: BusinessContextPanelProps) {
  const dateId = useId();
  const typeId = useId();
  const labelId = useId();

  const [events, setEvents] = useState<ContextEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [formDate, setFormDate] = useState("");
  const [formType, setFormType] = useState<string>("business");
  const [formLabel, setFormLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [formSuccess, setFormSuccess] = useState<string | null>(null);

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    setListError(null);
    try {
      const url = `/api/context-events?project_id=${encodeURIComponent(projectId)}`;
      const resp = await apiFetch(url);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
      }
      const data: ContextEventsResponse = await resp.json();
      setEvents(data.events ?? []);
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void fetchEvents();
  }, [fetchEvents]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setFormSuccess(null);

    if (!formDate) {
      setFormError("A date is required.");
      return;
    }
    if (!formLabel.trim()) {
      setFormError("A label is required.");
      return;
    }
    if (formLabel.trim().length > 120) {
      setFormError("The label must not exceed 120 characters.");
      return;
    }

    setSubmitting(true);
    try {
      const resp = await apiFetch("/api/context-events", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          event_date: formDate,
          type: formType,
          label: formLabel.trim(),
        }),
      });
      if (!resp.ok) {
        const body: { error?: string; message?: string } = await resp.json().catch(() => ({}));
        throw new Error(body.error ?? body.message ?? `HTTP ${resp.status}`);
      }
      setFormDate("");
      setFormType("business");
      setFormLabel("");
      setFormSuccess("Event added successfully.");
      await fetchEvents();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }, [formDate, formLabel, formType, projectId, fetchEvents]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Context</h1>
          <p>Business context events — annotate the timeline with launches, incidents, and deployments so the data reads in context.</p>
        </div>
      </div>

      <div className="events-layout">
        <form
          className="panel event-form"
          onSubmit={(e) => { void handleSubmit(e); }}
          aria-label="Add context event"
        >
          <div className="section-header" style={{ minHeight: "auto", padding: 0, border: 0 }}>
            <h2>Add an event</h2>
          </div>

          <div className="field">
            <label htmlFor={dateId}>Date *</label>
            <input
              id={dateId}
              className="field-input"
              type="date"
              value={formDate}
              onChange={(e) => setFormDate(e.target.value)}
              aria-label="Event date"
              required
            />
          </div>

          <div className="field">
            <label htmlFor={typeId}>Type *</label>
            <select
              id={typeId}
              className="field-select"
              value={formType}
              onChange={(e) => setFormType(e.target.value)}
              aria-label="Event type"
              required
            >
              {EVENT_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor={labelId}>Label (≤ 120 characters) *</label>
            <input
              id={labelId}
              className="field-input"
              type="text"
              value={formLabel}
              onChange={(e) => setFormLabel(e.target.value)}
              aria-label="Event label"
              maxLength={120}
              required
            />
          </div>

          {formError && (
            <div className="form-note error" role="alert">
              <span>{formError}</span>
              <button type="button" onClick={() => setFormError(null)} aria-label="Dismiss error">
                ×
              </button>
            </div>
          )}
          {formSuccess && (
            <div className="form-note success" role="status">
              <span>{formSuccess}</span>
              <button type="button" onClick={() => setFormSuccess(null)} aria-label="Dismiss message">
                ×
              </button>
            </div>
          )}

          <button
            type="submit"
            className="primary-button"
            disabled={submitting}
            aria-label="Add event"
          >
            {submitting ? "Adding…" : "+ Add context event"}
          </button>
        </form>

        <section className="panel events-panel">
          <div className="section-header">
            <div>
              <h2>Context events</h2>
              <p>Events recorded for this project.</p>
            </div>
          </div>

          {loading ? (
            <div className="events-state">Loading…</div>
          ) : listError ? (
            <div className="events-state error" role="alert">
              Failed to load: {listError}
            </div>
          ) : events.length === 0 ? (
            <div className="events-state">No context events for this project yet.</div>
          ) : (
            <table className="table events-table" aria-label="Context events">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>Label</th>
                  <th>Created by</th>
                </tr>
              </thead>
              <tbody>
                {events.map((evt) => (
                  <tr key={evt.id}>
                    <td className="date number">{evt.event_date}</td>
                    <td>
                      <span className="type-chip">{evt.type}</span>
                    </td>
                    <td>{evt.label}</td>
                    <td>{evt.created_by}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </>
  );
}
