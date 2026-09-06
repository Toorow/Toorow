/**
 * The inbound delivery credential seam, through `apiFetch` and nothing else.
 *
 * Every call here goes through `apiJson`, which is the one place the bearer is
 * attached. A bare `fetch("/api/...")` is the visible anomaly this repository
 * removed once already (finding F-010, and again in the Epic 46 review) -- the
 * MUI panel this replaces used bare `fetch` with a hand-rolled Authorization
 * header, which is exactly how a call site forgets it.
 */
import { apiJson } from "../../lib/apiFetch";

/** Safe read-model returned by GET .../credentials. Carries NO secret, ever. */
export interface DeliveryCredential {
  credential_id: string;
  datastream_id: string;
  channel: string;
  /** The last characters of the address, enough to recognise it. Not a secret. */
  safe_suffix: string | null;
  state: "ACTIVE" | "ROTATING" | "REVOKED" | "EXPIRED" | string;
  version: number;
  expires_at: string | null;
  overlap_until: string | null;
  issued_by: string | null;
  created_at: string | null;
}

/**
 * Returned ONCE by issue and rotate. `full_secret` is the delivery address; it
 * is never re-fetchable and never appears in any GET, MCP tool or audit payload
 * (Story 38.7 AC2, E38-NFR03).
 */
export interface IssuedCredential extends DeliveryCredential {
  /** Present only for the transaction that created the credential. */
  full_secret?: string;
  secret_available: boolean;
}

/**
 * The connector segment of the credential routes.
 *
 * `module_name` is the Datastream's own connector identity; `managed_feed` is
 * the fallback for a row that predates it. Source-agnostic either way -- no
 * vendor name is constructed here.
 */
function connectorSegment(connectorName: string | null | undefined): string {
  const value = (connectorName || "").trim();
  if (!value) throw new Error("Connector identity is unavailable");
  return value;
}

function basePath(
  moduleName: string | null | undefined,
  datastreamId: string,
): string {
  return `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}/datastreams/${encodeURIComponent(datastreamId)}/credentials`;
}

function mutationHeaders(idempotencyKey: string): Record<string, string> {
  return {
    "Content-Type": "application/json",
    "Idempotency-Key": idempotencyKey,
  };
}

