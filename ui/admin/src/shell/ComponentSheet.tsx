/**
 * Gate G1 — the component sheet.
 *
 * One route rendering every component of the library in every variant and
 * every state, so the geometry can be judged once, on one page, before any of
 * the 46 screens is migrated. An error seen here costs one file; the same
 * error seen at the end costs forty-six.
 *
 * It is reachable at `/debug/components` and only in a development build
 * (`import.meta.env.DEV`), so it cannot ship. It renders no data and calls no
 * API: everything on it is literal, because the point is the geometry.
 *
 * Every value visible here comes from a component file whose header quotes the
 * mockup rule it was measured from, or states that no mockup covers it.
 */
import { useState } from "react";
import {
  AlertTriangleIcon,
  CalendarIcon,
  ChevronDownIcon,
  DownloadIcon,
  PlusIcon,
  MoreHorizontalIcon,
  SearchIcon,
  SettingsIcon,
  TrashIcon,
} from "lucide-react";
import { stateLabel,
  Accordion, AccordionContent, AccordionItem, AccordionTrigger,
  Avatar, AvatarFallback,
  Badge,
  Breadcrumb, BreadcrumbItem, BreadcrumbLink, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator,
  Button,
  Checkbox,
  Cluster,
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
  EmptyState,
  Field,
  Input,
  Label,
  Metric,
  Panel, PanelHeader,
  Popover, PopoverContent, PopoverTrigger,
  Progress,
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetInline,
  SheetTitle,
  SheetTrigger,
  Separator,
  Skeleton,
  Spinner,
  RadioGroup, RadioGroupItem,
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
  ActivityLog,
  Calendar,
  ChoiceGroup,
  CoverageBars,
  type CoverageDay,
  type CoverageStatus,
  PhaseTimeline,
  Status,
  Stepper,
  Switch,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
  Tabs, TabsContent, TabsList, TabsTrigger,
  Textarea,
  Toaster, TooltipProvider, Tooltip, TooltipContent, TooltipTrigger,
  TONES, FILLS,
  notify,
  type Tone,
} from "../ui";
import DateBreakdownGrid, {
  type DailyBreakdown,
} from "../datastreams/workbench/DateBreakdownGrid";
import BindingsTable from "../datastreams/workbench/mapping/BindingsTable";
import CanonicalTargetCell from "../datastreams/workbench/mapping/CanonicalTargetCell";
import type { CanonicalCatalog, CanonicalField } from "../datastreams/workbench/mapping/canonicalFields";
import { foldedReadings, type ReadingContext } from "../datastreams/workbench/mapping/bindingReadings";
import "../styles/theme.css";
import "../styles/console.css";

/** A titled block of the sheet, with the rule it was measured from. */
function Section({
  title,
  source,
  children,
}: {
  title: string;
  /** Where the geometry comes from — or that nothing covers it. */
  source: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-8">
      <div className="mb-3">
        <h2 className="m-0 font-display text-h2 font-h2 text-text">{title}</h2>
        <p className="mt-1 mb-0 font-mono text-caption text-text-secondary">{source}</p>
      </div>
      <Panel className="flex flex-col gap-5">{children}</Panel>
    </section>
  );
}

/** A labelled row inside a section, so a variant can be named while it is seen. */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[168px_minmax(0,1fr)] items-center gap-4">
      <div className="font-mono text-caption text-text-secondary">{label}</div>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </div>
  );
}

/**
 * A two-month ledger for the sheet. Shaped like a real one: mostly collected,
 * a failed stretch, a couple of partials, days the provider answered emptily,
 * and a tail that was never requested.
 */
