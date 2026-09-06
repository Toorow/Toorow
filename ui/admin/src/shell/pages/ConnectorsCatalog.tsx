/**
 * Data -> Connectors — the screen that owes the installation gesture.
 *
 * `docs/product-architecture/execution-substrate.md`, `Incomplete if` 11: "…or
 * registering an installation still requires a REST call because no console
 * surface holds the gesture — the catalogue page `Data -> Connectors` reads
 * `app.connector_installations` and is the screen that owes it." Two of that
 * criterion's three clauses were closed in `core/connector_family.py` and
 * `run_verification`; this file closes the third.
 *
 * The table has always read the installation state. What it could not do was
 * ACT on it: `POST …/installation` and `POST …/verify` had no caller in the
 * console, and production held one installation row because every other one
 * would have had to be created by hand.
 *
 * ONE QUESTION AT A TIME, and each one reduces the next. The setup dialog asks
 * which Connector, and only then — the answer in hand — reads that Connector's
 * state and offers the single gesture that state allows. A row's own button
 * answers the first question by being clicked, so it is never asked twice.
 *
 * NOTHING HERE INVENTS A STATE. The evidence age comes from `ttl_seconds` on the
 * evidence row; a table cell, which has read no evidence, never says a check is
 * overdue; and a refusal is turned into the gesture that repairs it rather than
 * into the platform's code.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { DataCollectionLayout, EvidenceTime, StateValue, type DataColumn } from "../../data/DataCollectionLayout";
import { useDataSurface } from "../../data/dataSurface";
import {
  Button,
  ConnectorMark,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Input,
  NativeSelect,
  Status,
  Timestamp,
  Retry,
} from "../../ui";
import {
  installationReading,
  installationStateReading,
  listAvailableConnectors,
  nextStepLabel,
  readInstallation,
  readVerification,
  refusalSentence,
  registerInstallation,
  verifyInstallation,
  type AvailableConnector,
  type InstallationRead,
  type VerificationRead,
} from "./connectorInstallationApi";

/** What the dialog has read about the Connector currently being set up. */
type SetupRead =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; installation: InstallationRead; verification: VerificationRead | null };

/** The Connector this dialog is about, and how it got here. */
type Subject = { name: string; label: string } | null;

function ConnectorPicker({
  projectId,
  onPick,
}: {
  projectId: string;
  onPick: (subject: { name: string; label: string }) => void;
}) {
  const [rows, setRows] = useState<AvailableConnector[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [choice, setChoice] = useState("");

  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setRows(null);
    setMessage(null);
    void listAvailableConnectors(projectId, controller.signal)
      .then((items) => { if (alive) setRows(items); })
      .catch((reason: unknown) => {
        if (!alive) return;
        setMessage(refusalSentence(reason, null));
      });
    return () => { alive = false; controller.abort(); };
  }, [projectId, reloadToken]);

  if (message) {
    return <Status as="block" tone="error" title="The Connector list could not be read"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >{message}</Status>;
  }
  if (rows === null) {
    return <p role="status" className="text-body text-text-secondary">Reading the Connectors this deployment ships…</p>;
  }
  if (rows.length === 0) {
    // An empty list says why, and names the gesture that fills it.
    return (
      <Status as="block" tone="info" title="This deployment ships no Connector">
        Nothing can be set up until a Connector is deployed with the platform. Ask a
        platform administrator to deploy one, then reopen this.
      </Status>
    );
  }
  const picked = rows.find((row) => row.connector_name === choice);
  return (
    <div className="grid gap-4">
      <NativeSelect
        aria-label="Which Connector do you want to set up?"
        value={choice}
        onChange={(event) => setChoice(event.target.value)}
      >
        <option value="">Choose a Connector…</option>
        {rows.map((row) => (
          <option key={row.connector_name} value={row.connector_name}>
            {row.display_name || row.connector_name}
          </option>
        ))}
      </NativeSelect>
      <div>
        <Button
          disabled={!picked}
          onClick={() => { if (picked) onPick({ name: picked.connector_name, label: picked.display_name || picked.connector_name }); }}
        >
          Continue
        </Button>
      </div>
    </div>
  );
}