/** A per-attempt key, so a double click replays instead of issuing twice. */
export function newIdempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${random}`;
}

export async function listDeliveryCredentials(
  moduleName: string | null | undefined,
  datastreamId: string,
  signal?: AbortSignal,
): Promise<DeliveryCredential[]> {
  const payload = await apiJson<{ credentials?: DeliveryCredential[] }>(
    basePath(moduleName, datastreamId),
    { method: "GET", cache: "no-store", signal },
  );
  return payload.credentials ?? [];
}

export async function issueDeliveryCredential(
  moduleName: string | null | undefined,
  datastreamId: string,
  channel: string,
  idempotencyKey: string,
): Promise<IssuedCredential> {
  return apiJson<IssuedCredential>(basePath(moduleName, datastreamId), {
    method: "POST",
    headers: mutationHeaders(idempotencyKey),
    body: JSON.stringify({ channel }),
    cache: "no-store",
  });
}

export async function rotateDeliveryCredential(
  moduleName: string | null | undefined,
  datastreamId: string,
  credentialId: string,
  idempotencyKey: string,
): Promise<IssuedCredential> {
  return apiJson<IssuedCredential>(
    `${basePath(moduleName, datastreamId)}/${encodeURIComponent(credentialId)}/rotate`,
    {
      method: "POST",
      headers: mutationHeaders(idempotencyKey),
      body: "{}",
      cache: "no-store",
    },
  );
}

export async function revokeDeliveryCredential(
  moduleName: string | null | undefined,
  datastreamId: string,
  credentialId: string,
  idempotencyKey: string,
): Promise<DeliveryCredential> {
  return apiJson<DeliveryCredential>(
    `${basePath(moduleName, datastreamId)}/${encodeURIComponent(credentialId)}/revoke`,
    {
      method: "POST",
      headers: mutationHeaders(idempotencyKey),
      body: "{}",
      cache: "no-store",
    },
  );
}

/**
 * One received ATTACHMENT, from the Story 38.14 inbox.
 *
 * This replaces the ledger-derived list the MUI panel used
 * (`/managed-feed/imports`), which by construction could only show deliveries
 * that had already produced a ledger row -- that is, the successes. A refused,
 * failed or unparseable delivery appeared nowhere, and that is exactly the one
 * an operator goes looking for.
 */
/**
 * Les trois étages AVAL de la colonne vertébrale (38.14 AC2) : ce que la pièce
 * jointe est devenue une fois atterrie.
 *
 * `published` est DÉCLARÉ, jamais déduit d'`outcome` ici. Un écran qui
 * traduirait lui-même « written » en « publié » serait la seconde surface d'un
 * désaccord — et c'est exactement l'erreur que le serveur vient de corriger :
 * atterri veut dire « une ligne de registre existe », pas « c'est en ligne ».
 *
 * `rejected_row_pct` est `null` tant que rien n'a été compté. 0 rejetée sur un
 * total inconnu n'est pas 0 %, et afficher 0 % rassurerait sur un import qui
 * n'a jamais lu une ligne.
 */
export interface DownstreamStages {
  mapping: {
    mapping_version_id: string | null;
    plan_version_id: string | null;
    write_mode: string | null;
  };
  dq: {
    accepted_row_count: number | null;
    rejected_row_count: number;
    rejected_row_pct: number | null;
  };
  publication: {
    outcome: string | null;
    published: boolean;
    execution_id: string | null;
    superseded_ledger_id: string | null;
    error_code: string | null;
    snapshot_observed_at: string | null;
  };
}

/** Ce que l'opérateur lit à la place d'un code d'issue. */
export const OUTCOME_SENTENCE: Record<string, string> = {
  opened: "import opened, nothing counted yet",
  written: "a candidate exists, nothing is live yet",
  noop: "the snapshot was unchanged, nothing was rewritten",
  rejected: "refused by the data-quality gate",
  published: "published",
  failed: "the import failed",
};

export interface ReceivedAttachment {
  raw_import_id: string | null;
  /** Absent tant que la pièce jointe n'a pas atterri. */
  downstream?: DownstreamStages | null;
  ordinal: number;
  /** Untrusted sender text, already defused server-side. Display only. */
  filename: string | null;
  media_type_declared: string | null;
  media_type_detected: string | null;
  size_bytes: number | null;
  content_hash: string | null;
  state: string;
  error_code: string | null;
  import_ledger_id: string | null;
  created_at: string | null;
  scan_verdict: ScanVerdict | null;
  scan_job?: {
    state:
      | "QUEUED"
      | "RUNNING"
      | "RETRY_WAIT"
      | "SUCCEEDED"
      | "REJECTED"
      | "DEAD_LETTER"
      | "UNKNOWN";
    attempt_count: number | null;
    max_attempts: number | null;
    error_code: string | null;
    recovery_count: number;
    updated_at: string | null;
    recovery?: {
      version: string | null;
      job_id: string | null;
      command: string | null;
      requires_authorization: boolean;
    } | null;
  } | null;
  receipt: {
    receipt_id: string;
    channel: string;
    state: string;
    provider_event_id: string | null;
    created_at: string | null;
  } | null;
}

/**
 * Redacted, content-free evidence produced before an inbound parser can run.
 * Every field is optional so inbox rows written by an older scan policy remain
 * readable instead of being mistaken for complete current-policy evidence.
 */
export interface ScanVerdict {
  accepted?: boolean;
  reason?: string | null;
  detected_type?: string | null;
  declared_type?: string | null;
  size_bytes?: number | null;
  malware?: "clean" | "infected" | "unavailable" | "not_run" | null;
  malware_detail?: string | null;
  malware_engine?: string | null;
  policy?: {
    version?: string | null;
    max_bytes?: number | null;
    max_uncompressed_bytes?: number | null;
    max_compression_ratio?: number | null;
    max_archive_entries?: number | null;
    max_rows?: number | null;
    max_columns?: number | null;
    max_scan_seconds?: number | null;
    max_memory_bytes?: number | null;
  } | null;
  evidence?: {
    size_bytes?: number | null;
    size_limit?: number | null;
    encoding?: string | null;
    row_estimate?: number | null;
    row_limit?: number | null;
    column_estimate?: number | null;
    column_limit?: number | null;
    archive_entries?: number | null;
    archive_entry_limit?: number | null;
    archive_uncompressed_bytes?: number | null;
    archive_uncompressed_limit?: number | null;
    archive_central_directory_bytes?: number | null;
    archive_central_directory_limit?: number | null;
    archive_worst_ratio?: number | "infinite" | null;
    archive_ratio_limit?: number | null;
    spreadsheet_rows?: number | null;
    spreadsheet_columns?: number | null;
  } | null;
}

export async function listReceivedAttachments(
  moduleName: string | null | undefined,
  datastreamId: string,
  signal?: AbortSignal,
): Promise<ReceivedAttachment[]> {
  const payload = await apiJson<{ items?: ReceivedAttachment[] }>(
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}/datastreams/${encodeURIComponent(datastreamId)}/inbox?limit=10`,
    { method: "GET", cache: "no-store", signal },
  );
  return payload.items ?? [];
}

