/**
 * WidgetCardsPage — Analyze › Widgets. Restyled onto the validated v3 design
 * system (application.css shell classes + widgets.css). The application shell
 * renders the frame/sidebar/topbar and <main className="main">; this component
 * renders only the page content that lives inside <main>: the page header and a
 * grid of MCP App widget-template cards.
 *
 * Styling: application.css (global, via the shell) for the shell/layout classes
 * (page-header, panel, secondary-button) + widgets.css for the catalog grid and
 * card internals. Colors come exclusively from the application.css CSS
 * variables, so the page is dark-safe.
 *
 * Behavior is unchanged from the prior MUI version: a static catalog of the
 * available widget templates. "Preview widget" has no wired handler yet.
 */
import "./shell/application.css";
import "./widgets.css";

interface WidgetCard {
  title: string;
  category: string;
  description: string;
  tag: string;
}

const CARDS: WidgetCard[] = [
  {
    title: "KPI Hero Card",
    category: "Metrics",
    description:
      "Displays a single tabular-numeral hero metric with a day-over-day delta indicator.",
    tag: "Interactive",
  },
  {
    title: "Trend Sparkline Card",
    category: "Dataviz",
    description:
      "7-day volume trend visualization for revenue, spend, and conversion events.",
    tag: "Chart",
  },
  {
    title: "Channel Breakdown Donut",
    category: "Attribution",
    description:
      "Multi-channel share donut comparing paid search, paid social, and organic traffic.",
    tag: "Chart",
  },
  {
    title: "Conversion Funnel Card",
    category: "Commerce",
    description:
      "Step-by-step conversion funnel from session landing to checkout completion.",
    tag: "Interactive",
  },
];

export default function WidgetCardsPage({ projectId = "default" }: { projectId?: string }) {
  void projectId;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Widget catalog</h1>
          <p>Interactive MCP App widget templates available for AI agent responses.</p>
        </div>
      </div>

      <section className="widget-catalog">
        {CARDS.map((card) => (
          <article key={card.title} className="panel widget-card">
            <div className="widget-card-head">
              <span className="widget-tag category">{card.category}</span>
              <span className="widget-tag type">{card.tag}</span>
            </div>
            <h3>{card.title}</h3>
            <p>{card.description}</p>
            <div className="widget-card-actions">
              {/* TODO(api): wire the widget preview flow. */}
              <button className="secondary-button" type="button">
                Preview widget
              </button>
            </div>
          </article>
        ))}
      </section>
    </>
  );
}