const COVERAGE_DAYS: CoverageDay[] = (() => {
  const out: CoverageDay[] = [];
  for (let i = 0; i < 61; i++) {
    const d = new Date(2026, 5, 1 + i);
    const day = d.getDate();
    const month = d.getMonth();
    let status: CoverageStatus = "ok";
    // Order matters: the running day has to be tested before the
    // never-requested tail swallows it. The first fixture put `running` after
    // `day > 21` and the sheet quietly showed five of the six states.
    if (month === 6 && day === 22) status = "running";
    else if (month === 6 && day > 22) status = "never_fetched";
    else if (month === 6 && day >= 18 && day <= 21) status = "failed";
    else if (day === 4 || day === 17) status = "partial";
    else if (d.getDay() === 0) status = "empty";
    out.push({
      date: `${d.getFullYear()}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
      status,
    });
  }
  return out;
})();

/**
 * The day-by-day grid of story 58.2, mounted so its three refusals can be
 * MEASURED (arbitrage 9) — `scripts/measure_component_sheet.py` reads the 52px
 * compact row, the status pill that must not take two lines, and the grid that
 * must not overflow the 1128px main column, against a real browser.
 *
 * Vitest cannot see any of the three: `vitest.config.ts:15,26` is jsdom with
 * `css: false`, so no stylesheet applies and every rectangle is zero.
 *
 * This is a fixture on a dev-only sheet, not data: it carries the shapes the
 * geometry has to survive — the widest state word, a mapped field, an unmapped
 * one, an absence with its reason — and it is the only reason it exists.
 */
const DAY_GRID: DailyBreakdown = {
  connector: "meta-ads",
  window: { start: "2026-07-10", end: "2026-07-13", bounded_at: 92, bound_reached: false },
  columns: [
    { source_field: "media_date", target_field: "media_date_98245b", is_key_column: true,
      binding_status: "confirmed" },
    { source_field: "clicks", target_field: "clicks", is_key_column: false,
      binding_status: "confirmed" },
    { source_field: "audience_label", target_field: null, is_key_column: false,
      binding_status: "suggested" },
  ],
  columns_reason: null,
  columns_source: "mapping_version",
  column_row_join_available: false,
  column_row_join_reason: "mart_metric_is_a_dbt_literal",
  days: [
    { date: "2026-07-10", extract_status: "ok", job_state: "done", extract_count: 1,
      row_count: 150, row_count_reason: null, rows: 4, rows_reason: null,
      execution_id: "dse_1" },
    // The longest words of the vocabulary, on purpose: if the pill takes two
    // lines anywhere, it takes them here -- and on the prevented day below,
    // which is one character longer and puts the SAME sentence in the cell
    // beside it.
    { date: "2026-07-11", extract_status: "empty", job_state: "done", extract_count: 1,
      row_count: 0, row_count_reason: null, rows: 0, rows_reason: null,
      execution_id: "dse_2" },
    { date: "2026-07-12", extract_status: "ok", job_state: "done", extract_count: 2,
      row_count: null, row_count_reason: "measured_per_window", rows: 12,
      rows_reason: null, execution_id: "dse_3" },
    { date: "2026-07-13", extract_status: "never_fetched", job_state: "cancelled",
      extract_count: 1, row_count: null, row_count_reason: "not_verified", rows: null,
      rows_reason: "connector_not_in_mart", execution_id: null },
    // AI-307. The widest row this grid can draw: the refusal fills the `Extract`
    // pill AND the `Collection window` cell, and the row action carries the
    // connector's whole sentence as its accessible name.
    { date: "2026-07-14", extract_status: "never_fetched", job_state: "prevented",
      extract_count: 0, row_count: null, row_count_reason: "not_verified", rows: null,
      rows_reason: "connector_not_in_mart", execution_id: null,
      prevented_reason: "reviews_access_pending",
      prevented_message:
        "Request the reviews allowlist for this project, then re-ask these dates." },
  ],
  reason: null,
  rows_note: null,
  // WITH the country column — story 58.5. The grid is measured at its WIDEST or the
  // measurement is of a narrower grid than the one that ships: the country column is
  // the eighth, and 58.4 already showed what an unmeasured extra column does (the
  // shares went to 12.8/12 and the grid scrolled at 1128px). The capability is
  // `ready` here, which no project of the estate is: this sheet is an instrument
  // bench, not a screen, and it measures the layout rather than reporting a state.
  country: {
    capability_state: "ready",
    active: true,
    degraded: false,
    reason: null,
    bounded_at: 12,
    days: {
      "2026-07-10": {
        values: [
          { value: "FR", kind: "country", label: "FR", rows: 5 },
          { value: "DE", kind: "country", label: "DE", rows: 3 },
          {
            value: "__country_absent__",
            kind: "country_absent",
            label: "No country reported",
            rows: 2,
          },
        ],
        country_count: 2,
      },
    },
  },
};

/**
 * THE MAPPING TABLE, SO ITS GEOMETRY CAN BE MEASURED — findings D-1 and D-2 of
 * the 2026-08-12 visual review.
 *
 * `Split…` was cut off at the right edge on every row at 1600px and nothing went
 * red, because the epic contract's geometric guard covered the day grid and only
 * the day grid. This mounts the same component the Mapping tab mounts, read by
 * `scripts/measure_component_sheet.py` against a real browser.
 *
 * IT IS MOUNTED TWICE, because the table has two shapes and they carry two
 * different refusals.
 *
 *   · the shape that SHIPS — the readings that say the same thing on every row
 *     folded away, which is what the review captured on ten real rows. No column
 *     may leave the frame at 1128px and no control may be clipped.
 *   · the shape where EVERY reading varies — all seven of them back as columns.
 *     Ten columns of real text do not fit 1128px and the table scrolls, which is
 *     what `TableScroll` is for. What may never scroll out of reach is the way to
 *     act on a row, so the acts column is pinned and measured as "no control is
 *     clipped". Measuring only the folded shape would leave that unguarded.
 *
 * The values are the longest of their vocabularies (`Split into 3`, `Resolved by
 * a list`, a full ULID concept identity, a sample nobody truncated). `editable`
 * is true, which is what puts the acts column on the row at all.
 */
const SHEET_CATALOG: CanonicalField[] = [
  { id: "mdm_6D13WZ6E18GSZTTEH1JPZBACYW", canonical_name: "Sessions", concept_kind: "dimension", unit: null, aggregation: null, description: null },
  { id: "mdm_7E24X08F29HT0VVFJ2KQ0CBDZX", canonical_name: "Media cost (micros)", concept_kind: "metric", unit: "micros", aggregation: "sum", description: null },
];

const SHEET_CANONICAL_CATALOG: CanonicalCatalog = {
  projectId: "proj_EXAMPLE",
  fields: SHEET_CATALOG,
  loading: false,
  error: null,
  declare: async () => SHEET_CATALOG[0],
};

const MAPPING_ROWS: ReadingContext[] = [
  {
    id: "media_cost_micros",
    treated: false,
    reading: {
      field_id: "media_cost_micros", treatment: "Direct", sample_value: "1250000",
      joined_with: [], contributes_to: [],
    },
    field: {
      field_id: "media_cost_micros", source_identity: "media_cost_micros",
      suggestion: { semantic_role: "measure_additive" }, semantic_type: "INTEGER",
      aggregation: "sum", sensitivity: "none", profile: { confidence: 0.94 },
      binding: { status: "bound", mdm_target: "mdm_7E24X08F29HT0VVFJ2KQ0CBDZX" },
    },
  },
  {
    id: "campaign_country_theme",
    treated: true,
    reading: {
      field_id: "campaign_country_theme", treatment: "Split into 3",
      sample_value: "FR_brand_awareness", joined_with: [],
      contributes_to: ["country", "campaign_theme", "creative_format"],
    },
    field: {
      field_id: "campaign_country_theme", source_identity: "campaign_country_theme",
      suggestion: { semantic_role: "dimension" }, semantic_type: "STRING",
      aggregation: "none", sensitivity: "restricted", profile: { confidence: 0.2 },
      binding: { status: "ambiguous", mdm_target: null },
    },
  },
  {
    id: "audience_label",
    treated: false,
    reading: {
      field_id: "audience_label", treatment: "Resolved by a list",
      sample_value: "no sample value", joined_with: ["audience_id"], contributes_to: [],
    },
    field: {
      field_id: "audience_label", source_identity: "audience_label",
      suggestion: { semantic_role: "attribute" }, semantic_type: "STRING",
      aggregation: "count_distinct", sensitivity: "personal", profile: { confidence: 0.61 },
      binding: { status: "blocking", mdm_target: "mdm_6D13WZ6E18GSZTTEH1JPZBACYW" },
    },
  },
  {
    id: "legacy_placement_key",
    treated: false,
    reading: {
      field_id: "legacy_placement_key", treatment: "Excluded", sample_value: "unknown",
      joined_with: [], contributes_to: [],
    },
    field: {
      field_id: "legacy_placement_key", source_identity: "legacy_placement_key",
      suggestion: {}, semantic_type: "TIMESTAMP", aggregation: null,
      sensitivity: "confidential", profile: {},
      binding: { status: "excluded", mdm_target: null },
    },
  },
];

/**
 * The shape the review actually captured: ten rows on which `Role`, `Treatment`,
 * `Example value`, `Aggregation`, `Sensitivity` and the confidence all read the
 * same. Four rows are enough to state it — the fold rule needs two.
 */
const MAPPING_ROWS_FOLDED: ReadingContext[] = [
  "media_cost_micros",
  "campaign_country_theme",
  "audience_label",
  "legacy_placement_key",
].map((id, index) => ({
  id,
  treated: false,
  reading: {
    field_id: id, treatment: "Unavailable", sample_value: "unknown",
    joined_with: [], contributes_to: [],
  },
  field: {
    field_id: id, source_identity: id, suggestion: {}, semantic_type: "STRING",
    aggregation: null, sensitivity: "none", profile: {},
    binding: {
      status: ["bound", "ambiguous", "blocking", "excluded"][index],
      mdm_target: index === 0 ? "mdm_7E24X08F29HT0VVFJ2KQ0CBDZX" : null,
    },
  },
}));

/** Both mounts, through the SAME fold rule the screen runs. Handing the sheet a
 *  hand-written `folded` set would measure a table the product cannot produce. */
function MappingBindings({ rows }: { rows: ReadingContext[] }) {
  return (
    <BindingsTable
      rows={rows}
      folded={foldedReadings(rows)}
      editable
      excludedOf={(row) => (row.field.binding as Record<string, unknown>).status === "excluded"}
      joinSelection={["audience_label"]}
      onToggleExclusion={() => undefined}
      onToggleJoin={() => undefined}
      onSplit={() => undefined}
      targetCell={(row, excluded) => (
        <CanonicalTargetCell
          fieldId={row.id}
          target={String((row.field.binding as Record<string, unknown>).mdm_target ?? "")}
          editable={!excluded}
          catalog={SHEET_CANONICAL_CATALOG}
          claimed={new Set()}
          pending={row.id === "audience_label"}
          onSelect={() => undefined}
          onDeclare={() => undefined}
        />
      )}
    />
  );
}

const BUTTON_VARIANTS = ["default", "secondary", "ghost", "destructive", "link"] as const;

export default function ComponentSheet() {
  const [checked, setChecked] = useState(true);
  const [switched, setSwitched] = useState(true);
  const [radio, setRadio] = useState("daily");

  return (
    <TooltipProvider>
      {/* The base typography. The shell sets this on `body` in
          `application.css`; this route never loads that sheet, so without it
          every element that does not name a family inherited the system font
          and 16px — meaning the components were being judged in the wrong
          typeface. Found by `scripts/audit_component_consistency.py`. */}
      <div className="min-h-screen bg-background-light font-primary text-body text-text">
        <div className="mx-auto max-w-[1280px] px-10 py-10">
          <header className="mb-8">
            <p className="m-0 text-caption text-text-secondary">Gate G1</p>
            <h1 className="m-0 font-display text-h1 font-h1 tracking-[var(--h1-letter-spacing)] text-text">
              Component sheet
            </h1>
            <p className="mt-2 mb-0 max-w-[70ch] text-body text-text-secondary">
              Every component of the console library, in every variant and every state. Each
              block names the mockup rule its geometry was measured from — or says that no
              mockup covers it. Nothing on this page is migrated screen work; it exists to be
              looked at once, before the screens are touched.
            </p>
          </header>

          {/* ---------------------------------------------------------- Button */}
          <Section
            title="Button"
            source="application-v3.css l.209-234 — 40px · radius 999px · padding 0 16px · 13px/700 · 1px border. Icon-only: 40px square, radius 10px."
          >
            {BUTTON_VARIANTS.map((variant) => (
              <Row key={variant} label={variant}>
                <Button variant={variant}>Publish</Button>
                <Button variant={variant} size="sm">
                  Small
                </Button>
                <Button variant={variant} disabled>
                  Disabled
                </Button>
                <Button variant={variant}>
                  <PlusIcon />
                  With icon
                </Button>
              </Row>
            ))}
            <Row label="size · usage">
              <Button size="xs">xs 24 — in dense content</Button>
              <Button size="sm">sm 32 — in a row</Button>
              <Button size="default">default 40 — the page&rsquo;s actions</Button>
              <Button size="lg">lg 48 — the only action</Button>
            </Row>
            <Row label="icon-only">
              <Button size="icon-xs" variant="secondary" aria-label="Settings">
                <SettingsIcon />
              </Button>
              <Button size="icon-sm" variant="secondary" aria-label="Settings">
                <SettingsIcon />
              </Button>
              <Button size="icon" variant="secondary" aria-label="Settings">
                <SettingsIcon />
              </Button>
              <Button size="icon-lg" variant="secondary" aria-label="Settings">
                <SettingsIcon />
              </Button>
              <Button size="icon" variant="destructive" aria-label="Delete">
                <TrashIcon />
              </Button>
            </Row>
            <Row label="in a toolbar">
              <Button variant="secondary">
                <DownloadIcon />
                Export Excel
              </Button>
              <Button variant="ghost">
                Last 30 days
                <ChevronDownIcon />
              </Button>
              <Button>Activate</Button>
            </Row>
            {/* secondary and ghost differ by their ground, not their chrome:
                white reads on the page tint, page tint reads on white. Shown on
                both so the pair can be judged where each is meant to sit. */}
            <Row label="ground · on white">
              <div className="flex items-center gap-3 rounded-large bg-surface-light p-3">
                <Button variant="secondary">Secondary</Button>
                <Button variant="ghost">Ghost</Button>
                <Button>Recommended</Button>
              </div>
            </Row>
            <Row label="ground · on page">
              <div className="flex items-center gap-3 rounded-large bg-background-light p-3">
                <Button variant="secondary">Secondary</Button>
                <Button variant="ghost">Ghost</Button>
                <Button>Recommended</Button>
              </div>
            </Row>
          </Section>

          {/* ---------------------------------------------------------- Status */}
          <Section
            title="Status — inline, banner, toast"
            source="application-v3.css l.281 — 8px dot, 4px halo at 14% of the tone. The banner is a 2px gradient rim on a white surface, not a saturated fill. `active` makes the halo breathe (stops under prefers-reduced-motion)."
          >
            <Row label="inline">
              {FILLS.map((tone) => (
                <Status key={tone} tone={tone}>
                  {tone}
                </Status>
              ))}
            </Row>
            <Row label="inline · in flight">
              {FILLS.map((tone) => (
                <Status key={tone} tone={tone} active>
                  {tone}
                </Status>
              ))}
            </Row>
            {/* accent is not a sixth state: it is the recommended direction,
                and it is the same object as the rose dot on an invalid field. */}
            <Status
              tone="accent"
              as="block"
              title="Banner — accent"
              action={<Button size="sm">Connect a source</Button>}
            >
              The recommended direction, not a state. One per view.
            </Status>
            {TONES.map((tone: Tone) => (
              <Status
                key={tone}
                tone={tone}
                as="block"
                active={tone === "info"}
                title={`Banner — ${tone}`}
                action={<Button size="sm" variant="secondary">Review</Button>}
              >
                The same statement as the dot above, given room for a sentence and one
                follow-up.
              </Status>
            ))}
            <Row label="toast">
              {TONES.map((tone) => (
                <Button
                  key={tone}
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    notify(`Toast — ${tone}`, {
                      tone,
                      description: "Dispatched through notify(), which takes only a tone.",
                    })
                  }
                >
                  {tone}
                </Button>
              ))}
            </Row>
          </Section>

          {/* ----------------------------------------------------------- Badge */}
          <Section
            title="Badge"
            source="application-v3.css l.346-359 — min-height 28px · padding 0 10px · radius 999px · 12px/600. Fills are the `*-container` tokens: a badge is a small shape carrying text, so it stays light enough to read ink on. The deeper `*-surface` step is for large fields with no text, like the coverage marks."
          >
            <Row label="tone">
              {FILLS.map((tone) => (
                <Badge key={tone} tone={tone}>
                  {tone}
                </Badge>
              ))}
            </Row>
            <Row label="outline">
              <Badge outline>GA4</Badge>
              <Badge outline>google_ads</Badge>
              <Badge outline>v3</Badge>
            </Row>
            <Row label="with a mark">
              <Badge tone="success">
                <AlertTriangleIcon />
                Published
              </Badge>
              <Badge tone="warning">
                <AlertTriangleIcon />
                Partial
              </Badge>
            </Row>
          </Section>

          {/* ----------------------------------------------------------- Table */}
          <Section
            title="Table"
            source="application-v3.css l.361-384 — th 44px, 12px/700 uppercase, tracking .035em · td 64px, 14px · hairline divider, none on the last row."
          >
            <Panel flush className="shadow-none">
              <PanelHeader
                title="Datastreams"
                description="Four rows, one of each state."
                actions={<Button size="sm">
                  <PlusIcon />
                  New
                </Button>}
              />
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Datastream</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Published through</TableHead>
                    <TableHead className="text-right">Rows</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[
                    { name: "GA4 — web", tone: "success" as Tone, state: "Published", date: "2026-07-28", rows: "1 284 902" },
                    { name: "Google Ads — search", tone: "warning" as Tone, state: "Partial", date: "2026-07-26", rows: "402 118" },
                    { name: "Search Console", tone: "error" as Tone, state: "Failed", date: "2026-07-19", rows: "0" },
                    { name: "Meta Ads", tone: "info" as Tone, state: "Collecting", date: "—", rows: "—" },
                  ].map((r) => (
                    <TableRow key={r.name} interactive>
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <Avatar size="sm">
                            <AvatarFallback>{r.name.slice(0, 2)}</AvatarFallback>
                          </Avatar>
                          <div>
                            <div className="font-semibold">{r.name}</div>
                            <div className="text-caption text-text-secondary">example.com</div>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Status tone={r.tone}>{stateLabel(r.state)}</Status>
                      </TableCell>
                      <TableCell className="font-mono text-caption">{r.date}</TableCell>
                      <TableCell className="text-right font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                        {r.rows}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Panel>
            <Row label="compact rows">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Field</TableHead>
                    <TableHead>Type</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow density="compact">
                    <TableCell className="font-mono">session_source</TableCell>
                    <TableCell>string</TableCell>
                  </TableRow>
                  <TableRow density="compact">
                    <TableCell className="font-mono">sessions</TableCell>
                    <TableCell>integer</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </Row>
          </Section>

          {/* ------------------------------------------- Day-by-day breakdown */}
          <Section
            title="Day-by-day breakdown"
            source="epic-58 contract — a 52px compact row, a status pill that never takes two lines, and no column leaving the frame at 1128px (the main column at 1440: 1440 − 248 rail − 64 margin, from the shell chrome as it stood in application.css; the live geometry is shell/ApplicationShell.tsx l.86 and l.101 and is 24px narrower). Measured by scripts/measure_component_sheet.py."
          >
            {/* WITH the re-collection column — story 58.4, arbitrage 5. The
                column only exists when a handler does, so a sheet mounted
                without one would measure a grid one column narrower than the
                shipped screen and report that it fits. The handler does nothing
                here: this sheet is an instrument bench, not a screen.

                AND WITH THE SELECTION — amendment of 2026-08-18. The bulk
                re-collection puts an 18px checkbox INSIDE the date cell rather
                than in an eighth column, precisely because 58.4 measured what an
                eighth column does to this grid at 1128px. That decision is only
                proved by measuring it, so the sheet mounts the shape that
                ships: without this second handler the checkbox is absent here
                and present on the screen. */}
            <DateBreakdownGrid
              breakdown={DAY_GRID}
              onRepullDay={() => undefined}
              onRepullDays={() => undefined}
            />
          </Section>

          {/* ------------------------------------------- Mapping bindings table */}
          <Section
            title="Mapping bindings — the table a person actually works in"
            source="Findings D-1 and D-2 of the 2026-08-12 visual review — no column leaving the frame at 1128px, and no control clipped inside its own cell. Every reading varies on these four rows, so the table is measured at its widest. Measured by scripts/measure_component_sheet.py."
          >
            <Row label="as it ships">
              <div className="w-full" data-testid="mapping-bindings-sheet">
                <MappingBindings rows={MAPPING_ROWS_FOLDED} />
              </div>
            </Row>
            <Row label="every reading varies">
              <div className="w-full" data-testid="mapping-bindings-widest">
                <MappingBindings rows={MAPPING_ROWS} />
              </div>
            </Row>
          </Section>

          {/* ------------------------------------------------- Panel & headers */}
          <Section
            title="Panel, headers, metric, empty state"
            source="application-v3.css l.275 (panel, radius 16px) · l.262 (panel header band, 58px, divider) · l.317 (metric, 92px, value 24px Geist tabular) · l.241 (page header, h1 30px)."
          >
            <div className="grid grid-cols-2 gap-5">
              <Panel flush>
                <PanelHeader
                  title="Coverage"
                  description="Panel header band — 58px, divider, actions on the trailing edge."
                  actions={<Button size="sm" variant="ghost">Details</Button>}
                />
                <div className="flex divide-x divide-divider-base">
                  <Metric label="Datastreams" value="11" hint="3 need attention" />
                  <Metric label="Rows today" value="1 687 020" hint="+4.2%" />
                </div>
              </Panel>
              <Panel flush>
                <PanelHeader title="Renders" description="The empty state, honest." />
                <EmptyState
                  title="No render kept yet"
                  description="A render is kept when you save a card from a conversation. Nothing has been saved for this project."
                  action={<Button size="sm">Open Analyze</Button>}
                />
              </Panel>
            </div>
          </Section>

          {/* ------------------------------------------------------------ Form */}
          <Section
            title="Form — field, input, select, checkbox, radio, switch"
            source="key-datastreams.html .search — 44px · radius 12px · padding 0 14px. Label 13px/700. Checkbox, radio and switch: no mockup draws them bare; see each file's header. Switch and progress fills are a call-site choice — rose for a control you operate, a tone for a state it reports (Jean, 2026-07-29)."
          >
            <div className="grid grid-cols-2 gap-5">
              <Field label="Project name" hint="Shown in the project switcher." required>
                {(p) => <Input {...p} defaultValue="Acme — EU" />}
              </Field>
              <Field label="Reporting timezone" error="Pick a timezone before publishing.">
                {(p) => (
                  <Select>
                    <SelectTrigger {...p}>
                      <SelectValue placeholder="Select a timezone" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="utc">UTC</SelectItem>
                      <SelectItem value="paris">Europe/Paris</SelectItem>
                      <SelectItem value="ny">America/New_York</SelectItem>
                    </SelectContent>
                  </Select>
                )}
              </Field>
              <Field label="Search" hint="The toolbar field, at its measured 44px.">
                {(p) => (
                  <div className="relative">
                    <SearchIcon className="pointer-events-none absolute top-1/2 left-3.5 size-5 -translate-y-1/2 text-text-secondary" />
                    <Input {...p} className="pl-11" placeholder="Filter datastreams" />
                  </div>
                )}
              </Field>
              <Field label="Note" hint="Several lines.">
                {(p) => <Textarea {...p} placeholder="Why this exception was granted" />}
              </Field>
            </div>
            <Row label="checkbox">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="sheet-cb"
                  checked={checked}
                  onCheckedChange={(v) => setChecked(v === true)}
                />
                <Label htmlFor="sheet-cb">Include partial days</Label>
              </div>
              <div className="flex items-center gap-2">
                <Checkbox id="sheet-cb-off" />
                <Label htmlFor="sheet-cb-off">Unchecked</Label>
              </div>
              <div className="flex items-center gap-2">
                <Checkbox id="sheet-cb-dis" disabled />
                <Label htmlFor="sheet-cb-dis">Disabled</Label>
              </div>
            </Row>
            <Row label="radio">
              <RadioGroup
                value={radio}
                onValueChange={setRadio}
                className="grid-flow-col items-center gap-5"
              >
                {["hourly", "daily", "weekly"].map((v) => (
                  <div key={v} className="flex items-center gap-2">
                    <RadioGroupItem value={v} id={`sheet-r-${v}`} />
                    <Label htmlFor={`sheet-r-${v}`}>{v}</Label>
                  </div>
                ))}
              </RadioGroup>
            </Row>
            <Row label="switch">
              <div className="flex items-center gap-2">
                <Switch id="sheet-sw" checked={switched} onCheckedChange={setSwitched} />
                <Label htmlFor="sheet-sw">Country split</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch id="sheet-sw-sm" size="sm" defaultChecked={false} />
                <Label htmlFor="sheet-sw-sm">Small</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch id="sheet-sw-dis" disabled />
                <Label htmlFor="sheet-sw-dis">Disabled</Label>
              </div>
            </Row>
            {/* The fill is a choice, not a hardcode: rose says "you operate
                this", a tone says "this is its state". Both, side by side. */}
            <Row label="switch · fill">
              {FILLS.map((tone) => (
                <div key={tone} className="flex items-center gap-2">
                  <Switch defaultChecked tone={tone} aria-label={tone} />
                  <span className="text-caption text-text-secondary">{tone}</span>
                </div>
              ))}
            </Row>
            <Row label="progress · fill">
              <div className="grid w-full max-w-[560px] gap-2.5">
                {FILLS.map((tone, i) => (
                  <div key={tone} className="flex items-center gap-3">
                    <span className="w-14 shrink-0 font-mono text-caption text-text-secondary">
                      {tone}
                    </span>
                    {/* A gallery bar measures nothing: its subject IS the tone
                        being demonstrated, and the name says so rather than
                        pretending to report on some object. */}
                    <Progress
                      value={40 + i * 10}
                      tone={tone}
                      className="flex-1"
                      aria-label={`Demonstration bar, ${tone} fill`}
                    />
                    <span className="w-10 shrink-0 text-right font-numeric text-caption text-text-secondary">
                      {40 + i * 10}%
                    </span>
                  </div>
                ))}
              </div>
            </Row>
            {/* `Progress` answers HOW FAR. When there is no number, the console
                had nothing — so fourteen files imported MUI's CircularProgress
                and one shipped a `role="progressbar"` carrying no value. */}
            <Row label="spinner · size">
              <Spinner size="inline" label="Loading, inline" />
              <Spinner label="Loading, default" />
              <Spinner size="page" label="Loading the panel" />
              <Spinner size="page" tone="warning" label="Retrying the run" showLabel />
            </Row>
            {/* And `Skeleton` answers a third question the other two cannot:
                WHAT SHAPE is coming. It holds the layout so the page does not
                jump when the data lands — which is the thing a reader notices
                most. Only honest where the shape is known in advance; for a list
                that may turn out empty, `EmptyState` is the truthful answer. */}
            <Row label="skeleton · the shape, before the data">
              <div className="grid w-full gap-2" aria-busy>
                <Skeleton className="h-4 w-1/3" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-2/3" />
              </div>
            </Row>
          </Section>

          {/* ---------------------------------------------------------- Separator */}
          <Section
            title="Separator — one hairline, and no options"
            source="No mockup draws a rule on its own; it is the same --color-divider-base every table row and panel edge already uses, so a rule inside a card and the card's own border stay one decision."
          >
            {/* Eight screens imported MUI's `Divider` for this. A bare <hr> would
                have drawn it — the reason this is a component is that a rule
                between two labelled blocks is DECORATIVE, and an <hr> is
                announced as a thematic break. This one emits role="none". */}
            <Row label="separator · horizontal">
              <div className="grid w-full gap-3">
                <span className="text-ui">Above</span>
                <Separator />
                <span className="text-ui">Below</span>
              </div>
            </Row>
            <Row label="separator · vertical">
              <div className="flex h-8 items-center gap-3">
                <span className="text-ui">Left</span>
                <Separator orientation="vertical" />
                <span className="text-ui">Right</span>
              </div>
            </Row>
          </Section>

          {/* ------------------------------------------------------- Side panels */}
          <Section
            title="Sheet — the side panel, in its two shapes"
            source="datastream-recovery.html `.recovery-drawer` (384px, in the flow, 1px left rule). The modal shape has no mockup — see the file header."
          >
            {/* The distinction is the reason this primitive exists. A modal
                Sheet dims the page; the mockups' drawer sits BESIDE live
                content and must not. No library ships the second one, which is
                why ten screens wrote their own. */}
            <Row label="inline · in the flow, no overlay">
              <div className="flex h-64 w-full max-w-[720px] overflow-hidden rounded-card border border-divider-base">
                <div className="min-w-0 flex-1 p-4 text-ui text-text-secondary">
                  The table stays live and operable beside the panel. No overlay,
                  no focus trap — trapping focus here would make this column
                  unreachable by keyboard.
                </div>
                <SheetInline
                  title="Run diagnosis"
                  description="2026-08-02 · nightly"
                  onClose={() => undefined}
                  className="w-72"
                >
                  <p className="text-ui text-text-secondary">
                    Evidence you consult while working.
                  </p>
                </SheetInline>
              </div>
            </Row>
            <Row label="modal · finish or abandon">
              <Sheet>
                <SheetTrigger asChild>
                  <Button variant="secondary">Open the modal sheet</Button>
                </SheetTrigger>
                <SheetContent>
                  <SheetHeader>
                    <SheetTitle>Field detail</SheetTitle>
                    <SheetDescription>
                      Dims the page and traps focus, because this is a task you
                      finish or abandon.
                    </SheetDescription>
                  </SheetHeader>
                  <SheetBody className="text-ui text-text-secondary">
                    Body scrolls; the header and footer do not.
                  </SheetBody>
                  <SheetFooter>
                    <Button variant="secondary">Cancel</Button>
                    <Button>Confirm</Button>
                  </SheetFooter>
                </SheetContent>
              </Sheet>
            </Row>
          </Section>

          {/* ------------------------------------------------ Navigation, menus */}
          <Section
            title="Tabs, breadcrumb, dropdown, dialog, tooltip, accordion"
            source="application-v3.css l.403-431 (tabs: 52px band, 3px rose mark) · l.203 (breadcrumb 13px) · application-v4.css l.41-74 (menu: radius 14px, rows 42px, overlay shadow). Dialog and tooltip: no mockup — see their headers."
          >
            <Tabs defaultValue="overview">
              <TabsList>
                <TabsTrigger value="overview">Overview</TabsTrigger>
                <TabsTrigger value="data">Data</TabsTrigger>
                <TabsTrigger value="mapping">Mapping</TabsTrigger>
                <TabsTrigger value="runs" disabled>
                  Runs
                </TabsTrigger>
              </TabsList>
              <TabsContent value="overview" className="pt-4 text-ui text-text-secondary">
                The active tab is ink with a 3px rose mark sitting on the divider.
              </TabsContent>
              <TabsContent value="data" className="pt-4 text-ui text-text-secondary">
                Data.
              </TabsContent>
              <TabsContent value="mapping" className="pt-4 text-ui text-text-secondary">
                Mapping.
              </TabsContent>
            </Tabs>
            <Row label="segmented">
              <Tabs defaultValue="all">
                <TabsList variant="segmented">
                  <TabsTrigger value="all">All</TabsTrigger>
                  <TabsTrigger value="attention">Needs attention</TabsTrigger>
                </TabsList>
              </Tabs>
            </Row>
            <Row label="breadcrumb">
              <Breadcrumb>
                <BreadcrumbList>
                  <BreadcrumbItem>
                    <BreadcrumbLink href="#">Acme</BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbLink href="#">Datastreams</BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator />
                  <BreadcrumbItem>
                    <BreadcrumbPage>GA4 — web</BreadcrumbPage>
                  </BreadcrumbItem>
                </BreadcrumbList>
              </Breadcrumb>
            </Row>
            <Row label="dropdown / dialog / tooltip">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="secondary">
                    Actions
                    <ChevronDownIcon />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-72">
                  <DropdownMenuLabel>Datastream</DropdownMenuLabel>
                  <DropdownMenuItem>Open</DropdownMenuItem>
                  <DropdownMenuItem>Recover an interval</DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem variant="destructive">
                    <TrashIcon />
                    Archive
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>

              <Dialog>
                <DialogTrigger asChild>
                  <Button variant="secondary">Open dialog</Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Archive this datastream?</DialogTitle>
                    <DialogDescription>
                      Collection stops. Published data stays readable, and the last publication
                      is not rolled back.
                    </DialogDescription>
                  </DialogHeader>
                  <Field label="Type the datastream name to confirm" required>
                    {(p) => <Input {...p} placeholder="GA4 — web" />}
                  </Field>
                  <DialogFooter>
                    <DialogClose asChild>
                      <Button variant="secondary">Cancel</Button>
                    </DialogClose>
                    <Button variant="destructive">Archive</Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>

              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost">Hover me</Button>
                </TooltipTrigger>
                <TooltipContent>
                  Published through is the last date every source in this datastream has
                  confirmed.
                </TooltipContent>
              </Tooltip>
            </Row>
            <Accordion type="single" collapsible className="w-full">
              {["Why is this partial?", "What changed on 2026-07-26?"].map((q) => (
                <AccordionItem key={q} value={q}>
                  <AccordionTrigger>{q}</AccordionTrigger>
                  <AccordionContent>
                    One source has not confirmed the last two days, so the interval is reported
                    as partial rather than published.
                  </AccordionContent>
                </AccordionItem>
              ))}
            </Accordion>
          </Section>

          {/* --------------------------------------------------------- Stepper */}
          <Section
            title="PhaseTimeline — the ordered sequence, each phase evidenced"
            source="mockups/first-publication.html — 78px rows, 16px mark with a 4px container ring, 2px connector, evidence and time per phase. VERTICAL: DESIGN.md l.152 requires publication to reach a terminal state independently of the backfill behind it, which a horizontal rail cannot say."
          >
            <PhaseTimeline
              phases={[
                { label: "Authorization verified", evidence: "Acme Ads · account access confirmed", at: "09:02", state: "done" },
                { label: "Recent extraction", evidence: "1–21 Jul · 186 420 rows", at: "09:06", state: "done" },
                { label: "Validate and map", evidence: "8 checks passed · mapping_v17", at: "09:08", state: "done" },
                { label: "Publish recent data", evidence: "Available to UI, API and MCP", at: "09:10", state: "done" },
                {
                  label: "Historical backfill",
                  evidence: "Jan–Jun 2026 · 31% complete",
                  at: "Running",
                  state: "running",
                  percent: 31,
                },
              ]}
            />
            <PhaseTimeline
              phases={[
                { label: "Authorization verified", evidence: "Acme Ads", at: "09:02", state: "done" },
                { label: "Recent extraction", evidence: "Rate limit — 2 retries left", at: "09:14", state: "failed" },
                { label: "Validate and map", state: "todo" },
              ]}
            />
          </Section>

          {/* ------------------------------------------- Choice, log, wizard */}
          <Section
            title="ChoiceGroup — a select whose options you can read"
            source="pill: application-v3.css l.209-234, the same 40px/999px/13px-700 as every control. card: .source-choice in mockups/datastream-create.html — 16px radius, rose edge and 3px rose halo when selected. A RadioGroup underneath, so arrows move and a reader hears '2 of 4'."
          >
            <Row label="pill">
              <ChoiceGroup
                aria-label="Grain"
                defaultValue="daily"
                choices={[
                  { value: "daily", label: "Daily" },
                  { value: "weekly", label: "Weekly" },
                  { value: "monthly", label: "Monthly" },
                ]}
                overflow={
                  <Button variant="secondary" size="icon" aria-label="More grains">
                    <MoreHorizontalIcon />
                  </Button>
                }
              />
            </Row>
            <Row label="pill · with hints">
              <ChoiceGroup
                aria-label="Reconciliation"
                defaultValue="source"
                choices={[
                  { value: "source", label: "Source of truth", hint: "One source wins" },
                  { value: "sum", label: "Sum", hint: "Add every source" },
                  { value: "max", label: "Maximum", hint: "Keep the highest" },
                ]}
              />
            </Row>
            <ChoiceGroup
              variant="card"
              aria-label="Where data comes from"
              defaultValue="connector"
              choices={[
                {
                  value: "connector",
                  label: "Connector report",
                  hint: "Select a provider report, fields, grain, history and supported schedule.",
                },
                {
                  value: "bigquery",
                  label: "Existing BigQuery",
                  hint: "Reference a read-only table or view while its external writer stays authoritative.",
                },
              ]}
            />
          </Section>

          <Section
            title="ActivityLog — what happened, in order"
            source="Dashed connector and open rings, on purpose: a solid line says 'these are the steps of one thing', a dashed line says 'time passed, and there may be more'. A log is never complete. Entries carry links inside."
          >
            <ActivityLog
              entries={[
                {
                  children: (<>Jane Studio answered the form <strong>Graphic design brief</strong></>),
                  at: "2026-07-28 19:15",
                },
                {
                  children: (<>Joe Studio sent <strong>Graphic design brief</strong> to Jane Studio.</>),
                  at: "2026-07-28 19:11",
                  action: <a href="#activity-log">View email</a>,
                },
                {
                  children: (<>Publication <strong>2026-07-21 → 2026-07-27</strong> failed on a rate limit.</>),
                  at: "2026-07-28 18:53",
                  tone: "error",
                  action: <a href="#activity-log">Open run</a>,
                },
                {
                  children: (<>Joe Doe created the datastream <strong>GA4 — web</strong>.</>),
                  at: "2026-07-28 18:51",
                  tone: "success",
                },
              ]}
            />
          </Section>

          <Section
            title="Stepper — the wizard rail"
            source="docs/product-architecture/datastream-workbench-and-wizard.md l.31 contracts a PERSISTENT LEFT stepper with revisitable completed steps and Automatic sections. Jean's reference is horizontal; both are here, vertical is the default, and the divergence is open."
          >
            <div className="grid grid-cols-[260px_minmax(0,1fr)] gap-8">
              <Stepper
                steps={[
                  { label: "Source", detail: "Where data comes from", state: "done", onSelect: () => {} },
                  { label: "Access", detail: "Connection and scope", state: "done", onSelect: () => {} },
                  { label: "Report", detail: "Fields, grain, history", state: "current" },
                  { label: "Schedule", detail: "Cadence and window", state: "todo" },
                  { label: "Mapping", detail: "Derived from the report", state: "todo", automatic: true },
                  { label: "Preview and validate", detail: "Sample, gates, cost", state: "todo" },
                ]}
              />
              <div className="text-ui text-text-secondary">
                The central task area. The rail stays on the left through every section of the
                flow; a completed step is clickable, and an <em>Automatic</em> section stays
                visible as evidence without becoming a stop. The steps above are this sheet's own
                example — the Datastream wizard carries five, and none of them is Automatic.
              </div>
            </div>
            <Row label="horizontal">
              <Stepper
                orientation="horizontal"
                className="w-full"
                steps={[
                  { label: "Investment details", detail: "Set your plan.", state: "current" },
                  { label: "Payment setup", detail: "Configure and automate.", state: "todo" },
                  { label: "Goal tracking", detail: "Track and reach it.", state: "todo" },
                ]}
              />
            </Row>
          </Section>

          {/* -------------------------------------------------------- Calendar */}
          <Section
            title="Calendar — and the date field that opens it"
            source="No mockup draws a calendar; the header of calendar.tsx says so. Built from the rules that do apply: 36px cells against the 44px control rhythm, the 10px menu-row radius on a day, the accent on the selected date, numerals in the numeric font with tabular figures, and the table's column-header treatment on the weekday row."
          >
            <div className="flex flex-wrap items-start gap-8">
              <Calendar
                mode="single"
                defaultMonth={new Date(2026, 6, 1)}
                selected={new Date(2026, 6, 21)}
                className="rounded-large border border-divider-base"
              />
              <div className="flex flex-col gap-3">
                <Popover>
                  <PopoverTrigger asChild>
                    <Button variant="secondary">
                      <CalendarIcon />
                      21 Jul 2026
                      <ChevronDownIcon />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent align="start" className="w-auto">
                    <Calendar mode="single" defaultMonth={new Date(2026, 6, 1)} />
                  </PopoverContent>
                </Popover>
                <p className="m-0 max-w-[38ch] text-caption text-text-secondary">
                  Recovery picks an interval, coverage reads a calendar, an import names a
                  window and a schedule names a start — four date pickers avoided.
                </p>
              </div>
            </div>
          </Section>

          {/* ------------------------------------------------ CoverageCalendar */}
          <Section
            title="CoverageBars — where the data is, and asking for it again"
            source="One bar per day, not a month grid: the question is where the gaps are, and a continuous run answers it in a glance. The six-status vocabulary is the ledger's own (core/extract_ledger.py), verbatim from CoverageStrip — `empty` is an answer, `never_fetched` is an absence. Click a day, then a second to take the span; the footer says how many can ACTUALLY be re-collected."
          >
            <CoverageBars
              days={COVERAGE_DAYS}
              onRepair={(dates) =>
                notify(`Re-collecting ${dates.length} day${dates.length > 1 ? "s" : ""}`, {
                  tone: "info",
                  description: dates.slice(0, 3).join(", ") + (dates.length > 3 ? " …" : ""),
                })
              }
            />
          </Section>

          {/* --------------------------------------------------------- Cluster */}
          <Section
            title="Avatar"
            source="application-v3.css l.176 — 36px, radius 10px, lavender, initials at 13px/700. No image loading."
          >
            <Cluster>
              <Avatar size="sm">
                <AvatarFallback>JA</AvatarFallback>
              </Avatar>
              <Avatar>
                <AvatarFallback>JA</AvatarFallback>
              </Avatar>
              <Avatar size="lg">
                <AvatarFallback>JA</AvatarFallback>
              </Avatar>
            </Cluster>
          </Section>
        </div>
        <Toaster />
      </div>
    </TooltipProvider>
  );
}