export async function recoverInboundScanJob(
  moduleName: string | null | undefined,
  datastreamId: string,
  jobId: string,
  idempotencyKey: string,
): Promise<void> {
  await apiJson(
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}` +
      `/datastreams/${encodeURIComponent(datastreamId)}` +
      `/scan-jobs/${encodeURIComponent(jobId)}/recover`,
    {
      method: "POST",
      headers: mutationHeaders(idempotencyKey),
      body: "{}",
      cache: "no-store",
    },
  );
}

/**
 * La proposition de reprise d'une livraison retenue, et son exécution.
 *
 * POURQUOI CES DEUX FONCTIONS N'EXISTAIENT PAS, ET CE QUE ÇA COÛTAIT.
 * `inbound_reprocess_api.py` sert `GET` et `POST
 * .../raw-imports/{id}/reprocess` depuis la story 38.18. Aucun appelant côté
 * console : `grep -rn "reprocess" ui/admin/src` ne rendait qu'un COMMENTAIRE.
 * Le serveur offrait, aucun écran ne demandait.
 *
 * Mesuré le 2026-08-08 sur `ds_01KZEV9T7RGMDRRD7E6JT262K1` : trois livraisons
 * arrêtées en `PROCESSING` depuis le matin, leurs octets conservés, et un
 * chemin de reprise gouverné que personne ne pouvait atteindre depuis la
 * console. Un opérateur voyait la livraison bloquée et n'avait rien à cliquer.
 *
 * LA PROPOSITION SE LIT AVANT D'AGIR. Elle n'écrit rien, et elle porte sa
 * propre indisponibilité comme une VALEUR (`available: false` + `reason`) :
 * l'écran rend la raison plutôt qu'un bouton mort. C'est le contrat que
 * `evaluate_reprocess` déclare -- « unavailability is a VALUE here, and the
 * caller renders it ».
 */
export interface ReprocessProposal {
  availability: {
    available: boolean;
    reason: string | null;
    detail?: string | null;
    content_hash?: string | null;
    size_bytes?: number | null;
    filename?: string | null;
    retention_expires_at?: string | null;
    legal_hold?: boolean | null;
  };
  raw_import_id: string;
  datastream_id: string;
  bound_versions?: { plan_version_id: string | null; mapping_version_id: string | null };
  creates_new_execution?: boolean;
  may_move_published_pointer?: boolean;
  write_mode?: string | null;
  mutates_prior_execution?: boolean;
  requires_provider_call?: boolean;
  /**
   * 38-18 AC1. La version SOUS LAQUELLE le fichier serait rejoué.
   *
   * `target_selected` distingue « j'ai choisi celle-ci » de « c'est celle en
   * vigueur » — deux états qu'un même identifiant ne sépare pas : la version
   * courante EST un choix valide, et une personne qui la sélectionne
   * explicitement doit lire qu'elle l'a fait.
   *
   * `available_versions` porte les non exécutables aussi. Un brouillon qu'on ne
   * peut pas rejouer est exactement ce qu'on cherche quand on se demande
   * pourquoi son candidat n'est pas proposé ; le retirer de la liste
   * transformerait un refus explicable en absence.
   */
  target_selected?: boolean;
  target_refused?: string | null;
  available_versions?: ReprocessTargetVersion[];
  /**
   * 38-18 AC1, l'autre moitié : « target mapping/TEMPLATE versions ».
   *
   * TROIS FAITS, TROIS MOTS, et jamais un `null` pour les trois. « Ce
   * Datastream n'épingle aucun Template » (`not_applicable`), « le registre n'a
   * pas pu être lu » (`unavailable`), « l'épingle ne résout rien » (`unknown`)
   * et « la voici » (`bound`) sont quatre phrases qu'une valeur absente
   * confondrait en une seule.
   */
  template_version?: ReprocessTemplateVersion;
  available_template_versions?: ReprocessTemplateAlternative[];
}

export interface ReprocessTemplateVersion {
  state: "bound" | "not_applicable" | "unavailable" | "unknown";
  reason?: string | null;
  kind?: "project" | "catalog";
  template_id?: string;
  template_code?: string;
  version?: number;
  is_active?: boolean;
}

export interface ReprocessTemplateAlternative {
  template_id: string;
  version: number;
  is_active: boolean;
  is_pinned: boolean;
  replayable: boolean;
  not_replayable_reason: string | null;
  created_at: string | null;
  created_by: string | null;
}

/**
 * LA PORTÉE GOUVERNÉE — 38-18 AC1, « and a governed scope ».
 *
 * Le patron est ratifié, pas inventé :
 * `docs/product-architecture/datastream-workbench-and-wizard.md` —
 * « Every destructive or durable repair follows `Prepare > Review exact scope
 * and consequences > Confirm > Execute as a new durable operation` » — et la
 * même page fixe ce que « review exact scope » veut dire en pratique : **le
 * compte AVANT l'acte**, avec les objets qu'il nomme (mesuré 2026-08-10 aux lignes 2191 et 1080 ; ces
 * numéros BOUGENT — la même phrase était à `:2130` deux commits plus tôt dans
 * cette seule session, donc la phrase est la citation, pas le numéro).
 *
 * `scan_truncated` est la borne DITE. Une troncature muette laisserait quelqu'un
 * croire qu'un ensemble a été rejoué alors qu'il ne l'a jamais été.
 */
export interface ReprocessScopeProposal {
  schema: string;
  datastream_id: string;
  selection: {
    mode: "explicit" | "criterion";
    criterion: Record<string, unknown> | null;
    confirm_raw_import_ids: string[];
  };
  scope: {
    state: "counted";
    examined: number;
    reprocessable: number;
    refused: number;
  };
  members: ReprocessScopeMember[];
  scan_truncated: boolean;
  scan_limit: number;
  bound_versions?: { plan_version_id: string | null; mapping_version_id: string | null };
  target_selected?: boolean;
  target_refused?: string | null;
  available_versions?: ReprocessTargetVersion[];
  template_version?: ReprocessTemplateVersion;
  available_template_versions?: ReprocessTemplateAlternative[];
  creates_new_execution: boolean;
  may_move_published_pointer: boolean;
  write_mode?: string | null;
  mutates_prior_execution?: boolean;
  requires_provider_call?: boolean;
  rollback?: string;
}

export interface ReprocessScopeMember {
  raw_import_id: string;
  filename: string | null;
  content_hash: string | null;
  size_bytes: number | null;
  state: string | null;
  created_at: string | null;
  available: boolean;
  reason: string | null;
  detail: string | null;
}

export interface ReprocessTargetVersion {
  mapping_version_id: string;
  version_number: number;
  executable: boolean;
  blocking_count: number;
  created_at: string | null;
  created_by: string | null;
  is_current: boolean;
}

function reprocessPath(
  moduleName: string | null | undefined,
  datastreamId: string,
  rawImportId: string,
): string {
  return (
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}` +
    `/datastreams/${encodeURIComponent(datastreamId)}` +
    `/raw-imports/${encodeURIComponent(rawImportId)}/reprocess`
  );
}

