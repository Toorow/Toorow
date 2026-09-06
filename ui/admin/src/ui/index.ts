/**
 * The console's visual vocabulary.
 *
 * Two origins, one entry point. `../components/ui/*` holds the shadcn source,
 * installed by its CLI and then adapted — those files are ours to edit, which
 * is the point of the registry model. `./ *` holds what shadcn has no
 * counterpart for: the panel frame, the metric, the empty state, `Status`
 * (which unifies what would otherwise be a separate Alert and Signal), and the
 * `Field` wiring.
 *
 * Screens import from here, never from either directory, so a component can be
 * rebased from one to the other without touching 46 files.
 *
 * Every component below has been compared to its validated v3/v4 mockup and
 * edited in place; each file's header quotes the rule it was measured from, or
 * says plainly that no mockup covers it. Nothing here still carries the
 * registry's default geometry.
 */

// --- the tone scale, read by everything that carries state ------------------
// `Tone` is the five states. `Fill` adds `accent` for a solid bar or track,
// where the rose says "you operate this" and a tone says "this is its state" —
// the switch and the progress meter need both.
export {
  TONES, FILLS, TONE_TEXT, TONE_FILL, TONE_CONTAINER, TONE_BORDER,
  TONE_SURFACE, TONE_SURFACE_HOVER, TONE_DOT, TONE_EDGE_VARS,
  EDGE_BACKGROUND, EDGE_CLASS, HALO,
  FILL_BG, FILL_GRADIENT, FILL_ON, FILL_BG_CHECKED,
  DESTRUCTIVE_ROW_ACTION, INVALID, INVALID_BORDER, INVALID_MESSAGE,
} from "./tone";
export type { Tone, Fill } from "./tone";

// --- the state vocabulary, read by everything that renders a server word ----
// `tone.ts` says what a warning looks like; this says which words ARE warnings,
// and how each one is spelled to a person. Six screens held their own copy of
// that answer before it lived here. Since 76-2 the two words those six disagreed
// about are arbitrated in the file — `blocked` warning, `unavailable` neutral,
// with `not_offered` for the fact Analyze actually wanted to colour — and BOTH
// functions take one argument: there is no `overrides` to pass any more.
export {
  STATE_TONE, STATE_LABEL, stateTone, stateLabel,
  UNKNOWN_STATE_TONE, UNKNOWN_STATE_LABEL,
} from "./stateVocabulary";
// The key a screen owes its reader once it draws three tones at once
// (`console-presentation.md` §3). The screen passes the meanings, because a
// warning means something different on Governance and on Test › Runs; this
// component owns the mark, its shape and its order.
export { StatusLegend } from "./StatusLegend";
export type { StatusLegendEntry, StatusLegendProps } from "./StatusLegend";
// One panel width, one declaration — `console-presentation.md` §6. They are
// class strings and not numbers, for the reason `layout.ts` writes down: a
// width composed at runtime is a rule Tailwind never emits.
export {
  DRAWER_WIDTH, WIDE_DRAWER_WIDTH, WORKBENCH_GRID_MAX,
  WIZARD_GRID, WIZARD_ASIDE_STICKY,
} from "./layout";

// --- a column header that sorts, and announces the order it carries ---------
// `aria-sort` was absent from the whole console. `SortableHead` composes
// `TableHead` rather than replacing it, so a table that does not sort is
// unchanged. `PAGE_SORT_NOTE` is the sentence a paged collection owes the
// reader: these rows are the page, not the collection.
export {
  SortableHead, SortScopeNote, useTableSort, sortRows, nextSort,
  compareSortValues, PAGE_SORT_NOTE,
} from "./SortableHeader";
export type { SortDirection, SortState, SortValue, SortableHeadProps } from "./SortableHeader";

// --- the other half of a collection too long to read at once ----------------
// `SortableHead` says which column carries the order; `Pager` is the way to the
// rest of the collection. Three screens held three of them — a footer, a bare
// flex row and the Explorer's four-button pivot bar — with three different
// answers to "what does a direction that leads nowhere look like". It fetches
// nothing: `unit` spells the buttons, so one Pager per axis is how the pivot
// pages rows and columns without this file knowing that axes exist.
export { Pager } from "./Pager";
export type { PagerProps, PagerStep } from "./Pager";

