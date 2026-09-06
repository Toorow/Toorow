/**
 * The console's ONE door onto the Master Data authority's write commands.
 *
 * WHY IT EXISTS. The cutover ratified on 2026-08-25
 * (`docs/product-architecture/governance.md`) closed the four legacy identity
 * writers with a sentence naming two gestures in order — *converge this
 * organization, then create, rename or archive it in Master Data* — and the
 * second gesture had no address until the wave that added
 * `POST .../governance/master-data/nodes` and the `rename` action beside
 * `archive` / `restore`. Every control that used to call
 * `/api/context/business-domains` calls these instead: the Governance dialog and
 * the Context Hub, two doors onto ONE writer, which is the shape
 * `context-hub.md` ratified on 2026-08-17 and the reason a second write path is
 * never the answer.
 *
 * THE IDEMPOTENCY KEY IS THE CALLER'S, AND IT IS MINTED ONCE PER ACT. The route
 * answers 428 without one, deliberately: a key minted server-side would make
 * every retry a new command. `mintCommandKey` is called when a form is submitted
 * and the SAME key is reused for every retry of that submission, so a client
 * timeout cannot mint a second identity — the property
 * `master_data_convergence.py` states about itself, applied to creation.
 */
import { apiPost } from "../lib/apiFetch";

export type BusinessIdentityKind = "business_domain" | "business_classification";

export interface MasterDataCommandResult {
  operation_id?: string;
  outcome?: string;
  idempotent_replay?: boolean;
  result?: {
    node_id?: string;
    action?: string;
    kind?: string;
    label?: string;
    slug?: string;
    version_id?: string | null;
    consumers_at_command_time?: Array<Record<string, unknown>>;
    impact_acknowledged?: boolean;
  } | null;
}

export interface CreateIdentityBody {
  kind: BusinessIdentityKind;
  name: string;
  reason: string;
  slug?: string;
  description?: string;
  owner?: string;
  classification_type?: string;
  domain_node_id?: string;
  parent_node_id?: string | null;
}

export type NodeCommandAction = "rename" | "archive" | "restore";

/**
 * A rename STATES THE BASE IT RENAMES FROM, and the type is what makes that
 * unskippable (`governance.md`, amendment of 2026-08-30).
 *
 * `expected_version` is the version identity the read model gave this door — the
 * authority's current published revision, or `null` where the object has none.
 * It is REQUIRED on the rename arm and optional elsewhere, so a door that renames
 * without sending back what it read does not compile. That is deliberate: the
 * previous shape was a single optional field, and both doors simply did not send
 * it while the type stayed green.
 *
 * It is NOT the `version_number` a screen prints beside an identity. That number
 * unions two ledgers which count the same object independently, so sending it
 * would compare two counters — a guard that cannot fire, which is worse than
 * none.
 */
export type NodeCommandBody =
  | {
      action: "rename";
      reason: string;
      expected_version: string | null;
      label: string;
      description?: string;
      classification_type?: string;
    }
  | {
      action: "archive" | "restore";
      reason: string;
      acknowledge_impact?: boolean;
      expected_version?: string | null;
    };

/** One key per act, reused across its retries. Never derived from the payload:
 *  two people declaring the same short code are two commands, and the second
 *  must be refused rather than silently replayed as the first. */
export function mintCommandKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${random}`;
}

const base = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/governance/master-data`;

const withKey = (key: string) => ({
  headers: { "Content-Type": "application/json", "Idempotency-Key": key },
});

export function createBusinessIdentity(
  projectId: string,
  body: CreateIdentityBody,
  idempotencyKey: string,
): Promise<MasterDataCommandResult> {
  return apiPost<MasterDataCommandResult>(`${base(projectId)}/nodes`, body, withKey(idempotencyKey));
}

export function runNodeCommand(
  projectId: string,
  nodeId: string,
  body: NodeCommandBody,
  idempotencyKey: string,
): Promise<MasterDataCommandResult> {
  return apiPost<MasterDataCommandResult>(
    `${base(projectId)}/nodes/${encodeURIComponent(nodeId)}/commands`,
    body,
    withKey(idempotencyKey),
  );
}