/** Lit la proposition. N'écrit rien : c'est une inspection. */
export async function readReprocessProposal(
  moduleName: string | null | undefined,
  datastreamId: string,
  rawImportId: string,
  targetMappingVersionId?: string,
  signal?: AbortSignal,
): Promise<ReprocessProposal> {
  // En QUERY, pas en corps : la proposition est une lecture, et un corps sur un
  // GET est ce qu'un proxy ou un cache a le droit de jeter.
  const query = targetMappingVersionId
    ? `?target_mapping_version_id=${encodeURIComponent(targetMappingVersionId)}`
    : "";
  return apiJson<ReprocessProposal>(
    `${reprocessPath(moduleName, datastreamId, rawImportId)}${query}`,
    { method: "GET", cache: "no-store", signal },
  );
}

/**
 * Exécute la reprise. La RAISON est obligatoire côté serveur — « a reprocess
 * changes published data » — et elle est donc obligatoire ici : un client qui
 * enverrait une chaîne vide recevrait `missing_reason`, et l'opérateur lirait
 * un refus technique au lieu de la question qu'on aurait dû lui poser.
 */
export async function executeReprocess(
  moduleName: string | null | undefined,
  datastreamId: string,
  rawImportId: string,
  reason: string,
  idempotencyKey: string,
  targetMappingVersionId?: string,
): Promise<{ execution_id?: string; import_ledger_id?: string }> {
  return apiJson(reprocessPath(moduleName, datastreamId, rawImportId), {
    method: "POST",
    headers: mutationHeaders(idempotencyKey),
    // La version choisie voyage AVEC l'exécution, pas seulement avec la
    // proposition : deux rejeux du même fichier sous deux versions sont deux
    // actes différents, et le serveur l'inscrit dans sa trace.
    body: JSON.stringify(
      targetMappingVersionId
        ? { reason, target_mapping_version_id: targetMappingVersionId }
        : { reason },
    ),
    cache: "no-store",
  });
}

