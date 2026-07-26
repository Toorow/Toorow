/**
 * WidgetCardsPage — Analyze › Widgets: the card-template catalog.
 *
 * ── Data ─────────────────────────────────────────────────────────────────────
 * GET /api/cards/templates?project_id=… → {templates: [...]}. Each entry carries
 * `id`, `title`, `answers_question`, `widget_uri`, `kind` (kpi | context),
 * `required_metrics`, `required_dimensions`, `comment_builder`, and — because
 * project_id is sent — `usable` plus `missing` (the canonical fields the project
 * does not have). Usability is the only claim this page makes, and the server
 * makes it.
 *
 * History (audit 2026-07-25): the page listed four invented widgets ("KPI Hero
 * Card", "Trend Sparkline Card", …) with no I/O whatsoever, `void projectId`, and
 * a "Preview widget" button on each card that did nothing. The real catalog
 * endpoint existed and was never called.
 *
 * Styling: application.css (global, via the shell) + widgets.css. Colors come
 * exclusively from the application.css CSS variables (dark-safe).
 */
import { useCallback, useEffect, useState } from "react";
import { apiGet } from "./lib/apiFetch";
import "./shell/application.css";
import "./widgets.css";

interface CardTemplate {
  id: string;
  title?: string | null;
  answers_question?: string | null;
  widget_uri?: string | null;
  kind?: string | null;
  required_metrics?: string[];
  required_dimensions?: string[];
  comment_builder?: boolean;
  usable?: boolean;
  missing?: { metrics?: string[]; dimensions?: string[] };
}

interface TemplatesResponse {
  templates?: CardTemplate[];
}

type LoadState = "loading" | "error" | "ready";

function kindLabel(kind: string | null | undefined): string {
  if (kind === "context") return "Context";
  if (kind === "kpi") return "KPI";
  return kind || "Card";
}

export default function WidgetCardsPage({ projectId }: { projectId?: string }) {
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [templates, setTemplates] = useState<CardTemplate[]>([]);
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let alive = true;
    setState("loading");
    setError("");

    (async () => {
      try {
        // project_id is what makes the server compute `usable` / `missing`; without
        // it the catalog is generic. It is always present in the shell.
        const path = projectId
          ? `/api/cards/templates?project_id=${encodeURIComponent(projectId)}`
          : "/api/cards/templates";
        const body = await apiGet<TemplatesResponse>(path);
        if (!alive) return;
        setTemplates(Array.isArray(body.templates) ? body.templates : []);
        setState("ready");
      } catch (err) {
        if (!alive) return;
        setTemplates([]);
        setError(err instanceof Error ? err.message : "Request failed");
        setState("error");
      }
    })();

    return () => {
      alive = false;
    };
  }, [projectId, attempt]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Widget catalog</h1>
          <p>
            The card templates an AI agent can answer with, and whether this project
            holds the canonical fields each one needs.
          </p>
        </div>
      </div>

      {state === "loading" ? (
        <section className="panel widget-state" data-testid="widgets-loading">
          <p>Loading the card catalog…</p>
        </section>
      ) : null}

      {state === "error" ? (
        <section className="panel widget-state error" data-testid="widgets-error">
          <p>Couldn&apos;t load the card catalog. {error}</p>
          <button className="secondary-button" type="button" onClick={retry}>
            Retry
          </button>
        </section>
      ) : null}

      {state === "ready" && templates.length === 0 ? (
        <section className="panel widget-state" data-testid="widgets-empty">
          <p>No card template is registered on this server.</p>
        </section>
      ) : null}

      {state === "ready" && templates.length > 0 ? (
        <section className="widget-catalog">
          {templates.map((card) => {
            const missingMetrics = card.missing?.metrics ?? [];
            const missingDimensions = card.missing?.dimensions ?? [];
            const missingAll = [...missingMetrics, ...missingDimensions];
            const required = [
              ...(card.required_metrics ?? []),
              ...(card.required_dimensions ?? []),
            ];
            return (
              <article key={card.id} className="panel widget-card" data-testid={`card-${card.id}`}>
                <div className="widget-card-head">
                  <span className="widget-tag category">{kindLabel(card.kind)}</span>
                  {card.usable === undefined ? null : (
                    <span className={`signal-label ${card.usable ? "success" : "warning"}`}>
                      <span className="signal-mark" />
                      {card.usable ? "Usable" : "Missing inputs"}
                    </span>
                  )}
                </div>
                <h3>{card.title || card.id}</h3>
                <p>{card.answers_question || "No question recorded for this template."}</p>

                {required.length > 0 ? (
                  <div className="widget-requires">
                    <span className="widget-requires-label">Requires</span>
                    <div className="widget-chips">
                      {required.map((f) => (
                        <code
                          className={`mono widget-chip${missingAll.includes(f) ? " missing" : ""}`}
                          key={f}
                        >
                          {f}
                        </code>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="widget-requires">
                    <span className="widget-requires-label">
                      No canonical field required
                    </span>
                  </div>
                )}

                <div className="widget-card-foot">
                  <code className="mono widget-uri" title={card.widget_uri ?? undefined}>
                    {card.widget_uri || card.id}
                  </code>
                  {card.comment_builder ? (
                    <span className="widget-tag type">Commentary</span>
                  ) : null}
                </div>
              </article>
            );
          })}
        </section>
      ) : null}
    </>
  );
}
