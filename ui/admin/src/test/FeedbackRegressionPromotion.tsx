import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import { Button, Field, Input, NativeSelect, ObjectId, Panel, PanelBody, PanelHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Textarea, wireWord } from "../ui";
import { ExpectedAiPathTab, ExpectedResultTab } from "../shell/pages/GoldenQuestionWorkbench";
import { createFeedbackRegressionCase, feedbackRegressionCreatePayload, fetchFeedbackRegressionDraft, type FeedbackRegressionDraftAvailable, type FeedbackRegressionReceipt, type OwnerLink, type RegressionDomainOption } from "./feedbackReviewClient";
import { emptyDefinitionDraft, fetchGoldenQuestionOptions, refusalsOf, type DefinitionDraft, type GoldenQuestionOptions, type Refusal } from "./goldenQuestionClient";

type LoadPhase =
  | { status: "loading" }
  | { status: "unavailable"; reason: string }
  | { status: "ready"; draft: FeedbackRegressionDraftAvailable; options: GoldenQuestionOptions }
  | { status: "error"; message: string };

function mintRetryKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `feedback-regression-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function definitionFor(options: GoldenQuestionOptions): DefinitionDraft {
  return {
    ...emptyDefinitionDraft(options.provenance_link_kinds),
    contract_version: "golden-question.v2",
    assertions: [],
    v2_assertions: [],
  };
}

function domainKey(domain: RegressionDomainOption): string {
  return `${domain.domain_id}:${domain.version_number}`;
}

function ownerLabel(link: OwnerLink): string {
  return `${link.object_type} ${link.object_id}${link.version_id ? ` v${link.version_id}` : ""}`;
}

function refusalCode(error: ApiError): string | null {
  if (!error.body || typeof error.body !== "object") return null;
  const code = (error.body as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

export default function FeedbackRegressionPromotion({
  projectId,
  feedbackId,
  reviewVersionId,
  onOpenOwner,
}: {
  projectId: string;
  feedbackId: string;
  reviewVersionId: string | null;
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  const [phase, setPhase] = useState<LoadPhase>({ status: "loading" });
  const [title, setTitle] = useState("");
  const [owner, setOwner] = useState("");
  const [reason, setReason] = useState("");
  const [selectedDomain, setSelectedDomain] = useState("");
  const [definition, setDefinition] = useState<DefinitionDraft | null>(null);
  const [retryKey, setRetryKey] = useState(mintRetryKey);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<{ message: string; refusals: Refusal[] } | null>(null);
  const [receipt, setReceipt] = useState<FeedbackRegressionReceipt | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const generationRef = useRef(0);
  const saveAbortRef = useRef<AbortController | null>(null);

  const resetAuthored = useCallback((options: GoldenQuestionOptions, domains: RegressionDomainOption[]) => {
    setTitle("");
    setOwner("");
    setReason("");
    setDefinition(definitionFor(options));
    setSelectedDomain(domains.length === 1 ? domainKey(domains[0]) : "");
    setRetryKey(mintRetryKey());
    setReceipt(null);
  }, []);

  useEffect(() => {
    generationRef.current += 1;
    saveAbortRef.current?.abort();
    const generation = generationRef.current;
    const controller = new AbortController();
    setPhase({ status: "loading" });
    setSaving(false);
    setFailure(null);
    setReceipt(null);
    setDefinition(null);
    setTitle("");
    setOwner("");
    setReason("");
    setSelectedDomain("");
    setRetryKey(mintRetryKey());

    fetchFeedbackRegressionDraft(projectId, feedbackId, { signal: controller.signal })
      .then(async (draft) => {
        if (draft.state === "unavailable") {
          if (!controller.signal.aborted && generationRef.current === generation) {
            setPhase({ status: "unavailable", reason: draft.reason });
          }
          return;
        }
        if (draft.state !== "available" || !draft.create_contract || !draft.frozen) {
          throw new Error("The regression draft did not match feedback-regression-draft.v1.");
        }
        const options = await fetchGoldenQuestionOptions(projectId, { signal: controller.signal });
        if (controller.signal.aborted || generationRef.current !== generation) return;
        setDefinition(definitionFor(options));
        setSelectedDomain(draft.domain_options.length === 1 ? domainKey(draft.domain_options[0]) : "");
        setPhase({ status: "ready", draft, options });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || generationRef.current !== generation) return;
        setPhase({ status: "error", message: error instanceof Error ? error.message : String(error) });
      });

    return () => controller.abort();
  }, [projectId, feedbackId, reviewVersionId, reloadToken]);

  useEffect(() => () => {
    generationRef.current += 1;
    saveAbortRef.current?.abort();
  }, []);

  const create = useCallback(async () => {
    if (phase.status !== "ready" || !definition) return;
    const domain = phase.draft.domain_options.find((candidate) => domainKey(candidate) === selectedDomain);
    if (!domain) {
      setFailure({ message: "Choose one exact Business Domain version before creating the case.", refusals: [] });
      return;
    }
    let command;
    try {
      command = feedbackRegressionCreatePayload(phase.draft.create_contract, retryKey, {
        title,
        owner,
        reproduction_reason: reason,
        selected_domain: domain,
        definition,
      });
    } catch (error: unknown) {
      setFailure({ message: error instanceof Error ? error.message : String(error), refusals: [] });
      return;
    }

    saveAbortRef.current?.abort();
    const controller = new AbortController();
    saveAbortRef.current = controller;
    const generation = generationRef.current;
    const current = () => !controller.signal.aborted && generationRef.current === generation;
    setSaving(true);
    setFailure(null);
    try {
      const nextReceipt = await createFeedbackRegressionCase(projectId, feedbackId, command, {
        signal: controller.signal,
      });
      if (!current()) return;
      setReceipt(nextReceipt);
      setRetryKey(mintRetryKey());
    } catch (error: unknown) {
      if (!current()) return;
      if (
        error instanceof ApiError
        && error.status === 409
        && ["stale_review_head", "stale_review_version"].includes(refusalCode(error) ?? "")
      ) {
        try {
          const fresh = await fetchFeedbackRegressionDraft(projectId, feedbackId, { signal: controller.signal });
          if (!current()) return;
          if (fresh.state === "unavailable") {
            setPhase({ status: "unavailable", reason: fresh.reason });
            setFailure({ message: error.message, refusals: refusalsOf(error.body) });
            return;
          }
          const options = await fetchGoldenQuestionOptions(projectId, { signal: controller.signal });
          if (!current()) return;
          setPhase({ status: "ready", draft: fresh, options });
          resetAuthored(options, fresh.domain_options);
          setFailure({
            message: `${error.message}. The accepted review changed; authored expectations were cleared before retrying.`,
            refusals: refusalsOf(error.body),
          });
          return;
        } catch (refreshError: unknown) {
          if (!current()) return;
          setFailure({
            message: `${error.message}. The current promotion draft could not be refreshed: ${refreshError instanceof Error ? refreshError.message : String(refreshError)}`,
            refusals: refusalsOf(error.body),
          });
          return;
        }
      }
      setFailure({
        message: error instanceof Error ? error.message : String(error),
        refusals: error instanceof ApiError ? refusalsOf(error.body) : [],
      });
      // The same retry key and the same authored command deliberately survive
      // transport failure or a stable refusal. Only an acknowledgement rotates it.
    } finally {
      if (current()) setSaving(false);
      if (saveAbortRef.current === controller) saveAbortRef.current = null;
    }
  }, [phase, definition, selectedDomain, retryKey, title, owner, reason, projectId, feedbackId, resetAuthored]);

  if (phase.status === "loading") {
    return <p role="status" className="text-body text-text-secondary">Loading regression eligibility…</p>;
  }
  if (phase.status === "unavailable") {
    return (
      <Status as="block" tone="neutral" title="A regression case cannot be created">
        {phase.reason}. No authoring action is available for this frozen feedback state.
      </Status>
    );
  }
  if (phase.status === "error") {
    return (
      <Status
        as="block"
        tone="error"
        title="Regression eligibility could not be read"
        action={<Button variant="secondary" onClick={() => setReloadToken((value) => value + 1)}>Retry</Button>}
      >
        {phase.message}
      </Status>
    );
  }

  const { draft, options } = phase;
  return (
    <Stack data-testid="feedback-regression-promotion">
      <Panel flush>
        <PanelHeader
          title="Frozen promotion evidence"
          description="These pins come from the accepted negative feedback. They are evidence for reproduction, not editable expected truth."
        />
        <TableScroll label="Frozen promotion evidence">
          <Table>
            <TableHeader><TableRow><TableHead>Pin</TableHead><TableHead>Exact value</TableHead></TableRow></TableHeader>
            <TableBody>
              <TableRow><TableCell>Result</TableCell><TableCell className="text-technical break-all">{draft.frozen.result.id} / {draft.frozen.result.content_hash}</TableCell></TableRow>
              <TableRow><TableCell>Query Spec version</TableCell><TableCell><ObjectId value={draft.frozen.query_spec_version_id} title="Query Spec version" /></TableCell></TableRow>
              <TableRow><TableCell>Classification hash</TableCell><TableCell className="text-technical break-all">{draft.frozen.classification_hash}</TableCell></TableRow>
              <TableRow><TableCell>Semantic View</TableCell><TableCell className="text-technical break-all">{draft.frozen.semantic_view.id} / {draft.frozen.semantic_view.version_id}</TableCell></TableRow>
              <TableRow><TableCell>Capability</TableCell><TableCell className="text-technical break-all">{draft.frozen.capability.key} / {draft.frozen.capability.version_id}</TableCell></TableRow>
              <TableRow><TableCell>Result type</TableCell><TableCell className="text-technical">{wireWord(draft.frozen.result_type)}</TableCell></TableRow>
              <TableRow><TableCell>Observed AI Path</TableCell><TableCell className="text-technical break-all">{draft.frozen.ai_path}</TableCell></TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      {failure && (
        <Status as="block" tone="error" title="The regression case was not created" data-testid="feedback-regression-refusal">
          <p className="m-0">{failure.message}</p>
          {failure.refusals.length > 0 && (
            <ul className="mt-2 list-disc pl-5">
              {failure.refusals.map((refusal, index) => (
                <li key={`${refusal.code}-${index}`}><code className="text-technical">{refusal.subject ?? refusal.code}</code> — {refusal.message}</li>
              ))}
            </ul>
          )}
        </Status>
      )}

      {receipt && (
        <Status as="block" tone="success" title={receipt.status === "replayed" ? "Regression case acknowledgement replayed" : "Regression case created"}>
          <Stack>
            <span>Case <ObjectId value={receipt.regression_case_id} title="Regression case" /> pins Result <ObjectId value={receipt.result_id} title="Result" />.</span>
            {receipt.owner_links.map((link) => onOpenOwner ? (
              <Button key={`${link.section}-${link.object_type}-${link.object_id}`} variant="link" size="sm" onClick={() => onOpenOwner(link)}>
                {link.object_type === "golden-question" ? "Open Golden Question Definition" : `Open ${ownerLabel(link)}`}
              </Button>
            ) : <span key={`${link.section}-${link.object_type}-${link.object_id}`} className="text-technical">{ownerLabel(link)}</span>)}
          </Stack>
        </Status>
      )}

      <Panel flush>
        <PanelHeader
          title="Author the regression contract"
          description="Only these values become expected truth. None is initialized from the contested Result or its observed AI Path."
          actions={<Button data-testid="feedback-regression-create" onClick={() => void create()} disabled={saving}>{saving ? "Creating…" : "Create regression case"}</Button>}
        />
        <PanelBody className="grid gap-4 md:grid-cols-2">
          <Field label="Title" required>{(field) => <Input {...field} value={title} onChange={(event) => setTitle(event.target.value)} />}</Field>
          <Field label="Owner" required>{(field) => <Input {...field} value={owner} onChange={(event) => setOwner(event.target.value)} />}</Field>
          <Field label="Business Domain version" required>
            {(field) => (
              <NativeSelect {...field} value={selectedDomain} onChange={(event) => setSelectedDomain(event.target.value)}>
                <option value="">Choose an exact version</option>
                {draft.domain_options.map((domain) => <option key={domainKey(domain)} value={domainKey(domain)}>{domain.domain_id} v{domain.version_number}</option>)}
              </NativeSelect>
            )}
          </Field>
          <Field label="Reproduction reason" required>{(field) => <Textarea {...field} value={reason} onChange={(event) => setReason(event.target.value)} />}</Field>
        </PanelBody>
        <PanelBody className="grid gap-4 border-t border-divider-base">
          <Field label="Business question" required>{(field) => <Textarea {...field} value={definition?.question ?? ""} onChange={(event) => setDefinition((current) => current ? { ...current, question: event.target.value } : current)} />}</Field>
          <div className="grid gap-4 md:grid-cols-4">
            <Field label="Time boundary">{(field) => <NativeSelect {...field} value={definition?.time_boundary_kind ?? "none"} onChange={(event) => setDefinition((current) => current ? { ...current, time_boundary_kind: event.target.value as DefinitionDraft["time_boundary_kind"] } : current)}><option value="none">None declared</option><option value="as_of">As of</option><option value="range">Fixed range</option></NativeSelect>}</Field>
            {definition?.time_boundary_kind === "as_of" && <Field label="As of">{(field) => <Input {...field} type="date" value={definition.as_of} onChange={(event) => setDefinition({ ...definition, as_of: event.target.value })} />}</Field>}
            {definition?.time_boundary_kind === "range" && <><Field label="From">{(field) => <Input {...field} type="date" value={definition.from} onChange={(event) => setDefinition({ ...definition, from: event.target.value })} />}</Field><Field label="To">{(field) => <Input {...field} type="date" value={definition.to} onChange={(event) => setDefinition({ ...definition, to: event.target.value })} />}</Field></>}
            {definition?.time_boundary_kind !== "none" && <Field label="Timezone">{(field) => <Input {...field} value={definition?.timezone ?? ""} onChange={(event) => setDefinition((current) => current ? { ...current, timezone: event.target.value } : current)} />}</Field>}
          </div>
        </PanelBody>
      </Panel>

      {definition && <ExpectedResultTab draft={definition} options={options} update={(patch) => setDefinition((current) => current ? { ...current, ...patch } : current)} />}
      {definition && <ExpectedAiPathTab draft={definition} options={options} update={(patch) => setDefinition((current) => current ? { ...current, ...patch } : current)} />}
    </Stack>
  );
}