function reprocessScopePath(
  moduleName: string | null | undefined,
  datastreamId: string,
): string {
  return (
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}` +
    `/datastreams/${encodeURIComponent(datastreamId)}/reprocess-scope`
  );
}

/**
 * Lit la portée : le compte AVANT l'acte, et les objets qu'il nomme.
 *
 * N'écrit rien. Les identifiants voyagent en query parce que la préparation est
 * une LECTURE, et un corps sur un GET est ce qu'un proxy ou un cache a le droit
 * de jeter.
 */
export async function readReprocessScope(
  moduleName: string | null | undefined,
  datastreamId: string,
  rawImportIds: string[],
  targetMappingVersionId?: string,
  signal?: AbortSignal,
): Promise<ReprocessScopeProposal> {
  const query = new URLSearchParams();
  query.set("raw_import_ids", rawImportIds.join(","));
  if (targetMappingVersionId) {
    query.set("target_mapping_version_id", targetMappingVersionId);
  }
  return apiJson<ReprocessScopeProposal>(
    `${reprocessScopePath(moduleName, datastreamId)}?${query.toString()}`,
    { method: "GET", cache: "no-store", signal },
  );
}

/**
 * Exécute la portée confirmée, en UNE opération durable.
 *
 * Le corps porte les membres ÉNUMÉRÉS, jamais le critère : un critère réévalué
 * au moment de l'acte pourrait nommer un autre ensemble que celui qu'une
 * personne a lu et confirmé.
 */
export async function executeReprocessScope(
  moduleName: string | null | undefined,
  datastreamId: string,
  rawImportIds: string[],
  reason: string,
  idempotencyKey: string,
  targetMappingVersionId?: string,
): Promise<{ status?: string; operation_id?: string }> {
  return apiJson(reprocessScopePath(moduleName, datastreamId), {
    method: "POST",
    headers: mutationHeaders(idempotencyKey),
    body: JSON.stringify(
      targetMappingVersionId
        ? {
            reason,
            raw_import_ids: rawImportIds,
            target_mapping_version_id: targetMappingVersionId,
          }
        : { reason, raw_import_ids: rawImportIds },
    ),
    cache: "no-store",
  });
}

/**
 * La chronologie d'UNE livraison, toutes pièces jointes comprises.
 *
 * `GET .../deliveries/{receipt_id}` est servi depuis la story 38.14 et n'avait
 * lui non plus aucun appelant côté console — l'autre moitié de l'AC2 de 38-15.
 * L'inbox montre les pièces jointes d'un flux ; ceci montre UNE livraison et ce
 * qu'elle a produit.
 *
 * `published_data_changed` est DÉCLARÉ par le serveur, jamais déduit ici. Le
 * contrat le dit en toutes lettres : « AC2 requires the interface to say whether
 * published data changed, and leaving that to be deduced from an outcome string
 * is how two surfaces end up disagreeing about it ». Un écran qui le
 * recalculerait à partir d'un état serait la seconde surface de ce désaccord.
 */
export interface DeliveryTimeline {
  receipt: {
    receipt_id?: string;
    id?: string;
    state: string;
    channel?: string | null;
    created_at?: string | null;
    import_ledger_id?: string | null;
  };
  attachments: ReceivedAttachment[];
  attachment_count: number;
  landed_count: number;
  /** Combien ont RÉELLEMENT été publiées — distinct de `landed_count`. */
  published_count: number;
  published_data_changed: boolean;
}

export async function readDeliveryTimeline(
  moduleName: string | null | undefined,
  datastreamId: string,
  receiptId: string,
  signal?: AbortSignal,
): Promise<DeliveryTimeline> {
  return apiJson<DeliveryTimeline>(
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}` +
      `/datastreams/${encodeURIComponent(datastreamId)}` +
      `/deliveries/${encodeURIComponent(receiptId)}`,
    { method: "GET", cache: "no-store", signal },
  );
}

