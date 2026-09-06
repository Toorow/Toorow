/**
 * The delivery address, on the tab the ratified contract puts it.
 *
 * WHY IT WAS REWRITTEN RATHER THAN MOUNTED. Story 38.7 delivered this surface
 * as an MUI panel in `datastreams/`, and it sat mounted nowhere for a week --
 * so nobody could obtain the address their Datastream exists for. AD-35
 * (ratified 2026-07-31) makes MUI legacy, and the Workbench Overview is already
 * Tailwind + shadcn, so mounting it would have put MUI back into a workspace
 * that migrated out of it. This is the PORT: the same behaviour on the ratified
 * primitives, no new base class and no literal colour. The MUI original is
 * removed -- it never compiled, because nothing ever imported it.
 *
 * `datastream-workbench-and-wizard.md` puts "identity and bindings;
 * configuration readiness; blockers" on Overview, with the highest-priority
 * safe next step as its primary action. For an inbound Datastream with no
 * address, that next step is issuing one -- which is why this belongs here and
 * not on a seventh tab.
 *
 * THE ADDRESS IS A CREDENTIAL, and the surface treats it as one:
 *   * it is returned exactly once, at issuance and at rotation, and is never
 *     re-fetchable (Story 38.7 AC2);
 *   * it is cleared from component state as soon as the panel unmounts, so it
 *     does not sit in memory behind a tab nobody is looking at;
 *   * revoking is irreversible, so it asks first -- the Epic 46 review found
 *     confirmation missing on exactly this class of action.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ObjectId,
  Badge,
  Button,
  Checkbox,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Panel,
  PanelHeader,
  Status,
  formatNumber, formatTimestamp,
  formatDuration, formatPercent,
  stateLabel,
  stateTone,
} from "../../ui";
import {
  type DeliveryCredential,
  type IssuedCredential,
  type ReceivedAttachment,
  type ScanVerdict,
  issueDeliveryCredential,
  listDeliveryCredentials,
  recoverInboundScanJob,
  listReceivedAttachments,
  readReprocessProposal,
  executeReprocess,
  readReprocessScope,
  executeReprocessScope,
  type ReprocessScopeProposal,
  type ReprocessTemplateVersion,
  OUTCOME_SENTENCE,
  runRoutingTest,
  ROUTING_STEP_LABEL,
  readDeliveryTimeline,
  readMappingRepairContext,
  readInboundHealth,
  type InboundHealth,
  type MappingPreview,
  type RoutingReport,
  type RoutingStep,
  type ReprocessProposal,
  type DeliveryTimeline,
  type MappingRepairContext,
  newIdempotencyKey,
  revokeDeliveryCredential,
  rotateDeliveryCredential,
} from "./inboundDeliveryApi";

/**
 * THE WIRE WORD, TRANSLATED TO A DECLARED ONE — and nothing else.
 *
 * A private `stateTone` used to live here and drew `EXPIRED` as an error against
 * the union's `expired: "neutral"`: the third collision of 76-2, and the one
 * `stateVocabulary.test.ts` could not see, because it was a function rather than
 * an exported map. It is closed the way the other two were — the union wins, and
 * where a screen carries a genuinely different fact it DECLARES A WORD instead
 * of passing a private map (`console-presentation.md` §3).
 *
 * The fact is different here, and the server says so rather than this panel:
 * `server/core/inbound_credentials.py:96-97` puts `EXPIRED` in `_TERMINAL_STATES`
 * beside `REVOKED` — « a credential in one of these is permanently invalid » —
 * so deliveries fail closed until somebody issues a new one. The union's
 * `expired` is the harmless one (a lapsed Share confirmation). So this file maps
 * its own wire word to `credential_expired`, which is declared, red and spelled
 * in `ui/stateVocabulary.ts`, and reads every other state straight from the union.
 *
 * This is a one-line DOMAIN translation, not a second tone scale: it names no
 * colour, and changing what `credential_expired` looks like is a change to the
 * vocabulary, in front of everybody.
 */
function credentialWord(state: string): string {
  return state.toUpperCase() === "EXPIRED" ? "credential_expired" : state;
}

/** "None" is this panel's word for an absence; the instant is the console's. */
function when(value: string | null): string {
  return value ? formatTimestamp(value) : "None";
}

function message(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "Request failed";
}

/**
 * La version de Template, en QUATRE phrases distinctes.
 *
 * Jamais un `null` ni un `0` pour les quatre. « Ce Datastream n'épingle aucun
 * Template », « le registre n'a pas pu être lu », « l'épingle ne résout rien »
 * et « la voici, v4 » envoient une personne à quatre endroits différents ; les
 * confondre en une cellule vide envoie tout le monde au mauvais.
 */
function describeTemplateVersion(
  template: ReprocessTemplateVersion | undefined,
): string {
  if (!template) return "Template version not reported";
  if (template.state === "bound") {
    const code = template.template_code ? `${template.template_code} ` : "";
    return `Template ${code}v${template.version}`;
  }
  if (template.state === "not_applicable") {
    return (
      template.reason ?? "this Datastream is not pinned to a file-source Template"
    );
  }
  if (template.state === "unavailable") {
    return `Template version unreadable: ${
      template.reason ?? "the registry did not answer"
    }`;
  }
  return `Template pin resolves to nothing: ${
    template.reason ?? "no matching row"
  }`;
}

function number(value: number): string {
  return formatNumber(value);
}

function bytes(value: number): string {
  return `${number(value)} bytes`;
}

function boundedMeasure(
  observed: number | null | undefined,
  limit: number | null | undefined,
  format: (value: number) => string = number,
): string | null {
  if (observed == null && limit == null) return null;
  const observedText = observed == null ? "not recorded" : format(observed);
  return limit == null
    ? observedText
    : `${observedText} / limit ${format(limit)}`;
}