export default function ConnectorsCatalog({
  projectId,
  onOpenConnector,
}: {
  projectId?: string;
  onOpenConnector?: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [cursor, setCursor] = useState("");
  const { state, reload } = useDataSurface(projectId, "connectors", undefined, {
    q: query.trim() || undefined,
    state: stateFilter || undefined,
    cursor: cursor || undefined,
  });

  // --- the setup dialog -----------------------------------------------------
  const [open, setOpen] = useState(false);
  const [subject, setSubject] = useState<Subject>(null);
  const [read, setRead] = useState<SetupRead>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const openSetup = useCallback((next: Subject) => {
    setSubject(next);
    setRead({ status: "loading" });
    setRefusal(null);
    setDone(null);
    setAttempt((value) => value + 1);
    setOpen(true);
  }, []);

  const name = subject?.name ?? "";
  useEffect(() => {
    if (!open || !name) return;
    const controller = new AbortController();
    let alive = true;
    setRead({ status: "loading" });
    void (async () => {
      try {
        const installation = await readInstallation(name, controller.signal);
        // The restricted projection carries no `state`. Asking for the evidence
        // would then be asking a surface this person is not shown to exist.
        let verification: VerificationRead | null = null;
        if (installation.state) {
          verification = await readVerification(name, controller.signal).catch(() => null);
        }
        if (alive) setRead({ status: "ready", installation, verification });
      } catch (reason: unknown) {
        if (alive) setRead({ status: "error", message: refusalSentence(reason, null) });
      }
    })();
    return () => { alive = false; controller.abort(); };
  }, [open, name, attempt]);

  const reading = useMemo(
    () =>
      read.status === "ready"
        ? installationReading(read.installation, read.verification, Date.now())
        : null,
    [read],
  );

  const runGesture = useCallback(async () => {
    if (!subject || read.status !== "ready" || !reading?.gesture) return;
    const stored = read.installation.state ?? null;
    setBusy(true);
    setRefusal(null);
    setDone(null);
    try {
      if (reading.gesture.kind === "install") {
        await registerInstallation(subject.name);
        setDone(`${subject.label} is set up. Its next step is below.`);
      } else {
        await verifyInstallation(subject.name);
        setDone(`${subject.label} was checked just now. The result is below.`);
      }
      reload();
      setAttempt((value) => value + 1);
    } catch (reason: unknown) {
      setRefusal(refusalSentence(reason, stored));
    } finally {
      setBusy(false);
    }
  }, [subject, read, reading, reload]);

  const columns = useMemo<readonly DataColumn[]>(() => [
    {
      key: "connector",
      label: "Connector",
      render: (item) => (
        <div className="flex items-center gap-3">
          <ConnectorMark provider={item.connector_id} />
          <div className="min-w-0">
            <strong className="block truncate text-text">{item.connector_id ?? item.object_ref.id}</strong>
            <span className="font-mono text-caption text-text-secondary">{item.environment ?? "default"}</span>
          </div>
        </div>
      ),
    },
    {
      key: "installation",
      label: "Setup",
      // The reader's words, not the row's. `Domain pending` and `Verifying` are
      // states of a table in this deployment's database, and neither is a thing
      // a person recognises about a Connector they are trying to use.
      render: (item) => {
        const shown = installationStateReading(item.states.installation);
        return <Status tone={shown.tone}>{shown.label}</Status>;
      },
      sortValue: (item) => installationStateReading(item.states.installation).label,
    },
    { key: "activation", label: "Project activation", render: (item) => <StateValue value={item.states.activation} /> },
    { key: "contract", label: "Contract", render: (item) => <StateValue value={item.states.contract} /> },
    { key: "coverage", label: "Coverage", render: (item) => <StateValue value={item.states.coverage} /> },
    { key: "evidence", label: "Evidence as of", render: (item) => <EvidenceTime value={item.evidence_as_of} /> },
    {
      key: "next-step",
      label: "Next step",
      sortable: false,
      render: (item) => {
        const label = nextStepLabel(item.states.installation);
        const connector = String(item.connector_id ?? item.object_ref.id);
        if (!label) return <span className="text-text-secondary">No step here</span>;
        return (
          <Button
            variant="secondary"
            onClick={(event) => { event.stopPropagation(); openSetup({ name: connector, label: connector }); }}
          >
            {label}
          </Button>
        );
      },
    },
  ], [openSetup]);

  return (
    <>
      <DataCollectionLayout
        title="Connectors"
        description="Installed Connector contracts, immutable active versions and their exact Project coverage."
        emptyTitle="No Connector is in use in this Project"
        emptyDescription="A Connector shows up here once this Project uses it. Set one up on this deployment, then add a Datastream that reads from it."
        emptyAction={projectId ? <Button onClick={() => openSetup(null)}>Set up a Connector</Button> : undefined}
        state={state}
        reload={reload}
        columns={columns}
        onOpen={onOpenConnector ? (item) => onOpenConnector(item.object_ref.id) : undefined}
        actions={projectId ? <Button onClick={() => openSetup(null)}>Set up a Connector</Button> : undefined}
        filters={
          <div className="grid w-full gap-3 md:grid-cols-[minmax(14rem,2fr)_minmax(10rem,1fr)]">
            <Input aria-label="Search Connectors" placeholder="Search Connectors" value={query} onChange={(event) => { setQuery(event.target.value); setCursor(""); }} />
            <NativeSelect aria-label="Filter Connectors by state" value={stateFilter} onChange={(event) => { setStateFilter(event.target.value); setCursor(""); }}>
              <option value="">All states</option>
              {(state.status === "ready" ? state.envelope.filter_options?.states ?? [] : []).map((option) => <option key={option} value={option}>{option.replaceAll("_", " ")}</option>)}
            </NativeSelect>
          </div>
        }
        onNextPage={() => { if (state.status === "ready" && state.envelope.next_cursor) setCursor(state.envelope.next_cursor); }}
        onFirstPage={cursor ? () => setCursor("") : undefined}
      />

      <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) setSubject(null); }}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{subject ? `Set up ${subject.label}` : "Set up a Connector"}</DialogTitle>
            <DialogDescription>
              {subject
                ? "A Connector is set up once per deployment. Its check is what makes it available to every Project here."
                : "Which Connector do you want to set up on this deployment?"}
            </DialogDescription>
          </DialogHeader>

          {/* Question one. It disappears the moment it is answered. */}
          {!subject && projectId ? (
            <ConnectorPicker projectId={projectId} onPick={(picked) => openSetup(picked)} />
          ) : null}

          {subject && read.status === "loading" ? (
            <p role="status" className="text-body text-text-secondary">Reading how {subject.label} is set up…</p>
          ) : null}

          {subject && read.status === "error" ? (
            <Status
              as="block"
              tone="error"
              title="This Connector's setup could not be read"
              action={<Button variant="secondary" onClick={() => setAttempt((value) => value + 1)}>Try again</Button>}
            >
              {read.message}
            </Status>
          ) : null}

          {subject && read.status === "ready" && reading ? (
            <div className="grid gap-4">
              <Status as="block" tone={reading.tone} title={reading.label}>
                {reading.sentence}
              </Status>

              {/* The evidence, only where one exists. Never a claimed age. */}
              {read.verification?.verified && read.verification.last_run_at ? (
                <p className="text-body text-text-secondary">
                  Last checked <Timestamp value={read.verification.last_run_at} />
                  {typeof read.verification.ttl_seconds === "number" && read.verification.ttl_seconds > 0
                    ? `, and it is checked every ${Math.round(read.verification.ttl_seconds / 60)} minutes.`
                    : "."}
                </p>
              ) : null}

              {done ? <Status as="block" tone="success">{done}</Status> : null}
              {refusal ? <Status as="block" tone="error" title="That did not go through">{refusal}</Status> : null}

              {reading.gesture ? (
                <div>
                  <Button disabled={busy} onClick={() => { void runGesture(); }}>
                    {busy ? "Working…" : reading.gesture.label}
                  </Button>
                </div>
              ) : (
                // No gesture here, so the one that IS next is named rather than
                // left for the reader to guess.
                <p className="text-body text-text-secondary">{reading.elsewhere}</p>
              )}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