// --- the three things a screen says while it has no answer ------------------
// Reading, unreadable, and no Project chosen. Written for the Analyze artifact
// surfaces, and about no artifact: every collection passes through all three.
export { Loading, Failure, NoScope, ObjectNotFound, ProjectNotFound, Retry } from "./AsyncStates";

// --- ours: no shadcn counterpart ------------------------------------------
export { Panel, PanelHeader, PanelBody, PanelContent, PageFrame, PageHeader, SectionHeader, Stack, Cluster } from "./Surface";
export type { PanelProps, PanelHeaderProps, PanelBodyProps, PageHeaderProps, SectionHeaderProps } from "./Surface";
export { Status, Metric, EmptyState, TableScroll, ObjectId } from "./Data";
export { Alert } from "./Alert";
export type { AlertProps } from "./Alert";
export { ConfirmDialog } from "./ConfirmDialog";
export type { ConfirmDialogProps } from "./ConfirmDialog";
// One number, one format — `console-presentation.md` §2. This module and
// `Timestamp` below are the ONLY two files in `ui/admin/src` allowed to call
// `toLocaleString` or `Intl.*`; `__tests__/FormattingIsCentral.test.ts` greps
// for the rest. `NO_VALUE` is the dash a cell shows instead of nothing, and it
// is the same character `NO_TIMESTAMP` has always been.
export {
  CONSOLE_LOCALE, NO_VALUE, THIN_SPACE, MINUS_SIGN,
  formatNumber, formatCompact, formatPercent, formatCurrency,
  formatBytes, formatDuration, formatCount, percentValue, formatDayOffset,
} from "./format";
export type { NumericValue } from "./format";
// One word per concept — `console-presentation.md` §4. `format.ts` above holds
// the one number; this holds the one noun. The five entries are the §4 table and
// the retired spellings beside them, so a column header, a tab label or an
// empty-state title that names one of these concepts reads it from here.
// `__tests__/glossary.test.ts` asserts both halves: the module, and the screens.
export {
  GLOSSARY, GLOSSARY_CONCEPTS, REFUSED_ON_SCREEN, RETIRED_SPELLING, noun, wireWord,
} from "./glossary";
export type { GlossaryConcept, GlossaryEntry } from "./glossary";
// One instant, one rendering. `formatTimestamp` is the same decision for the
// places that can only produce a string — a `title`, a `Metric` value, a
// sentence being composed. `formatDate` and `formatClock` are the two grains
// below it — a date with no time, a clock with no date — and `formatRelative`
// is the reading a person does in their head ("4 min ago"), which §2 asks for
// once rather than in each screen's own template string.
export {
  Timestamp, formatTimestamp, formatRelative, formatDate, formatClock,
  NO_TIMESTAMP, NO_TIMESTAMP_MEANING,
} from "./Timestamp";
export type { TimestampProps, TimestampValue } from "./Timestamp";
// A clipboard write that reports its outcome — including the refusal six of
// the console's seven copy flows used to declare a success.
export { CopyButton } from "./CopyButton";
export type { CopyButtonProps } from "./CopyButton";
// A governed record read field by field. One vocabulary for both the Data
// workbenches and the Datastream confirmation reviews — the two places where
// `JSON.stringify` inside a `<pre>` has already been found once each.
export {
  EvidenceRows,
  evidenceRows,
  displayValue,
  fieldMeaning,
  shortenOpaque,
  summarizeObject,
  label,
} from "./Evidence";
export type { EvidenceRow } from "./Evidence";
export { PhaseTimeline } from "./PhaseTimeline";
export type { Phase } from "./PhaseTimeline";
// Three objects that look alike and mean different things — the header of each
// file says which is which, because picking the wrong one is the easiest
// mistake in this part of the library.
//   Stepper       you are doing this        (wizard rail, revisitable)
//   PhaseTimeline the system did this       (evidence, a time per phase)
//   ActivityLog   these things happened     (open-ended, dashed, never pending)
export { Stepper } from "./Stepper";
export type { Step } from "./Stepper";
export { ActivityLog } from "./ActivityLog";
export type { ActivityEntry } from "./ActivityLog";
export { ChoiceGroup } from "./ChoiceGroup";
export type { Choice } from "./ChoiceGroup";
// Composition by hand — the Builder's binding wells and the Explorer's
// Composition region are the same gesture, so they are one component. The
// keyboard path is inside it, not beside it (`visualization-and-rendering.md`,
// *Composition is direct manipulation*).
export { FieldComposer, DraggableField, FieldWell, FieldShelf, useCarriedField } from "./FieldWells";
export type { WellField, WellSpec, WellChange, WellBindings } from "./FieldWells";
export { ReferenceSelect } from "./ReferenceSelect";
export type { ReferenceItem, ReferenceSelectProps } from "./ReferenceSelect";
// ONE vocabulary for the state of a day, and it lives here (story 58.2,
// arbitrage 11). The strip renders it as a mark, the day-by-day grid as a pill;
// two tables of the same six words would be one fact under two spellings.
// `JOB_STATE_LABEL` is the second, separate fact — what the collecting WINDOW
// did — because `cancelled` and `superseded` both report `never_fetched`.
export {
  CoverageBars, REPAIRABLE, MEASURED_PER_WINDOW, NOT_VERIFIED, rowCountNote,
  rowCountAbsence, EXTRACT_STATUS_LABEL, EXTRACT_STATUS_TONE, extractStatusLabel,
  extractStatusTone, JOB_STATE_LABEL, jobStateLabel,
  // Story 58.4: the shape that separates two states sharing one tone, and the
  // sentence that names what a day is missing.
  EXTRACT_STATUS_SHAPE, extractStatusShape, extractGapSentence,
  EXTRACT_STATUS_MARK, extractStatusMark,
  // AI-307: the same distinction as a WORD, for the surfaces that count days
  // instead of naming one, and the predicate both of them read.
  extractGapLabel, isPrevented,
  // AI-307, second pass: the KEY of what a day says, and the order, the word,
  // the tone and the shape that hang off it. Every surface that groups, filters,
  // colours or spells a day asks for the key -- reading `extract_status` alone
  // is what let one page count « Never requested · 3 » beside a confirmation
  // saying « 2 Prevented; 1 Never requested ».
  PREVENTED_GAP_KEY, GAP_KEY_ORDER, extractGapKey, gapKeyLabel,
  extractGapTone, extractGapShape, JOB_STATE_UNKNOWN,
} from "./CoverageBars";
export type { ExtractMark, GapDay } from "./CoverageBars";
export type { CoverageDay, CoverageStatus, JobState } from "./CoverageBars";
// Capability coverage is a different object from CoverageBars: that one is a
// calendar of days, this one is a denominator over Datastreams.
export {
  CapabilityCoverage, CapabilityImpactMatrix, CoverageStateBadge,
  COVERAGE_STATES, CAPABILITY_LABELS, capabilityLabel,
} from "./CapabilityCoverage";
export type {
  CoverageCounts, CoverageState, ImpactMatrixRow, CapabilityCoverageProps,
} from "./CapabilityCoverage";