/**
 * Le contexte de réparation d'un mapping — 38-16 AC1.
 *
 * `GET .../raw-imports/{id}/mapping-context` est servi par
 * `inbound_mapping_entry.get_mapping_repair_context` et n'avait aucun appelant :
 * « rien n'est monté, aucun composant, aucune route ».
 *
 * Il répond aux trois questions d'un opérateur devant une livraison qui ne
 * mappe pas : ce que le fichier CONTENAIT (`source_columns`), ce à quoi il est
 * ÉPINGLÉ (`pinned_versions`), et COMMENT en changer (`governed_path`).
 *
 * `governed_path` est la clé du dessin. Il nomme le moteur EXISTANT --
 * `datastream_change`, avec ses URL de prepare et de confirm -- au lieu d'un
 * second chemin de publication. Le journal de la story le dit : « un second
 * chemin de publication serait le quatrième moteur que cette épique existe pour
 * éviter ». Cet écran renvoie donc vers lui ; il n'en refait pas un.
 */
export interface MappingRepairContext {
  available: boolean;
  reason: string | null;
  raw_import_id: string;
  datastream_id: string;
  project_id?: string | null;
  raw_import?: {
    state?: string | null;
    error_code?: string | null;
    filename?: string | null;
    media_type_detected?: string | null;
    content_hash?: string | null;
  };
  pinned_versions?: { plan_version_id: string | null; mapping_version_id: string | null };
  governed_path?: { engine: string; prepare: string; confirm: string };
  /**
   * ONE ENTRY PER COLUMN, AS AN OBJECT -- never a bare string.
   *
   * This was typed `string[]` and rendered with `.join(", ")`, which produces
   * `[object Object], [object Object]` against every real answer the server
   * has ever sent. `inbound_mapping_entry` has returned objects since the
   * screen was mounted (2026-08-08); the server test reads
   * `[c["name"] for c in context["source_columns"]]`.
   *
   * Nothing caught it: `tsc --noEmit` cannot see that a hand-written type is
   * false, and the UI test stubbed `["date", "campaign", "clicks"]` -- a shape
   * the server does not produce. The shape is now pinned to a recorded server
   * sample by `inboundContractSamples`.
   *
   * AC1 asks for columns / TYPES / SAFE SAMPLES: all three travel here, and
   * all three are now rendered.
   */
  source_columns?: MappingSourceColumn[];
  /**
   * The seven readings of 38.16 AC3, computed on the ALREADY RETAINED bytes.
   *
   * They lived on `build_file_source_preview`, whose only two callers were the
   * UPLOAD path -- one of them requiring `file_base64`, so a re-upload of the
   * very file the platform already held. The ratified document forbids that
   * result by name (`file-source-ingestion.md:203`: "the same file yields
   * different results by upload and by email").
   *
   * `available: false` always carries a REASON: a Datastream with no `fst_`
   * template has no verdict to report, and showing "0 issues" would send the
   * operator to repair something else.
   */
  preview?: MappingPreview;
}

