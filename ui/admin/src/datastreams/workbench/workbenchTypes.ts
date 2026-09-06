import type { Tab } from "../../shell/pages/datastreamTabs";

export interface WorkbenchIdentity {
  datastream_id: string;
  project_id: string;
  name: string | null;
  mode: string;
  data_role: string | null;
  owner: string | null;
  module: string | null;
  /** Canonical connector binding, including managed-feed template identity. */
  connector?: string | null;
  source_account_ref: string | null;
  declared_writer: string | null;
  business_domains: Array<{ id: string; name: string; slug: string }>;
  /**
   * Declared delivery channels for a `managed_feed`. Part of "identity and
   * bindings", which the ratified Overview contract puts on that tab -- and the
   * only way the console can tell an inbound Datastream from an upload one.
   * Absent on older payloads, so every reader must tolerate `undefined`.
   */
  delivery_channels?: string[];
}

/**
 * What ONE role would do to this Datastream, resolved by the server.
 *
 * `source_type` is the value `fee_tax_source_types` derives from the PAIR
 * (manifest category, role); `in_cost_cascade` is whether that value is read by
 * the cost ladder at all. Both come from the server because the agreement table
 * is the ladder's own, and a console that re-spelled it would be a second
 * authority on which Datastreams the cascade contains.
 */
export interface DataRoleOption {
  value: string;
  source_type: string;
  in_cost_cascade: boolean;
  /** What choosing it changes, in one sentence. Never composed here. */
  effect: string;
  is_current: boolean;
}

/**
 * The governed role change — amendment 9 of the 2026-08-11 review.
 *
 * « Le rôle et le mode s'éditent depuis le Workbench, par le même changement
 * gouverné que le reste : un changement préparé, une confirmation qui nomme ce
 * qui bouge en aval, une version. »
 *
 * `expected_data_role` is the BASE: the value the screen read, sent back with
 * the change so a stale screen is refused instead of silently winning. It is
 * served rather than derived from `identity.data_role` on purpose — a base a
 * screen composed itself is not a claim about what was read.
 */
export interface DataRoleChange {
  current: string | null;
  expected_data_role: string;
  category: string | null;
  category_reason: string | null;
  source_type: string;
  in_cost_cascade: boolean;
  options: DataRoleOption[];
  /**
   * The other half of amendment 9, and it says what it is rather than nothing.
   * The mode and the connector are NOT editable: re-sourcing a Datastream is a
   * governed change of its own, and the screen names the gesture instead of
   * offering a control no writer executes.
   */
  mode_and_connector: {
    changeable: boolean;
    mode: string;
    connector: string | null;
    reason: string;
    gesture: string;
  };
}

export interface WorkbenchHeader {
  schema: "datastream_workbench.header.v1";
  identity: WorkbenchIdentity;
  /** Amendment 9. Absent on an older payload, so every reader tolerates it. */
  data_role_change?: DataRoleChange | null;
  axes: {
    lifecycle: string;
    configuration: string;
    operations: string;
    publication: string;
  };
  versions: {
    active_plan: string | null;
    active_plan_number?: number | null;
    active_mapping: string | null;
    active_mapping_number?: number | null;
    proposed_plan: string | null;
    proposed_plan_number?: number | null;
    /** The newest mapping VERSION that is not current. Not publishable on its
     *  own — see below. */
    proposed_mapping: string | null;
    proposed_mapping_number?: number | null;
    /** The `ready` mapping PROPOSAL, the only thing governed publication can
     *  consume (`mapping_proposals`, migration 071). Distinct from
     *  `proposed_mapping`, and not derivable from it. */
    ready_mapping_proposal?: string | null;
  };
  /** The schedule evidence the Operations axis is derived from.
   *
   *  `datastream_workbench.py:382` has emitted this since the axis was written,
   *  and this type never declared it — so no screen could read it without
   *  casting, and the header showed the axis word with no reason under it.
   *  `datastream-workbench-and-wizard.md:87` fixes the axis as derived from
   *  "schedule, freshness, coverage and latest run evidence"; these are the
   *  schedule three. */
  operations_evidence: {
    next_run_at: string | null;
    missed_run_count: number | null;
    schedule_state_known: boolean;
    late_reasons: string[];
  };
  /**
   * The tabs a CAPABILITY opens, with the state that opened or closed each —
   * story 58.6, amendment « Une capacité activée AJOUTE son onglet ».
   *
   * It travels on the header because every tab of the Workbench loads it, so the
   * band learns which tabs exist from the same request that says what the
   * Datastream is. Optional in the type: an older payload carries none, and
   * `undefined` means "no conditional tab is open" — never "all of them are".
   */
  capability_tabs?: Array<{
    capability_key: string;
    /** The tab this capability opens, or `null` when it opens none — story 58.9.
     *  Four of the six capabilities add no tab, and the header says so rather
     *  than leaving them out: `Overview` shows a row per capability, and a header
     *  carrying only the tab-opening ones would show two modules out of six. */
    tab: string | null;
    /** `optional` or `always_present`, from migration 131's pairing CHECK. The
     *  database REFUSES `disabled` on an `always_present` capability, so those
     *  two rows carry no switch — the control is what is forbidden, not the row. */
    availability?: string;
    state: string;
    open: boolean;
  }>;
  /**
   * The open, not-yet-reviewed data-quality issues of this Datastream — story
   * 59.2. It travels on the header every tab already loads, so the badge is
   * visible from the six tabs without a second read.
   *
   * OPTIONAL IN THE TYPE, AND THAT IS THE FOURTH STATE. An absent key means the
   * count could not be read; `count: 0` means it was read and is zero, and
   * `monitored` says whether anything looked at all. The three never render for
   * each other — see `DatastreamIssueBadge`.
   */
  open_issues?: {
    monitored: boolean;
    count: number;
    by_severity: Record<string, number>;
    highest_severity: string | null;
  };
  runs: { latest: string | null; latest_state: string | null };
  publications: {
    candidate: string | null;
    current: string | null;
    last_known_good: string | null;
  };
  links: {
    source: string;
    project_settings: string;
    governance: string;
  };
  primary_action: {
    kind: string;
    label: string;
    reason: string;
    tab: Tab;
  };
}

export interface WorkbenchTabPayload {
  schema: string;
  tab: Tab;
  project_id: string;
  datastream_id: string;
  evidence: Record<string, unknown>;
}

export type WorkbenchLoadState =
  | { status: "loading"; key: string }
  | { status: "ok"; key: string; header: WorkbenchHeader; payload: WorkbenchTabPayload }
  | { status: "denied"; key: string; message: string }
  | { status: "error"; key: string; message: string };