// A different shape again: two-dimensional, entity x Datastream. The impact
// matrix above is one row per (capability, Datastream) pair and cannot express
// it (Story 48.5).
export { EntityMatrix } from "./EntityMatrix";
// The state vocabulary travels with the matrix: the Competitor workbench's
// Coverage tab shows the same states one entity at a time, and a second map
// beside this one is how two screens disagree about what `candidate` looks like.
export { bindingStateGlyph, bindingStateLabel, bindingStateTone } from "./EntityMatrix";
export type {
  BindingState, MatrixCell, MatrixDatastream, MatrixEntity,
} from "./EntityMatrix";

export { ConnectorMark, connectorSrc, connectorName } from "./ConnectorMark";
export { ObjectHeader, NavTabs } from "./ObjectNav";
export type { NavTab } from "./ObjectNav";
// The rail's own vocabulary. Separate from `Button` on purpose: a button is a
// bordered pill by contract, a navigation row is a soft ground and a rose mark.
export { NavItem, NavSubItem, NavBranch } from "./NavItem";
export type { NavItemProps } from "./NavItem";
export { Field } from "./Form";
export type { FieldProps } from "./Form";

// --- shadcn, adapted -------------------------------------------------------
export { Button, buttonVariants } from "../components/ui/button";
export { Badge, badgeVariants } from "../components/ui/badge";
export {
  Table, TableHeader, TableBody, TableFooter, TableHead, TableRow, TableCell,
  TableCaption, tableRowVariants,
} from "../components/ui/table";
// The two the library was missing, added 2026-08-04 while migrating the last MUI
// screens: `Divider` had eight callers and `Skeleton` one, and both were reasons
// to keep importing a whole design system for a grey rectangle.
export { Separator } from "../components/ui/separator";
export { Skeleton } from "../components/ui/skeleton";
export {
  Dialog, DialogTrigger, DialogContent, DialogHeader, DialogFooter, DialogTitle,
  DialogDescription, DialogClose, DialogOverlay, DialogPortal,
} from "../components/ui/dialog";
export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from "../components/ui/tooltip";
export {
  Select, SelectTrigger, SelectValue, SelectContent, SelectItem, SelectGroup,
  SelectLabel, SelectSeparator,
} from "../components/ui/select";
export { Input, NativeSelect, Textarea } from "../components/ui/input";
export { Label } from "../components/ui/label";
export { Checkbox } from "../components/ui/checkbox";
export { RadioGroup, RadioGroupItem } from "../components/ui/radio-group";
export { Collapsible, CollapsibleTrigger, CollapsibleContent } from "../components/ui/collapsible";
export { ScrollArea, ScrollBar } from "../components/ui/scroll-area";
export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants } from "../components/ui/tabs";
export { Accordion, AccordionItem, AccordionTrigger, AccordionContent } from "../components/ui/accordion";
export {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuCheckboxItem, DropdownMenuRadioGroup, DropdownMenuRadioItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuShortcut, DropdownMenuGroup,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuPortal,
} from "../components/ui/dropdown-menu";
export {
  Breadcrumb, BreadcrumbList, BreadcrumbItem, BreadcrumbLink, BreadcrumbPage,
  BreadcrumbSeparator, BreadcrumbEllipsis,
} from "../components/ui/breadcrumb";
export { Switch } from "../components/ui/switch";
export { Avatar, AvatarImage, AvatarFallback, AvatarBadge, AvatarGroup, AvatarGroupCount } from "../components/ui/avatar";
export { Progress } from "../components/ui/progress";
// `Progress` says how far. `Spinner` says something is happening and there is no
// number — the case fourteen files solved with MUI's `CircularProgress`, and one
// with a `role="progressbar"` carrying no value.
export { Spinner, spinnerVariants } from "../components/ui/spinner";
// The side panel, in the TWO shapes the product uses. `SheetContent` is modal —
// finish or abandon. `SheetInline` is the mockups' 384px `.recovery-drawer`,
// which sits IN the flow beside live content: no overlay, no focus trap, because
// trapping focus in a panel read alongside a table makes the table unreachable.
// No library ships the second one, which is why ten screens wrote their own.
export {
  Sheet, SheetTrigger, SheetContent, SheetInline, SheetHeader, SheetBody,
  SheetFooter, SheetTitle, SheetDescription, SheetClose, SheetOverlay, SheetPortal,
} from "../components/ui/sheet";
export { Calendar } from "../components/ui/calendar";
export { Popover, PopoverTrigger, PopoverContent, PopoverAnchor } from "../components/ui/popover";
export { Toaster, notify } from "../components/ui/sonner";
export type { NotifyOptions } from "../components/ui/sonner";
