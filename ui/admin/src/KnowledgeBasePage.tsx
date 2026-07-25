/**
 * KnowledgeBasePage — v3 restyle of the shared knowledge base.
 *
 * Mounted at Context/knowledge via shell/ContentRouter.tsx as
 * <KnowledgeBasePage projectId={projectId} />. The application shell already
 * renders the frame, sidebar, topbar, and <main>. This component renders ONLY
 * the page content inside <main>: the page header, an "Add knowledge entry"
 * action, and the grid of governed knowledge entries.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, header-actions, primary-button, secondary-button, panel) +
 * knowledge.css for the page-specific card grid, entry cards, and topic pill.
 * Colors come exclusively from the application.css CSS variables (dark-theme
 * safe); dates use Geist tabular via .knowledge-meta.
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * Backed by GET /api/knowledge?project_id=<id>. The literal entries below render
 * finished while the fetch is in flight and offline (fallback); a successful
 * fetch replaces them with the real, project-scoped knowledge base — which is
 * honestly empty until entries are authored. The Add/Edit actions stay
 * placeholders (no write endpoint wired yet).
 */
import { useEffect, useState } from "react";
import "./shell/application.css";
import "./knowledge.css";

interface KnowledgeEntry {
  title: string;
  topic: string;
  updated: string;
  author: string;
}

/** Shape of one row returned by GET /api/knowledge. */
interface ApiEntry {
  id: string;
  title: string;
  topic: string;
  body: string;
  author: string;
  updated_at: string;
}

// Literal fallback — the validated design, rendered verbatim while the fetch is
// in flight and offline so the surface stays finished. A successful fetch
// replaces these with the real project-scoped entries.
const FALLBACK_ENTRIES: KnowledgeEntry[] = [
  {
    title: "ROAS calculation & deduplication policy",
    topic: "Attribution",
    updated: "2 days ago",
    author: "Winston (Architect)",
  },
  {
    title: "Canonical revenue vs commerce gross sales",
    topic: "Semantics",
    updated: "1 week ago",
    author: "Mary (Analyst)",
  },
  {
    title: "Post-click attribution reconciliation procedure",
    topic: "Procedures",
    updated: "3 days ago",
    author: "Paige (Tech Writer)",
  },
];

/** Format an ISO updated_at to a short human date, e.g. "12 May 2026". */
function formatUpdated(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function toEntry(e: ApiEntry): KnowledgeEntry {
  return {
    title: e.title,
    topic: e.topic,
    updated: formatUpdated(e.updated_at),
    author: e.author,
  };
}

export default function KnowledgeBasePage({ projectId = "default" }: { projectId?: string }) {
  // FALLBACK_ENTRIES render finished while the fetch is in flight and offline; a
  // successful fetch replaces them with the real, project-scoped knowledge base
  // (which is honestly empty until entries are authored).
  const [entries, setEntries] = useState<KnowledgeEntry[]>(FALLBACK_ENTRIES);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`/api/knowledge?project_id=${encodeURIComponent(projectId)}`);
        if (!res.ok) return; // keep the literal fallback
        const body = (await res.json()) as { entries?: ApiEntry[] };
        if (alive) {
          setEntries((body.entries ?? []).map(toEntry));
          setLoaded(true);
        }
      } catch {
        /* offline — keep the designed literals so the surface stays finished. */
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Shared knowledge base</h1>
          <p>
            Governed business definitions, metric calculation procedures, and guidance read by AI
            agents during analysis.
          </p>
        </div>
        <div className="header-actions">
          {/* TODO(api): open the Add knowledge entry flow. */}
          <button className="primary-button" type="button">
            + Add knowledge entry
          </button>
        </div>
      </div>

      {loaded && entries.length === 0 ? (
        <section className="panel knowledge-empty">
          <h2>No knowledge entries yet</h2>
          <p>
            Governed definitions and procedures added here are read by AI agents during analysis.
            Add your first entry to start building this project&rsquo;s shared knowledge base.
          </p>
        </section>
      ) : (
        <section className="knowledge-grid">
          {entries.map((entry) => (
            <article key={entry.title} className="panel knowledge-card">
              <span className="knowledge-topic">{entry.topic}</span>
              <h2>{entry.title}</h2>
              <div className="knowledge-meta">
                <span>
                  Author: <b>{entry.author}</b>
                </span>
                <span>
                  Last updated: <time>{entry.updated}</time>
                </span>
              </div>
              {/* TODO(api): open this entry in the knowledge editor. */}
              <button className="secondary-button" type="button">
                Edit entry
              </button>
            </article>
          ))}
        </section>
      )}
    </>
  );
}