function ScanEvidence({
  verdict,
  fallbackSize,
}: {
  verdict: ScanVerdict;
  fallbackSize: number | null;
}) {
  const evidence = verdict.evidence ?? {};
  const policy = verdict.policy ?? {};
  const rows = boundedMeasure(
    evidence.row_estimate ?? evidence.spreadsheet_rows,
    evidence.row_limit ?? policy.max_rows,
  );
  const columns = boundedMeasure(
    evidence.column_estimate ?? evidence.spreadsheet_columns,
    evidence.column_limit ?? policy.max_columns,
  );
  const ratioLimit =
    evidence.archive_ratio_limit ?? policy.max_compression_ratio;
  const archiveRatio =
    evidence.archive_worst_ratio === "infinite"
      ? ratioLimit == null
        ? "infinite"
        : `infinite / limit ${number(ratioLimit)}:1`
      : boundedMeasure(
          evidence.archive_worst_ratio,
          ratioLimit,
          (value) => `${number(value)}:1`,
        );
  const measurements = [
    [
      // A SCAN DECISION IS NOT A DELIVERY STATE, AND IT MAY NOT BORROW ITS WORD.
      // This panel already renders `stateLabel(item.state)` on the row above,
      // where `ACCEPTED` and `REJECTED` are DECLARED states
      // (`ui/stateVocabulary.ts:96,212,314`) and read `Accepted` / `Rejected`.
      // Spelling the scan's verdict the same way put two different facts under
      // one word on one screen, which `console-presentation.md` §4 refuses
      // ("One term per concept"). §3 already settled the same shape once, for
      // `unavailable`: *"a different fact and gets its own word"*. The state
      // keeps the declared word; the verdict names what it decided about.
      "Decision",
      verdict.accepted == null
        ? "Not recorded"
        : verdict.accepted
          ? "Scan accepted"
          : "Scan rejected",
    ],
    ["Reason", verdict.reason ?? "None recorded"],
    ["Policy version", policy.version ?? "Not recorded"],
    ["Malware", verdict.malware ?? "Not recorded"],
    ["Malware detail", verdict.malware_detail ?? null],
    ["Malware engine", verdict.malware_engine ?? "Not recorded"],
    ["Declared type", verdict.declared_type ?? "Not recorded"],
    ["Detected type", verdict.detected_type ?? "Not recorded"],
    [
      "Size",
      boundedMeasure(
        evidence.size_bytes ?? verdict.size_bytes ?? fallbackSize,
        evidence.size_limit ?? policy.max_bytes,
        bytes,
      ),
    ],
    ["Encoding", evidence.encoding ?? null],
    ["Rows", rows],
    ["Columns", columns],
    [
      "Archive entries",
      boundedMeasure(
        evidence.archive_entries,
        evidence.archive_entry_limit ?? policy.max_archive_entries,
      ),
    ],
    [
      "Archive expanded size",
      boundedMeasure(
        evidence.archive_uncompressed_bytes,
        evidence.archive_uncompressed_limit ?? policy.max_uncompressed_bytes,
        bytes,
      ),
    ],
    [
      "Archive central directory",
      boundedMeasure(
        evidence.archive_central_directory_bytes,
        evidence.archive_central_directory_limit,
        bytes,
      ),
    ],
    ["Archive worst ratio", archiveRatio],
    [
      "Scan time budget",
      policy.max_scan_seconds == null
        ? null
        : `${number(policy.max_scan_seconds)} seconds`,
    ],
    [
      "Memory budget",
      policy.max_memory_bytes == null ? null : bytes(policy.max_memory_bytes),
    ],
  ].filter((row): row is [string, string] => row[1] != null);

  return (
    <div
      className="grid gap-2 rounded-md border border-divider-base p-3"
      data-testid="redacted-scan-evidence"
    >
      <p className="m-0 text-ui font-medium text-text">
        Redacted scan evidence
      </p>
      <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-ui max-md:grid-cols-1">
        {measurements.map(([label, value]) => (
          <div key={label}>
            <dt className="text-text-secondary">{label}</dt>
            <dd className="m-0 break-words text-text">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** The channel an address is issued for. `email` is what the backend stores. */
function configuredChannels(channels: string[]): Array<"email" | "webhook"> {
  return Array.from(
    new Set(
      channels
        .map((channel) => channel.trim().toLowerCase())
        .map((channel) => (channel === "inbound_email" ? "email" : channel))
        .filter(
          (channel): channel is "email" | "webhook" =>
            channel === "email" || channel === "webhook",
        ),
    ),
  );
}

/*
  FOUR FORMATTERS, ONE RULE: never render a zero for something that was not
  measured. The server carefully distinguishes `null` (no denominator),
  `{available:false}` (cannot exist) and a real count; flattening all three to
  `?? 0` at display time would undo that work at the last metre -- and it is the
  SCREEN, not the route, that an operator believes.
*/

function formatMetric(value: unknown): string {
  if (typeof value === "number") return formatNumber(value);
  return "not measured";
}

function formatRate(value: unknown): string {
  // A RATIO on the wire. `no rate` is not `0.0 %` -- see the block above.
  if (typeof value !== "number") return "no rate — nothing arrived";
  return formatPercent(value);
}

function formatSeconds(value: unknown): string {
  // SECONDS on the wire, milliseconds in the formatter. `precise` keeps the
  // tenth this reading is taken to; the plain ladder would answer `< 1 s`.
  if (typeof value !== "number") return "not measured";
  return formatDuration(value * 1000, { precise: true });
}

function formatPublication(value: unknown): string {
  if (!value || typeof value !== "object") return "none yet";
  const published = value as { published_at?: string | null };
  // A raw ISO instant reached the screen here until 76-1.
  return published.published_at ? formatTimestamp(published.published_at) : "none yet";
}

/**
 * What the server declares UNOBSERVABLE, rendered as such.
 *
 * A delivery refused at the credential creates no receipt, and the CHECK on
 * `app.inbound_receipts.state` admits no "denied" state: there is nothing to
 * count. Showing `0` would say "nothing was denied" where the truth is "this
 * surface cannot see denials".
 */
function formatUnavailable(value: unknown): string {
  if (typeof value === "number") return formatNumber(value);
  if (value && typeof value === "object") {
    const envelope = value as { available?: boolean; reason?: string; detail?: string };
    if (envelope.available === false) {
      return `${envelope.reason ?? "unavailable"} — ${envelope.detail ?? ""}`.trim();
    }
  }
  return "not measured";
}

/**
 * What the preview ACTUALLY READ, reading by reading.
 *
 * An absent reading is said to be absent: `null` means "not computed", and
 * confusing it with `0` would read as "nothing to report" where nobody looked.
 * Same rule as the health counters.
 */
function describePreview(preview: MappingPreview): string {
  const parts: string[] = [];
  const gate = preview.gate as { passed?: boolean } | null | undefined;
  if (gate && typeof gate.passed === "boolean") {
    parts.push(gate.passed ? "required fields covered" : "required fields MISSING");
  }
  if (Array.isArray(preview.coercions)) {
    parts.push(`${preview.coercions.length} coercions`);
  }
  if (Array.isArray(preview.units)) {
    parts.push(`${preview.units.length} unit/currency declarations`);
  }
  const rows = preview.row_validation as { rejected?: number } | null | undefined;
  if (rows && typeof rows.rejected === "number") {
    parts.push(`${rows.rejected} rows rejected`);
  }
  if (preview.classification) {
    parts.push("sensitive classification available");
  }
  if (preview.placement) {
    parts.push("identity/grain declared");
  }
  if (preview.blocked) {
    parts.push(`blocked: ${preview.blocked_reason ?? "unknown"}`);
  }
  return parts.length > 0 ? parts.join(", ") : "nothing measured";
}

export default function WorkbenchDeliveryPanel({
  datastreamId,
  connectorName,
  channels,
  onRepairMapping,
}: {
  datastreamId: string;
  connectorName: string;
  /** Declared delivery channels, from the Workbench header identity. */
  channels: string[];
  onRepairMapping?: (rawImportId: string) => void;
}) {
  const [credentials, setCredentials] = useState<DeliveryCredential[] | null>(
    null,
  );
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [issued, setIssued] = useState<IssuedCredential | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const [received, setReceived] = useState<ReceivedAttachment[] | null>(null);
  const [receivedError, setReceivedError] = useState<string | null>(null);
  const mounted = useRef(true);
  const loadGeneration = useRef(0);
  const mutationInFlight = useRef(false);
  const mutationKeys = useRef(new Map<string, string>());

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      const generation = ++loadGeneration.current;
      const current = () =>
        mounted.current &&
        generation === loadGeneration.current &&
        !signal?.aborted;
      try {
        const rows = await listDeliveryCredentials(
          connectorName,
          datastreamId,
          signal,
        );
        if (!current()) return;
        setCredentials(rows);
        setLoadError(null);
      } catch (error) {
        if (!current()) return;
        // An empty list and an unreadable one are different answers, and the
        // second must not render as "no address yet".
        setCredentials(null);
        setLoadError(message(error));
      }
      try {
        const items = await listReceivedAttachments(
          connectorName,
          datastreamId,
          signal,
        );
        if (!current()) return;
        setReceived(items);
        setReceivedError(null);
      } catch (error) {
        if (!current()) return;
        // Read separately from the credentials: an unreadable inbox must not
        // make an issued address look absent, nor the reverse.
        setReceived(null);
        setReceivedError(message(error));
      }
    },
    [connectorName, datastreamId],
  );

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void reload(controller.signal);
    return () => {
      mounted.current = false;
      ++loadGeneration.current;
      controller.abort();
      // The secret never outlives the panel.
      setIssued(null);
    };
  }, [reload]);

  const keyFor = (action: string) => {
    const existing = mutationKeys.current.get(action);
    if (existing) return existing;
    const created = newIdempotencyKey("inbound-" + action.split(":")[0]);
    mutationKeys.current.set(action, created);
    return created;
  };

  const run = useCallback(
    async (
      key: string,
      action: (idempotencyKey: string) => Promise<string | null>,
    ) => {
      if (mutationInFlight.current || issued?.full_secret) return;
      mutationInFlight.current = true;
      setBusy(key);
      setActionError(null);
      try {
        const notice = await action(keyFor(key));
        mutationKeys.current.delete(key);
        await reload();
        if (mounted.current && notice) setActionError(notice);
      } catch (error) {
        if (mounted.current) setActionError(message(error));
      } finally {
        mutationInFlight.current = false;
        if (mounted.current) setBusy(null);
      }
    },
    [issued?.full_secret, reload],
  );

  const onIssue = (channel: "email" | "webhook") =>
    run("issue:" + channel, async (idempotencyKey) => {
      const result = await issueDeliveryCredential(
        connectorName,
        datastreamId,
        channel,
        idempotencyKey,
      );
      if (!result.full_secret)
        return "The credential is committed, but its one-time secret was already shown. The committed state has been reloaded.";
      if (mounted.current) {
        setIssued(result);
        setCopied(false);
      }
      return null;
    });

  const onRotate = (credentialId: string) =>
    run("rotate:" + credentialId, async (idempotencyKey) => {
      const result = await rotateDeliveryCredential(
        connectorName,
        datastreamId,
        credentialId,
        idempotencyKey,
      );
      if (!result.full_secret)
        return "The rotation is committed, but its one-time secret was already shown. The committed state has been reloaded.";
      if (mounted.current) {
        setIssued(result);
        setCopied(false);
      }
      return null;
    });

  const onRevoke = (credentialId: string) =>
    run("revoke:" + credentialId, async (idempotencyKey) => {
      await revokeDeliveryCredential(
        connectorName,
        datastreamId,
        credentialId,
        idempotencyKey,
      );
      if (mounted.current) setConfirmRevoke(null);
      return null;
    });

  /**
   * LA REPRISE D'UNE LIVRAISON RETENUE — le chemin que le serveur offrait et
   * que rien n'atteignait.
   *
   * `inbound_reprocess_api.py` sert la proposition et son exécution depuis la
   * story 38.18. Mesuré le 2026-08-08 : aucun appelant côté console, et trois
   * livraisons arrêtées en `PROCESSING` sur un flux réel, leurs octets
   * conservés, sans rien à cliquer.
   *
   * LA PROPOSITION SE LIT D'ABORD, et elle n'écrit rien. Elle porte son
   * indisponibilité comme une valeur, donc un refus s'affiche AVEC SA RAISON
   * plutôt que sous la forme d'un bouton qui échouera. C'est la différence
   * entre proposer une action et proposer une action qui peut aboutir.
   */
  /**
   * LA CHRONOLOGIE D'UNE LIVRAISON — l'autre moitie de l'AC2 de 38-15.
   *
   * `GET .../deliveries/{receipt_id}` est servi depuis 38.14 et n'avait aucun
   * appelant. L'inbox liste les pieces jointes d'un flux ; ceci repond a la
   * question qu'un operateur pose devant une livraison arretee -- << qu'est-ce
   * que CELLE-CI a produit ? >>
   *
   * `published_data_changed` est rendu tel que le serveur le DECLARE. Le
   * recalculer depuis un etat ferait de cet ecran la seconde surface d'un
   * desaccord que le contrat existe pour empecher.
   */
  const [timelines, setTimelines] = useState<Record<string, DeliveryTimeline>>({});

  const onInspectDelivery = (receiptId: string) =>
    run("delivery:" + receiptId, async () => {
      const timeline = await readDeliveryTimeline(
        connectorName,
        datastreamId,
        receiptId,
      );
      if (mounted.current) {
        setTimelines((current) => ({ ...current, [receiptId]: timeline }));
      }
      return null;
    });

  /**
   * LE CONTEXTE DE REPARATION D'UN MAPPING -- 38-16 AC1, << rien n'est monte >>.
   *
   * Trois questions, une reponse : ce que le fichier CONTENAIT, ce a quoi il est
   * EPINGLE, et COMMENT en changer. La troisieme est un RENVOI vers le moteur
   * existant (`datastream_change`), jamais un second chemin de publication --
   * ce serait le quatrieme moteur que cette epique existe pour eviter.
   */
  const [mappingContext, setMappingContext] =
    useState<Record<string, MappingRepairContext>>({});

  /**
   * LA SANTE DU CONNECTEUR -- 38-14 AC5, << `/health` n'a aucun consommateur >>.
   *
   * Lue au montage, comme les credentials et l'inbox : une sante qu'il faut
   * demander est une sante que personne ne regarde.
   *
   * `overall` est rendu TEL QUEL. Il n'est pas recalcule depuis les couches,
   * parce que l'ordre qui le decide est declare cote serveur -- un `false` bat
   * un `unknown`, un `unknown` bat `healthy` -- et deux calculs finiraient par
   * ne plus etre d'accord. `unknown` est rendu comme un TROISIEME etat, jamais
   * replie sur vert : << nothing is ever reported green on missing evidence >>.
   */
  const [health, setHealth] = useState<InboundHealth | null>(null);
  /**
   * Le rapport de routage — 38-15 AC5.
   *
   * Il ne se lance PAS au montage. C'est une question qu'on pose, pas un état
   * qu'on observe : il dépense un événement de cadencement, et un écran qui le
   * déclencherait à chaque ouverture consommerait le quota de livraison de
   * l'opérateur sans qu'il ait rien demandé.
   */
  const [routing, setRouting] = useState<RoutingReport | null>(null);
  const [routingError, setRoutingError] = useState<string | null>(null);

  const onTestRouting = useCallback(async () => {
    setBusy("routing");
    setRoutingError(null);
    try {
      setRouting(await runRoutingTest(connectorName, datastreamId, "email"));
    } catch (error) {
      // AUCUN ETAT INVENTE. Un test qui n'a pas pu tourner n'est pas un
      // routage cassé, et l'afficher comme tel enverrait chercher une panne
      // qui n'existe peut-être pas.
      setRoutingError(error instanceof Error ? error.message : "unavailable");
      setRouting(null);
    } finally {
      setBusy(null);
    }
  }, [connectorName, datastreamId]);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const answer = await readInboundHealth(
          connectorName,
          datastreamId,
          controller.signal,
        );
        if (mounted.current) setHealth(answer);
      } catch {
        // Une sante illisible n'est PAS une sante mauvaise, et surtout pas une
        // bonne. Le panneau n'affiche alors rien plutot que d'inventer un etat.
        if (mounted.current) setHealth(null);
      }
    })();
    return () => controller.abort();
  }, [connectorName, datastreamId]);

  const onInspectMapping = (rawImportId: string) =>
    run("mapping:" + rawImportId, async () => {
      const context = await readMappingRepairContext(
        connectorName,
        datastreamId,
        rawImportId,
      );
      if (mounted.current) {
        setMappingContext((current) => ({ ...current, [rawImportId]: context }));
      }
      return null;
    });

  const [proposals, setProposals] = useState<Record<string, ReprocessProposal>>({});
  const [reprocessReason, setReprocessReason] = useState<Record<string, string>>({});
  /**
   * 38-18 AC1 : la version SOUS LAQUELLE le fichier serait rejoué.
   *
   * Vide = la paire en vigueur. Le rejeu était câblé sur `current_*`, donc il ne
   * répondait qu'à « rejoue le mapping d'aujourd'hui » — ce qui ne récupère rien
   * quand le mapping d'aujourd'hui EST le problème.
   */
  const [reprocessTarget, setReprocessTarget] = useState<Record<string, string>>({});

  /**
   * `target` est passe EXPLICITEMENT, jamais relu depuis l'etat.
   *
   * `setReprocessTarget` est asynchrone : appeler cette fonction juste apres
   * lisait la selection PRECEDENTE, donc choisir une version relisait la
   * proposition sous l'ancienne. La personne aurait confirme autre chose que ce
   * qu'elle voyait a l'ecran.
   */
  const onInspectReprocess = (rawImportId: string, target?: string) =>
    run("inspect:" + rawImportId, async () => {
      const proposal = await readReprocessProposal(
        connectorName,
        datastreamId,
        rawImportId,
        target ?? reprocessTarget[rawImportId] ?? undefined,
      );
      if (mounted.current) {
        setProposals((current) => ({ ...current, [rawImportId]: proposal }));
      }
      return null;
    });

  const onExecuteReprocess = (rawImportId: string) =>
    run("reprocess:" + rawImportId, async (idempotencyKey) => {
      // La raison est exigée par le serveur -- << a reprocess changes published
      // data >>. La demander ICI plutôt que de laisser partir une chaîne vide
      // évite de rendre `missing_reason` à quelqu'un qui n'a jamais vu la
      // question.
      await executeReprocess(
        connectorName,
        datastreamId,
        rawImportId,
        (reprocessReason[rawImportId] ?? "").trim(),
        idempotencyKey,
        reprocessTarget[rawImportId] || undefined,
      );
      if (mounted.current) {
        setProposals((current) => {
          const next = { ...current };
          delete next[rawImportId];
          return next;
        });
      }
      return null;
    });

  /**
   * LA PORTÉE GOUVERNÉE — 38-18 AC1, « and a governed scope ».
   *
   * Le rejeu ne portait que sur UN `raw_import`, donc il n'existait pas de
   * portée du tout : réparer un mapping et devoir cliquer douze fois n'est pas
   * une portée, c'est douze actes muets dont aucune trace ne dit qu'ils étaient
   * la même réparation.
   *
   * Le patron est celui du dépôt, pas un second :
   * `datastream-workbench-and-wizard.md` — « Prepare > Review exact scope and
   * consequences > Confirm > Execute as a new durable operation » — et la même
   * page pour ce que « review exact scope » veut dire : **le compte AVANT
   * l'acte**, et les objets qu'il nomme. Mesuré 2026-08-10 aux lignes 2191 et
   * 1080 ; ces numéros bougent, la phrase non.
   */
  const [scopeSelected, setScopeSelected] = useState<Record<string, boolean>>({});
  const [scopeProposal, setScopeProposal] =
    useState<ReprocessScopeProposal | null>(null);
  const [scopeReason, setScopeReason] = useState("");
  const selectedScopeIds = Object.keys(scopeSelected).filter(
    (id) => scopeSelected[id],
  );

  const onPrepareScope = () =>
    run("scope:prepare", async () => {
      const proposal = await readReprocessScope(
        connectorName,
        datastreamId,
        selectedScopeIds,
      );
      if (mounted.current) setScopeProposal(proposal);
      return null;
    });

  const onExecuteScope = () =>
    run("scope:execute", async (idempotencyKey) => {
      // La confirmation porte sur les objets ÉNUMÉRÉS par la proposition, pas
      // sur la sélection à l'écran : entre les deux, un fichier a pu devenir
      // illisible, et le serveur refuse alors la portée entière plutôt que d'en
      // rejouer onze sur douze.
      await executeReprocessScope(
        connectorName,
        datastreamId,
        scopeProposal?.selection.confirm_raw_import_ids ?? [],
        scopeReason.trim(),
        idempotencyKey,
      );
      if (mounted.current) {
        setScopeProposal(null);
        setScopeSelected({});
        setScopeReason("");
      }
      return null;
    });

  const onRecoverScanJob = (jobId: string) =>
    run("recover:" + jobId, async (idempotencyKey) => {
      await recoverInboundScanJob(
        connectorName,
        datastreamId,
        jobId,
        idempotencyKey,
      );
      return null;
    });

  const onCopy = async () => {
    if (!issued?.full_secret) return;
    try {
      await navigator.clipboard.writeText(issued.full_secret);
      if (mounted.current) setCopied(true);
    } catch {
      // Clipboard permission is a browser decision, not a failure of the
      // operation. The address stays selectable on screen either way.
      if (mounted.current) setCopied(false);
    }
  };

  const live = (credentials ?? []).filter(
    (credential) =>
      credential.state === "ACTIVE" || credential.state === "ROTATING",
  );
  const availableChannels = configuredChannels(channels);
  const issuedChannels = new Set(live.map((credential) => credential.channel));
  const missingChannels = availableChannels.filter(
    (channel) => !issuedChannels.has(channel),
  );
  const mutationsDisabled = busy !== null || Boolean(issued?.full_secret);
  const revealedKind =
    issued?.channel === "email" ? "email address" : "webhook secret";

  return (
    <Panel flush>
      <PanelHeader
        title="Delivery address"
        description="Files arrive at this address. It is a delivery credential: anyone holding it can deliver into this Datastream."
      />

      <div className="grid gap-4 p-5">
        <p className="m-0 text-ui text-text-secondary">
          Declared channels:{" "}
          {channels.length ? channels.join(", ") : "None declared"}
        </p>

        {/* LA SANTE, rendue telle que le serveur la DECIDE. `unknown` est un
            troisieme etat et porte le ton `neutral` : le replier sur vert
            inventerait une sante que personne n'a mesuree, et le replier sur
            rouge ferait courir apres une panne qui n'existe pas. */}
        <div className="flex flex-col gap-1" data-testid="routing-test">
          {/*
            LA QUESTION QU'ON POSE AVANT D'ENVOYER. La santé dit si le
            connecteur va bien ; ceci dit si CE Datastream-ci recevrait quelque
            chose. Les deux peuvent diverger, et de l'extérieur les deux pannes
            se ressemblent : rien n'arrive.
          */}
          <Button
            type="button"
            variant="ghost"
            onClick={onTestRouting}
            disabled={busy === "routing"}
          >
            {busy === "routing"
              ? "Walking the routing chain..."
              : "Would a delivery reach this Datastream?"}
          </Button>
          {routingError && (
            <p className="m-0 text-ui text-text-secondary">
              The routing test could not run ({routingError}). That is not a
              routing failure.
            </p>
          )}
          {routing && (
            <div className="flex flex-col gap-1">
              <p className="m-0 text-ui text-text-secondary">
                {routing.routes
                  ? `A delivery on ${routing.channel} would reach this Datastream (synthetic test — no delivery evidence was created).`
                  : `A delivery on ${routing.channel} would NOT arrive: ${routing.blocking_reason ?? "unknown"}.`}
              </p>
              {/*
                WHAT THE TEST COSTS, TOLD TO WHOEVER PAYS IT. This screen used to
                display "nothing was written". The test calls
                `resolve_by_token_hash`, which RECORDS a rate-limit event
                (`inbound_credentials._record_resolution_rate_event`), and the
                server commits it. That is deliberate -- routing around the
                throttle would duplicate security logic and report "routing
                works" for a Datastream that is precisely over budget -- but the
                consequence is real: repeated, the test exhausts the resolution
                budget and `_enforce_resolution_rate_limit` then refuses REAL
                deliveries. The operator had no way to know.
              */}
              <p
                className="m-0 text-ui text-text-secondary"
                data-testid="routing-test-cost"
              >
                This test spends one resolution event against this Datastream's
                rate limit — the same budget real deliveries draw on. It creates
                no receipt, no raw import, no execution and no publication.
              </p>
              {routing.steps.map((step: RoutingStep) => (
                <p
                  key={step.step}
                  className="m-0 text-ui text-text-secondary"
                  data-testid={`routing-step-${step.step}`}
                >
                  {/*
                    Trois états, jamais deux : `null` veut dire NON ATTEINT.
                    Une étape que personne n'a essayée n'est pas en panne.
                  */}
                  {step.passed === true ? "ok" : step.passed === false ? "blocked" : "not reached"}
                  {" — "}
                  {ROUTING_STEP_LABEL[step.step] ?? step.step}
                </p>
              ))}
            </div>
          )}
        </div>
        {health && (
          <Status
            as="block"
            tone={
              health.overall === "blocked"
                ? "error"
                : health.overall === "healthy"
                  ? "success"
                  : "neutral"
            }
            title={`Inbound health: ${health.overall}`}
            data-testid="inbound-health"
          >
            {health.blocking_cause
              ? `${health.blocking_cause} - ${health.authority} owns this`
              : "No blocking cause reported."}
            {/*
              LA PHRASE, PUIS L'ACTION. `next_action` dit ce qui devrait
              arriver ; l'AC4 demande que l'alerte POINTE la reparation. Une
              couche qui n'en propose pas -- saine, ou illisible -- n'affiche
              rien de plus : offrir un bouton devant une base injoignable
              invite a le presser en boucle.
            */}
            {(health.layers ?? [])
              .filter((layer) => layer.next_action)
              .map(
                (layer) =>
                  ` ${layer.layer}: ${layer.next_action}` +
                  (layer.recovery?.mcp_tool || layer.recovery?.api
                    ? ` [${layer.authority} runs ${
                        layer.recovery.mcp_tool ?? layer.recovery.api
                      }]`
                    : ""),
              )
              .join("")}
          </Status>
        )}

        {/*
          THE COUNTERS, RENDERED. 38.14 AC3 names seven readings; they were
          served on `/health` and `grep -n "metrics" WorkbenchDeliveryPanel.tsx`
          returned ZERO usage. A measurement no screen reads is not
          observability, it is a route.

          NEVER A ZERO FOR WHAT COULD NOT BE MEASURED: the server returns `null`
          when there is no denominator, and an `{available:false, reason}`
          object for what CANNOT exist (a delivery refused at the credential
          creates no receipt). The screen repeats the distinction instead of
          flattening it.
        */}
        {health?.metrics && (
          <div className="flex flex-col gap-1" data-testid="inbound-metrics">
            <p className="m-0 text-ui text-text-secondary">
              Over the last {formatMetric(health.metrics.window_days)} days:{" "}
              {formatMetric(health.metrics.deliveries_total)} deliveries,{" "}
              {formatMetric(health.metrics.attachments_total)} attachments,{" "}
              {formatMetric(health.metrics.parser_failures)} parser failures.
            </p>
            <p className="m-0 text-ui text-text-secondary">
              Duplicate arrivals: {formatMetric(health.metrics.duplicate_attachments)}{" "}
              ({formatRate(health.metrics.duplicate_rate)}) across{" "}
              {formatMetric(health.metrics.repeated_content_hashes)} repeated files.
            </p>
            <p className="m-0 text-ui text-text-secondary" data-testid="inbound-latency">
              Processing latency over {formatMetric(health.metrics.settled_receipts)}{" "}
              settled deliveries: median{" "}
              {formatSeconds(health.metrics.processing_latency_median_seconds)}, max{" "}
              {formatSeconds(health.metrics.processing_latency_max_seconds)}.
            </p>
            <p className="m-0 text-ui text-text-secondary">
              Last successful publication:{" "}
              {formatPublication(health.metrics.last_successful_publication)}
            </p>
            <p className="m-0 text-ui text-text-secondary" data-testid="denied-deliveries">
              Denied deliveries: {formatUnavailable(health.metrics.denied_deliveries)}
            </p>
          </div>
        )}

        {loadError && (
          <Status as="block" tone="error" title="Address state unreadable">
            {loadError} — this is not the same as having no address; nothing was
            issued or revoked.
          </Status>
        )}

        {issued?.full_secret && (
          <Status
            as="block"
            tone="warning"
            title={`${revealedKind === "email address" ? "Email address" : "Webhook secret"} shown once — copy it now`}
          >
            <p className="m-0 break-all font-mono text-text">
              {issued.full_secret}
            </p>
            <p className="m-0 mt-2 text-text-secondary">
              This {revealedKind} will not be shown again and cannot be
              retrieved. Treat it as a credential: anyone who has it can deliver
              files into this Datastream. Rotate it if it is ever shared by
              mistake.
            </p>
            <div className="mt-3 flex gap-2">
              <Button type="button" variant="secondary" onClick={onCopy}>
                {copied ? "Copied" : `Copy ${revealedKind}`}
              </Button>
              <Button
                type="button"
                variant="secondary"
                onClick={() => setIssued(null)}
              >
                I have stored it
              </Button>
            </div>
          </Status>
        )}

        {actionError && (
          <Status as="block" tone="error" title="Action failed">
            {actionError}
          </Status>
        )}

        {credentials !== null && missingChannels.length > 0 && !loadError && (
          <EmptyState
            title={
              live.length
                ? "Another delivery channel is available"
                : "No credential issued yet"
            }
            description="Issue each configured channel independently. Issuing a delivery credential does not publish anything: the first delivered file is described for review before any data is used."
            action={
              <div className="flex flex-wrap gap-2">
                {missingChannels.map((channel) => (
                  <Button
                    key={channel}
                    type="button"
                    onClick={() => onIssue(channel)}
                    disabled={mutationsDisabled}
                  >
                    {busy === "issue:" + channel
                      ? "Issuing…"
                      : channel === "email"
                        ? "Issue email address"
                        : "Issue webhook credential"}
                  </Button>
                ))}
              </div>
            }
          />
        )}

        {live.map((credential) => (
          <div
            key={credential.credential_id}
            className="grid gap-3 border-t border-divider-base pt-4 first:border-t-0 first:pt-0"
          >
            <div className="flex flex-wrap items-center gap-3">
              <Badge tone={stateTone(credentialWord(credential.state))}>
                {stateLabel(credentialWord(credential.state))}
              </Badge>
              <span className="font-mono text-ui text-text">
                …{credential.safe_suffix ?? "unknown"}
              </span>
              <span className="text-ui text-text-secondary">
                version {credential.version}
              </span>
            </div>
            <dl className="grid grid-cols-3 gap-4 text-ui max-md:grid-cols-1">
              <div>
                <dt className="text-text-secondary">Channel</dt>
                <dd className="m-0 text-text">{credential.channel}</dd>
              </div>
              <div>
                <dt className="text-text-secondary">Expires</dt>
                <dd className="m-0 text-text">{when(credential.expires_at)}</dd>
              </div>
              <div>
                <dt className="text-text-secondary">Overlap until</dt>
                <dd className="m-0 text-text">
                  {when(credential.overlap_until)}
                </dd>
              </div>
            </dl>

            {confirmRevoke === credential.credential_id ? (
              <Status as="block" tone="error" title="Revoke this address?">
                Deliveries to it stop immediately and it cannot be restored.
                Files already received are unaffected. Senders will need the new
                address.
                <div className="mt-3 flex gap-2">
                  <Button
                    type="button"
                    variant="destructive"
                    onClick={() => onRevoke(credential.credential_id)}
                    disabled={mutationsDisabled}
                  >
                    {busy === `revoke:${credential.credential_id}`
                      ? "Revoking…"
                      : "Revoke permanently"}
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => setConfirmRevoke(null)}
                  >
                    Keep it
                  </Button>
                </div>
              </Status>
            ) : (
              <div className="flex flex-wrap gap-2">
                {credential.state === "ACTIVE" && (
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => onRotate(credential.credential_id)}
                    disabled={mutationsDisabled}
                  >
                    {busy === "rotate:" + credential.credential_id
                      ? "Rotating…"
                      : "Rotate"}
                  </Button>
                )}
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setConfirmRevoke(credential.credential_id)}
                  disabled={mutationsDisabled}
                >
                  Revoke
                </Button>
              </div>
            )}
          </div>
        ))}

        {credentials === null && !loadError && (
          <p className="m-0 text-ui text-text-secondary">
            Loading address state…
          </p>
        )}
      </div>

      <PanelHeader
        title="Received files"
        description="One entry per attachment, newest first — including the deliveries that never landed, which are the ones worth finding."
      />

      <div className="grid gap-3 p-5">
        {receivedError && (
          <Status as="block" tone="error" title="Delivery history unreadable">
            {receivedError} — this is not the same as having received nothing.
          </Status>
        )}

        {received !== null && received.length === 0 && !receivedError && (
          <EmptyState
            title="Nothing delivered yet"
            description="Send a file to the address above. The first one is described here for review; nothing is published until you validate it."
          />
        )}

        {/*
          PREPARE > REVIEW EXACT SCOPE > CONFIRM > EXECUTE. Le bouton ne rejoue
          rien : il PRÉPARE, et la proposition qui revient porte le compte et les
          objets. Confirmer une portée sans les avoir lus serait le geste que le
          patron ratifié existe pour empêcher.
        */}
        {selectedScopeIds.length > 0 && !scopeProposal && (
          <div
            className="flex flex-wrap items-center gap-3"
            data-testid="scope-bar"
          >
            <span className="text-ui text-text-secondary">
              {selectedScopeIds.length} selected
            </span>
            <Button
              type="button"
              variant="secondary"
              onClick={() => void onPrepareScope()}
              disabled={mutationsDisabled}
            >
              {busy === "scope:prepare"
                ? "Reading the retained bytes…"
                : "Prepare a scoped reprocess"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setScopeSelected({})}
              disabled={mutationsDisabled}
            >
              Clear selection
            </Button>
          </div>
        )}

        {scopeProposal && (
          <div
            className="grid gap-2 rounded-md border border-divider-base p-3"
            data-testid="scope-proposal"
          >
            {/*
              LE COMPTE AVANT L'ACTE, dans les mots de l'effet. « N of M » est le
              gabarit du dépôt (« N of 46 columns stop landing ») : le nombre qui
              va arriver, sur le nombre examiné.
            */}
            <p className="m-0 text-ui text-text">
              {scopeProposal.scope.reprocessable} of{" "}
              {scopeProposal.scope.examined} selected file(s) will be replayed as
              ONE durable operation.
              {scopeProposal.scope.refused > 0
                ? ` ${scopeProposal.scope.refused} cannot be, and is listed below with its reason.`
                : ""}
            </p>
            {/* LA BORNE EST DITE. Jamais une troncature muette. */}
            {scopeProposal.scan_truncated && (
              <Status as="block" tone="warning" title="Scope truncated">
                More files match than this scope can carry; only the first{" "}
                {scopeProposal.scan_limit} were examined. Narrow the selection
                and prepare again — the rest were NOT included.
              </Status>
            )}
            <p className="m-0 text-ui text-text-secondary">
              Replays under mapping{" "}
              {scopeProposal.bound_versions?.mapping_version_id ?? "none"} and
              plan {scopeProposal.bound_versions?.plan_version_id ?? "none"} —{" "}
              {describeTemplateVersion(scopeProposal.template_version)}. It
              creates a NEW execution and may move the published pointer; no
              prior execution is mutated.
            </p>
            <div className="grid gap-1">
              {scopeProposal.members.map((member) => (
                <p
                  key={member.raw_import_id}
                  className="m-0 text-ui text-text-secondary"
                  data-testid={`scope-member-${member.raw_import_id}`}
                >
                  {/* A NAME READS AS A NAME, AN ADDRESS READS AS AN ADDRESS.
                      `inbound_raw_imports.filename` is nullable on purpose
                      (migration 171: an attachment can arrive with none), so
                      this absence is real and the id is the only thing left
                      that identifies the delivery. It is kept, and MARKED --
                      the distinction `visualization-and-rendering.md` draws
                      between an identifier standing where a word belongs and
                      one presented as the token it is. Falling straight through
                      from the file name to the raw import's id was neither: a
                      `raw_<ULID>` in the same voice as a file name, in the
                      sentence a person reads before replaying it.
                      (The census of this class reads whole files and cannot
                      tell code from prose, so an example spelt out here would
                      count as a site of its own.) */}
                  {member.filename ?? (
                    <ObjectId value={member.raw_import_id} title="Import" />
                  )}
                  {" — "}
                  {member.available
                    ? "will be replayed"
                    : `will NOT be replayed: ${member.reason}${
                        member.detail ? ` (${member.detail})` : ""
                      }`}
                </p>
              ))}
            </div>
            <Field label="Reason">
              {(fieldProps) => (
                <Input
                  {...fieldProps}
                  value={scopeReason}
                  onChange={(event) => setScopeReason(event.target.value)}
                  placeholder="Why this scope is being replayed"
                />
              )}
            </Field>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                type="button"
                variant="default"
                onClick={() => void onExecuteScope()}
                disabled={
                  mutationsDisabled ||
                  !scopeReason.trim() ||
                  scopeProposal.scope.reprocessable === 0
                }
              >
                {busy === "scope:execute"
                  ? "Replaying…"
                  : `Confirm reprocess of ${scopeProposal.scope.reprocessable} file(s)`}
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setScopeProposal(null)}
                disabled={mutationsDisabled}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {(received ?? []).map((item) => {
          const refused = item.state === "REJECTED" || item.state === "FAILED";
          return (
            <div
              key={
                item.raw_import_id ??
                `${item.receipt?.receipt_id ?? "unknown"}:${item.ordinal}`
              }
              className="grid gap-2 border-t border-divider-base pt-3 first:border-t-0 first:pt-0"
            >
              <div className="flex flex-wrap items-center gap-3">
                {item.raw_import_id && (
                  // Le choix EXPLICITE de ce qui entre dans la portée. Une
                  // portée déduite d'un filtre à l'écran replaierait ce qu'un
                  // tri a décidé, pas ce qu'une personne a désigné.
                  <Checkbox
                    aria-label={`Include ${
                      item.filename ?? item.raw_import_id
                    } in the reprocess scope`}
                    data-testid={`scope-select-${item.raw_import_id}`}
                    checked={Boolean(scopeSelected[item.raw_import_id])}
                    onCheckedChange={(value) =>
                      setScopeSelected((current) => ({
                        ...current,
                        [item.raw_import_id!]: value === true,
                      }))
                    }
                  />
                )}
                <Badge
                  tone={
                    refused
                      ? "error"
                      : item.state === "LANDED"
                        ? "success"
                        : "neutral"
                  }
                >
                  {stateLabel(item.state)}
                </Badge>
                <span className="text-ui text-text">
                  {item.filename ?? "Unnamed file"}
                </span>
                <span className="text-ui text-text-secondary">
                  {when(item.created_at)}
                </span>
              </div>
              {refused && (
                // The reason a file was refused, without downloading a byte of
                // it (Story 38.10 AC5). A state with no cause is what sends an
                // operator to ask the sender to "try again".
                <p className="m-0 text-ui text-text-secondary">
                  Refused: {item.error_code ?? "reason unavailable"}
                  {item.media_type_declared &&
                  item.media_type_detected &&
                  item.media_type_declared !== item.media_type_detected
                    ? ` — declared ${item.media_type_declared}, detected ${item.media_type_detected}`
                    : ""}
                </p>
              )}
              {item.state === "ACCEPTED" && (
                <p className="m-0 text-ui text-text-secondary">
                  Retained for setup review. Its shape is what the wizard reads
                  to propose a mapping; nothing is published until you validate.
                </p>
              )}
              {item.scan_job && item.scan_job.state !== "SUCCEEDED" && (
                <p
                  className="m-0 text-ui text-text-secondary"
                  data-testid="scan-job-evidence"
                >
                  Scan job: {item.scan_job.state}
                  {item.scan_job.attempt_count != null &&
                  item.scan_job.max_attempts != null
                    ? ` (${item.scan_job.attempt_count}/${item.scan_job.max_attempts} attempts)`
                    : ""}
                  {item.scan_job.error_code
                    ? ` - ${item.scan_job.error_code}`
                    : ""}
                  {item.scan_job.state === "DEAD_LETTER" &&
                  item.scan_job.recovery?.command
                    ? `; recovery: ${item.scan_job.recovery.command} (authorization required)`
                    : ""}
                </p>
              )}
              {item.scan_job?.state === "DEAD_LETTER" &&
                item.scan_job.recovery?.job_id && (
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() =>
                      onRecoverScanJob(item.scan_job!.recovery!.job_id!)
                    }
                    disabled={mutationsDisabled}
                  >
                    {busy === `recover:${item.scan_job.recovery.job_id}`
                      ? "Queuing retry..."
                      : "Retry scan"}
                  </Button>
                )}
              {item.receipt?.receipt_id && (
                <div
                  className="flex flex-col gap-1"
                  data-testid={`delivery-${item.receipt.receipt_id}`}
                >
                  {!timelines[item.receipt.receipt_id] ? (
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => onInspectDelivery(item.receipt!.receipt_id)}
                      disabled={mutationsDisabled}
                    >
                      {busy === `delivery:${item.receipt.receipt_id}`
                        ? "Reading the delivery..."
                        : "What did this delivery produce?"}
                    </Button>
                  ) : (
                    <>
                      <p className="m-0 text-ui text-text-secondary">
                        {timelines[item.receipt.receipt_id].landed_count} of{" "}
                        {timelines[item.receipt.receipt_id].attachment_count}{" "}
                        attachment(s) landed,{" "}
                        {timelines[item.receipt.receipt_id].published_count}{" "}
                        published
                        {"; "}
                        {/* DECLARE par le serveur, jamais deduit ici. */}
                        {timelines[item.receipt.receipt_id].published_data_changed
                          ? "published data changed"
                          : "published data unchanged"}
                        {timelines[item.receipt.receipt_id].receipt.import_ledger_id
                          ? ` (${timelines[item.receipt.receipt_id].receipt.import_ledger_id})`
                          : ""}
                      </p>
                      {/*
                        LA MOITIE AVAL DE LA COLONNE VERTEBRALE. « Atterri » ne
                        dit pas ce que la piece jointe est DEVENUE : combien de
                        lignes sont passees, combien ont ete rejetees, et si
                        quoi que ce soit est en ligne. Le serveur le servait ;
                        aucun ecran ne le demandait.
                      */}
                      {timelines[item.receipt.receipt_id].attachments
                        .filter((attachment) => attachment.downstream)
                        .map((attachment) => {
                          const stages = attachment.downstream!;
                          const accepted = stages.dq.accepted_row_count;
                          return (
                            <p
                              key={`downstream-${attachment.ordinal}`}
                              className="m-0 text-ui text-text-secondary"
                              data-testid={`downstream-${attachment.ordinal}`}
                            >
                              {attachment.filename ?? `attachment ${attachment.ordinal}`}
                              {" — "}
                              {accepted === null
                                ? "no rows counted yet"
                                : `${accepted} row(s) accepted, ${stages.dq.rejected_row_count} rejected`}
                              {stages.dq.rejected_row_pct !== null
                                ? ` (${stages.dq.rejected_row_pct}%)`
                                : ""}
                              {"; "}
                              {OUTCOME_SENTENCE[stages.publication.outcome ?? ""] ??
                                stages.publication.outcome ??
                                "outcome unknown"}
                              {stages.publication.error_code
                                ? ` (${stages.publication.error_code})`
                                : ""}
                              {stages.mapping.mapping_version_id
                                ? ` — mapping ${stages.mapping.mapping_version_id}`
                                : ""}
                            </p>
                          );
                        })}
                    </>
                  )}
                </div>
              )}
              {item.raw_import_id && (
                <div
                  className="flex flex-col gap-1"
                  data-testid={`mapping-${item.raw_import_id}`}
                >
                  {!mappingContext[item.raw_import_id] ? (
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => onInspectMapping(item.raw_import_id!)}
                      disabled={mutationsDisabled}
                    >
                      {busy === `mapping:${item.raw_import_id}`
                        ? "Reading the file's columns..."
                        : "What is this mapped against?"}
                    </Button>
                  ) : !mappingContext[item.raw_import_id].available ? (
                    <Status as="block" tone="warning" title="No mapping context">
                      {mappingContext[item.raw_import_id].reason}
                    </Status>
                  ) : (
                    <div className="flex flex-col gap-1">
                      {/*
                        COLUMNS, TYPES AND SAMPLES -- 38.16 AC1 asks for all
                        three. The server has sent all three since the screen was
                        mounted; only the names were read, and they were read by
                        `.join(", ")` over a list of OBJECTS, which rendered
                        `[object Object], [object Object]`.
                      */}
                      {(mappingContext[item.raw_import_id].source_columns ?? []).length === 0 ? (
                        <p className="m-0 text-ui text-text-secondary">
                          Columns in the file: not readable from the retained bytes
                        </p>
                      ) : (
                        <div className="flex flex-col gap-1">
                          <p className="m-0 text-ui text-text-secondary">
                            Columns in the file (
                            {(mappingContext[item.raw_import_id].source_columns ?? []).length}
                            ), with the types read from it and masked samples:
                          </p>
                          {(mappingContext[item.raw_import_id].source_columns ?? []).map(
                            (column) => (
                              <p
                                key={`${column.index}-${column.name}`}
                                className="m-0 text-ui text-text-secondary"
                                data-testid={`source-column-${column.name}`}
                              >
                                <span className="font-mono">{column.name}</span>
                                {column.source_label ? ` (${column.source_label})` : ""}
                                {" — "}
                                {column.detected_type ?? "type not detected"}
                                {/*
                                  UN ECHANTILLON EST DEJA MASQUE PAR LE SERVEUR.
                                  Aucune valeur brute du fournisseur n'arrive
                                  ici, et l'ecran n'en redemande pas.
                                */}
                                {column.masked_samples && column.masked_samples.length > 0
                                  ? ` — e.g. ${column.masked_samples
                                      .filter((sample) => sample !== null)
                                      .join(" | ")}`
                                  : ""}
                              </p>
                            ),
                          )}
                        </div>
                      )}
                      {/*
                        THE SEVEN AC3 READINGS, on the already-retained bytes.
                        A reason when there is none -- never "0 issues" for a
                        Datastream that carries no template.
                      */}
                      {mappingContext[item.raw_import_id].preview && (
                        <p
                          className="m-0 text-ui text-text-secondary"
                          data-testid={`mapping-preview-${item.raw_import_id}`}
                        >
                          {mappingContext[item.raw_import_id].preview!.available
                            ? `Preview on the retained file: ${describePreview(
                                mappingContext[item.raw_import_id].preview!,
                              )}`
                            : `No preview: ${
                                mappingContext[item.raw_import_id].preview!.reason
                              } — nothing was assumed in its place.`}
                        </p>
                      )}
                      <p className="m-0 text-ui text-text-secondary">
                        Pinned against plan{" "}
                        {mappingContext[item.raw_import_id].pinned_versions?.plan_version_id
                          ?? "none"}{" "}
                        and mapping{" "}
                        {mappingContext[item.raw_import_id].pinned_versions?.mapping_version_id
                          ?? "none"}
                      </p>
                      {/* Le RENVOI, pas un second moteur : la story le refuse
                          explicitement. L'ecran nomme le chemin gouverne. */}
                      {mappingContext[item.raw_import_id].governed_path && (
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="m-0 text-ui text-text-secondary">
                            Changing it is a governed change through{" "}
                            {mappingContext[item.raw_import_id].governed_path!.engine}: prepare,
                            then confirm. This screen does not publish.
                          </p>
                          {onRepairMapping && (
                            <Button
                              type="button"
                              variant="secondary"
                              size="sm"
                              data-testid={`repair-mapping-${item.raw_import_id}`}
                              onClick={() => onRepairMapping(item.raw_import_id!)}
                            >
                              Repair this file's mapping
                            </Button>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
              {item.raw_import_id && (
                <div
                  className="flex flex-col gap-2"
                  data-testid={`reprocess-${item.raw_import_id}`}
                >
                  {!proposals[item.raw_import_id] ? (
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => onInspectReprocess(item.raw_import_id!)}
                      disabled={mutationsDisabled}
                    >
                      {busy === `inspect:${item.raw_import_id}`
                        ? "Reading the retained bytes..."
                        : "Reprocess this delivery"}
                    </Button>
                  ) : !proposals[item.raw_import_id].availability.available ? (
                    // Le refus PORTE SA RAISON. Un bouton grise sans phrase
                    // laisse l'operateur deviner ce qui manque.
                    <Status as="block" tone="warning" title="Cannot be reprocessed">
                      {proposals[item.raw_import_id].availability.reason}
                      {proposals[item.raw_import_id].availability.detail
                        ? ` - ${proposals[item.raw_import_id].availability.detail}`
                        : ""}
                    </Status>
                  ) : (
                    <>
                      <p className="m-0 text-ui text-text-secondary">
                        Replays the retained bytes through the pinned plan and
                        mapping. It creates a NEW execution and may move the
                        published pointer; the prior execution is never mutated.
                      </p>
                      {/*
                        L'AUTRE MOITIÉ DE L'AC1 : sous QUEL Template. Elle n'est
                        pas un paramètre de ce chemin — l'épingle appartient au
                        Datastream, et la bouger est un changement gouverné —
                        mais une personne qui confirme un rejeu a le droit de
                        savoir contre quel contrat il tourne. Quatre états, quatre
                        phrases : jamais une cellule vide pour les quatre.
                      */}
                      <p
                        className="m-0 text-ui text-text-secondary"
                        data-testid={`reprocess-template-${item.raw_import_id}`}
                      >
                        {describeTemplateVersion(
                          proposals[item.raw_import_id!]?.template_version,
                        )}
                      </p>
                      {/*
                        LE CHOIX QUE L'AC1 DEMANDE. Sans liste, le paramètre
                        existe et personne ne peut s'en servir : il faudrait
                        déjà connaître un identifiant `dmv_` pour le taper, donc
                        la seule version atteignable resterait la courante.

                        Les non exécutables restent AFFICHÉES et désactivées.
                        Un brouillon qu'on ne peut pas rejouer est exactement ce
                        qu'on cherche quand on se demande pourquoi son candidat
                        n'est pas proposé ; le retirer transformerait un refus
                        explicable en absence.
                      */}
                      {(proposals[item.raw_import_id!]?.available_versions?.length ?? 0) > 0 && (
                        <label
                          className="flex flex-col gap-1 text-ui text-text-secondary"
                          data-testid={`reprocess-target-${item.raw_import_id}`}
                        >
                          Replay under
                          <NativeSelect
                            value={reprocessTarget[item.raw_import_id!] ?? ""}
                            onChange={(event: React.ChangeEvent<HTMLSelectElement>) => {
                              const value = event.target.value;
                              setReprocessTarget((current) => ({
                                ...current,
                                [item.raw_import_id!]: value,
                              }));
                              // La proposition est RELUE sous la version
                              // choisie : ce qui va arriver change avec elle, et
                              // laisser l'ancienne à l'écran ferait confirmer
                              // autre chose que ce qui est affiché.
                              void onInspectReprocess(item.raw_import_id!, value);
                            }}
                          >
                            <option value="">
                              The version in force (no change)
                            </option>
                            {proposals[item.raw_import_id!]!.available_versions!.map(
                              (version) => (
                                <option
                                  key={version.mapping_version_id}
                                  value={version.mapping_version_id}
                                  disabled={!version.executable}
                                >
                                  v{version.version_number}
                                  {version.is_current ? " (in force)" : ""}
                                  {version.executable
                                    ? ""
                                    : ` — not executable, ${version.blocking_count} blocking`}
                                </option>
                              ),
                            )}
                          </NativeSelect>
                        </label>
                      )}
                      {proposals[item.raw_import_id!]?.target_refused && (
                        <p
                          className="m-0 text-ui text-text-secondary"
                          data-testid={`reprocess-refused-${item.raw_import_id}`}
                        >
                          That version cannot be replayed:{" "}
                          {proposals[item.raw_import_id!]!.target_refused}
                        </p>
                      )}
                      <Field label="Reason">
                        {(fieldProps) => (
                          <Input
                            {...fieldProps}
                            value={reprocessReason[item.raw_import_id!] ?? ""}
                            onChange={(event) =>
                              setReprocessReason((current) => ({
                                ...current,
                                [item.raw_import_id!]: event.target.value,
                              }))
                            }
                            placeholder="Why this delivery is being replayed"
                          />
                        )}
                      </Field>
                      <Button
                        type="button"
                        variant="default"
                        onClick={() => onExecuteReprocess(item.raw_import_id!)}
                        disabled={
                          mutationsDisabled ||
                          !(reprocessReason[item.raw_import_id] ?? "").trim()
                        }
                      >
                        {busy === `reprocess:${item.raw_import_id}`
                          ? "Replaying..."
                          : "Confirm reprocess"}
                      </Button>
                    </>
                  )}
                </div>
              )}
              {item.scan_verdict && (
                <ScanEvidence
                  verdict={item.scan_verdict}
                  fallbackSize={item.size_bytes}
                />
              )}
            </div>
          );
        })}

        {received === null && !receivedError && (
          <p className="m-0 text-ui text-text-secondary">
            Loading delivery history…
          </p>
        )}
      </div>
    </Panel>
  );
}