/** The result of the SAME composer the upload path calls. */
export interface MappingPreview {
  available: boolean;
  reason: string | null;
  blocked?: boolean;
  blocked_reason?: string | null;
  /** Required-field coverage, column by column. */
  fields?: unknown[] | null;
  /** The landing verdict -- the SAME one the import uses. */
  gate?: Record<string, unknown> | null;
  /** Date / timezone / number coercions the file needs. */
  coercions?: unknown[] | null;
  /** Declared currencies and units. */
  units?: unknown[] | null;
  /** Identity and grain, through the placement class. */
  placement?: Record<string, unknown> | null;
  /** Sensitive-column classification. */
  classification?: Record<string, unknown> | null;
  /** Row-level validation summary. */
  row_validation?: Record<string, unknown> | null;
}

/** One column of the retained file, as `inbound_mapping_entry` serialises it. */
export interface MappingSourceColumn {
  name: string;
  index: number;
  detected_type: string | null;
  null_count: number | null;
  /** A label carried by the file (SAV), offered to a human, never auto-bound. */
  source_label: string | null;
  /** Masked before it leaves the server -- raw provider values never arrive. */
  masked_samples: (string | null)[];
}

export async function readMappingRepairContext(
  moduleName: string | null | undefined,
  datastreamId: string,
  rawImportId: string,
  signal?: AbortSignal,
): Promise<MappingRepairContext> {
  return apiJson<MappingRepairContext>(
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}` +
      `/datastreams/${encodeURIComponent(datastreamId)}` +
      `/raw-imports/${encodeURIComponent(rawImportId)}/mapping-context`,
    { method: "GET", cache: "no-store", signal },
  );
}

/**
 * La santé du connecteur entrant — 38-14 AC5, « `/health` n'a aucun consommateur ».
 *
 * TROIS FAITS DU CONTRAT COMMANDENT CE QUE L'ÉCRAN DOIT FAIRE, et les ignorer
 * produirait une surface qui ment dans le sens le plus dangereux :
 *
 * 1. `healthy` est à TROIS valeurs — `true` / `false` / `null`. `null` veut dire
 *    « pas déterminé », et le serveur le dit lui-même : « nothing is ever
 *    reported green on missing evidence ». Un écran qui replierait `null` sur
 *    « ok » inventerait une santé que personne n'a mesurée.
 * 2. LA COUCHE LA PLUS FAIBLE DÉCIDE, et l'ordre est déclaré : un `false` bat un
 *    `unknown`, un `unknown` bat `healthy`. L'écran rend `overall` tel quel ; il
 *    ne le recalcule pas depuis les couches.
 * 3. `authority` dit QUI peut agir. Une cause bloquante sans son autorité envoie
 *    quelqu'un réparer ce qu'il n'a pas le droit de toucher.
 */
/**
 * La réparation vers laquelle une alerte pointe — 38-14 AC4.
 *
 * `next_action` est une phrase ; ceci est l'action. `console` est une référence
 * SÉMANTIQUE (jamais une URL en dur) que le registre de navigation résout, de
 * sorte qu'un écran renommé ne périme pas l'alerte.
 *
 * `null` sur une couche saine — rien à réparer — et sur une couche illisible :
 * une base injoignable ne se répare pas en appelant quelque chose, et offrir un
 * bouton là inviterait à le presser en boucle. `authority` dit quand même à qui
 * le problème appartient.
 */
export interface LayerRecovery {
  api: string | null;
  method: string | null;
  mcp_tool: string | null;
  console: { owner_reference?: Record<string, unknown> } | null;
}

export interface InboundHealthLayer {
  layer: string;
  state: string;
  healthy: boolean | null;
  authority: string;
  blocking_cause: string | null;
  next_action?: string | null;
  recovery?: LayerRecovery | null;
}

export interface InboundHealth {
  connector_name: string;
  environment?: string | null;
  datastream_id?: string | null;
  overall: "healthy" | "unknown" | "blocked" | string;
  authority: string;
  blocking_cause: string | null;
  layers: InboundHealthLayer[];
  metrics?: Record<string, unknown> | null;
}

export async function readInboundHealth(
  moduleName: string | null | undefined,
  datastreamId?: string,
  signal?: AbortSignal,
): Promise<InboundHealth> {
  const query = datastreamId
    ? `?datastream_id=${encodeURIComponent(datastreamId)}`
    : "";
  return apiJson<InboundHealth>(
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}/health${query}`,
    { method: "GET", cache: "no-store", signal },
  );
}

/**
 * Le test de routage au niveau Datastream — 38-15 AC5.
 *
 * La question qu'un opérateur pose AVANT de dire à un fournisseur d'envoyer :
 * « si un fichier arrive maintenant, atteint-il mon Datastream ? » Le connecteur
 * avait déjà un test synthétique, mais au niveau installation : il prouve la
 * porte d'entrée, pas la chaîne derrière.
 *
 * Il n'écrit RIEN — ni reçu, ni évidence brute, ni exécution, ni publication —
 * donc il ne peut pas être confondu avec une livraison de fournisseur, et
 * l'inbox ne montre jamais un fichier fantôme qu'on irait chercher.
 *
 * `passed: null` veut dire NON ATTEINT, jamais « échoué ». Une étape que
 * personne n'a essayée n'est pas une étape en panne, et l'afficher comme telle
 * enverrait réparer un maillon peut-être intact.
 */
export interface RoutingStep {
  step: string;
  passed: boolean | null;
  reason: string | null;
}

export interface RoutingReport {
  synthetic: boolean;
  datastream_id: string;
  channel: string;
  routes: boolean;
  blocking_step: string | null;
  blocking_reason: string | null;
  steps: RoutingStep[];
}

/** Ce que chaque maillon veut dire pour quelqu'un qui ne lit pas le code. */
export const ROUTING_STEP_LABEL: Record<string, string> = {
  datastream_exists: "the Datastream exists",
  connector_binding: "it is bound to this connector",
  datastream_receivable: "it accepts this delivery channel",
  domain_ready: "the delivery domain is verified",
  credential_active: "its delivery key is still valid",
  credential_resolves_back: "that key routes back to this Datastream",
};

export async function runRoutingTest(
  moduleName: string | null | undefined,
  datastreamId: string,
  channel: string,
  signal?: AbortSignal,
): Promise<RoutingReport> {
  return apiJson<RoutingReport>(
    `/api/connectors/${encodeURIComponent(connectorSegment(moduleName))}` +
      `/datastreams/${encodeURIComponent(datastreamId)}/routing-test`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel }),
      cache: "no-store",
      signal,
    },
  );
}
