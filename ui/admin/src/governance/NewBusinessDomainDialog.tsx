/**
 * NewBusinessDomainDialog — the Governance door onto Master Data creation.
 *
 * WHY IT IS WIRED HERE. Ratified 2026-08-17 (`docs/product-architecture/context-hub.md`,
 * "Amendment, 2026-08-17 — where a Business Domain is created"): Business
 * Domains are created from BOTH surfaces — Context Hub primary, Governance
 * second — through ONE creation gesture. This dialog is a second DOOR onto that
 * gesture, never a second write path.
 *
 * WHICH WRITER, AND WHY IT CHANGED. Until 2026-08-25 the one endpoint was
 * `POST /api/context/business-domains`. The cutover ratified that day made the
 * four legacy identity writers refuse, and named the gesture that replaces them:
 * *converge this organization, then create, rename or archive it in Master
 * Data*. So this dialog now calls the authority
 * (`governance/masterDataApi.createBusinessIdentity`) — the same command the
 * Context Hub calls, still one writer behind two doors.
 *
 * THREE STATES, AND THE CONTROL AGREES WITH ALL THREE. `GovernanceCollection`
 * reads the convergence plan for the section: on an organization measured as
 * UNCONVERGED it does not draw the button at all and the panel offers the
 * convergence instead; on a converged one the button opens this dialog and the
 * command works; when the plan could NOT be read the button stays (a control is
 * not withdrawn on absence of evidence) and the server answers the truth —
 * which is why the convergence refusal below is printed verbatim rather than
 * folded into "the short code is taken".
 *
 * THE IDEMPOTENCY KEY IS MINTED ONCE PER SUBMISSION and reused for every retry
 * of it: the route answers 428 without one, and a fresh key per attempt would
 * make a client timeout mint a second identity.
 *
 * WHAT THE CONTRACT DEMANDS, AND SO THE FORM ASKS. `reason` is recorded with the
 * command, so the form asks for it rather than sending a write the server will
 * refuse. A Classification needs a parent Business Domain and a classification
 * type; those two questions appear only once "Classification" is chosen, and the
 * parent list is read rather than typed so the form can only offer ids the
 * server accepts.
 */
import { useEffect, useState } from "react";
import { ApiError } from "../lib/apiFetch";
import { listBusinessDomainOptions } from "../connaissances/businessTaxonomyApi";
import { createBusinessIdentity, mintCommandKey } from "./masterDataApi";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  NativeSelect,
  Spinner,
  Status,
  Textarea,
  notify,
} from "../ui";

interface NewBusinessDomainDialogProps {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onCreated: () => void;
}

type NodeType = "domain" | "classification";

interface DomainOption {
  id: string;
  name: string;
}

/**
 * Every refusal this endpoint can produce, said as the gesture that repairs it.
 *
 * The server's own wording is a developer's ("reason is required",
 * "Context Hub is temporarily unavailable"); none of it names what the person
 * in front of the form should do next. The 404 is existence-hiding by design
 * (`business_taxonomy_api._not_found`), so the sentence must not claim the
 * project is missing NOR that the right is missing — it names the one check
 * that resolves either.
 */
function repairSentence(err: unknown, nodeType: NodeType): string {
  const label = nodeType === "domain" ? "Business Domain" : "Classification";
  if (!(err instanceof ApiError)) {
    return err instanceof Error && err.message
      ? err.message
      : `The ${label} was not created. Submit the form again.`;
  }
  if (err.status === 0) {
    return "The server could not be reached, so nothing was created. Check your connection and submit again.";
  }
  if (err.status === 401) {
    return "Your session has expired. Sign in again, then submit this form once more.";
  }
  if (err.status === 403 || err.status === 404) {
    return "Master Data cannot be written from this account on this project. Ask an organization owner for the Manage right, then reopen this dialog.";
  }
  if (err.status === 409) {
    // THREE DIFFERENT 409s, AND ONLY ONE OF THEM IS A DUPLICATE SHORT CODE.
    // The authority refuses an unconverged organization
    // (`master_data_organization_not_converged`), a short code already in use
    // (`master_data_short_code_taken`) and an idempotency key bound to another
    // command — three repairs, done by different people on different screens.
    // Printing "change the Short code" over the convergence refusal would send
    // a person to edit a field that is not the problem, so every refusal the
    // server wrote for a reader is carried through as it wrote it.
    if (err.code === "master_data_short_code_taken" || err.code === "conflict") {
      return (
        err.message
        || `A ${label} already uses this short code. Change the Short code and submit again.`
      );
    }
    return err.message || `The ${label} was not created.`;
  }
  if (err.status === 422) {
    const detail = (err.message || "").toLowerCase();
    if (detail.includes("reason")) {
      return "Add a reason — it is recorded with the change so the registry stays auditable.";
    }
    if (detail.includes("classification_type")) {
      return "Enter a classification type, for example product_line.";
    }
    if (detail.includes("domain_id")) {
      return "Choose the parent Business Domain this classification belongs to.";
    }
    if (detail.includes("slug")) {
      return "The short code must contain letters or digits, and stay under 96 characters.";
    }
    if (detail.includes("name")) {
      return "Enter a name for this item.";
    }
    return `The ${label} was not created as entered. Review the fields and submit again.`;
  }
  return `Master Data is temporarily unavailable, so nothing was created. Wait a moment and submit again.`;
}

export default function NewBusinessDomainDialog({
  open,
  projectId,
  onClose,
  onCreated,
}: NewBusinessDomainDialogProps) {
  const [nodeType, setNodeType] = useState<NodeType>("domain");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [reason, setReason] = useState("");
  const [classificationType, setClassificationType] = useState("product_line");
  const [domainId, setDomainId] = useState("");

  const [domains, setDomains] = useState<DomainOption[] | null>(null);
  const [domainsError, setDomainsError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Minted on the first attempt, kept across retries, dropped after a success. */
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);

  // The parent list is read only once the question is asked. Loading it on open
  // would spend a call on the common case, which is creating a root domain.
  useEffect(() => {
    if (!open || nodeType !== "classification" || domains !== null || domainsError) return;
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await listBusinessDomainOptions(projectId);
        if (cancelled) return;
        setDomains(payload.domains ?? []);
      } catch (err: unknown) {
        if (cancelled) return;
        // Unreadable is not empty: an empty select would read as "this
        // organization has no domain", which is a different sentence.
        setDomainsError(
          err instanceof Error && err.message ? err.message : "the taxonomy could not be read",
        );
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [open, nodeType, projectId, domains, domainsError]);

  const resetForm = () => {
    setNodeType("domain");
    setName("");
    setSlug("");
    setDescription("");
    setReason("");
    setClassificationType("product_line");
    setDomainId("");
    setError(null);
    setIdempotencyKey(null);
  };

  const handleClose = () => {
    resetForm();
    onClose();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError("Enter a name for this item.");
      return;
    }
    if (!reason.trim()) {
      setError("Add a reason — it is recorded with the change so the registry stays auditable.");
      return;
    }
    if (nodeType === "classification" && !domainId) {
      setError("Choose the parent Business Domain this classification belongs to.");
      return;
    }

    setSubmitting(true);
    setError(null);

    // An empty short code is left out entirely: the server derives the slug from
    // the name, and sending "" would be refused for a field nobody filled in.
    const cleanSlug = slug.trim() ? slug.trim() : undefined;
    // The SAME key for every retry of this submission. A fresh one per attempt
    // would let a client timeout mint a second identity.
    const key = idempotencyKey ?? mintCommandKey("md-create");
    setIdempotencyKey(key);

    try {
      await createBusinessIdentity(
        projectId,
        nodeType === "domain"
          ? {
              kind: "business_domain",
              name: name.trim(),
              description: description.trim(),
              slug: cleanSlug,
              reason: reason.trim(),
            }
          : {
              kind: "business_classification",
              name: name.trim(),
              description: description.trim(),
              slug: cleanSlug,
              reason: reason.trim(),
              classification_type: classificationType.trim(),
              domain_node_id: domainId,
            },
        key,
      );

      notify(
        nodeType === "domain"
          ? `Business Domain created: ${name.trim()} is now in the registry.`
          : `Classification created: ${name.trim()} is now in the registry.`,
      );

      handleClose();
      onCreated();
    } catch (err: unknown) {
      setError(repairSentence(err, nodeType));
    } finally {
      setSubmitting(false);
    }
  };

  const noDomainYet = nodeType === "classification" && domains !== null && domains.length === 0;

  return (
    <Dialog open={open} onOpenChange={(val) => !val && handleClose()}>
      <DialogContent className="sm:max-w-[500px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create Master Data Item</DialogTitle>
            <DialogDescription>
              Add a root business domain or classification to the Master Data registry.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            {error && (
              <Status as="block" tone="error" title="Nothing was created">
                {error}
              </Status>
            )}

            <div className="space-y-1.5">
              <Label htmlFor="node-type">Item Type</Label>
              <NativeSelect
                id="node-type"
                value={nodeType}
                onChange={(e) => {
                  setNodeType(e.target.value as NodeType);
                  setError(null);
                }}
              >
                <option value="domain">Root Business Domain</option>
                <option value="classification">Classification / Taxonomy</option>
              </NativeSelect>
            </div>

            {/* Asked only for a Classification: a root domain has no parent, and
                the server refuses a classification without one. */}
            {nodeType === "classification" && (
              <>
                <div className="space-y-1.5">
                  <Label htmlFor="parent-domain">Parent Business Domain</Label>
                  {domainsError ? (
                    <Status as="block" tone="warning" data-testid="parent-domain-unavailable">
                      Business Domains could not be read ({domainsError}), so no parent can be
                      chosen. Close this dialog and reopen it to try again.
                    </Status>
                  ) : domains === null ? (
                    <Spinner label="Loading Business Domains…" showLabel />
                  ) : noDomainYet ? (
                    <p
                      className="m-0 text-caption text-text-secondary"
                      data-testid="parent-domain-empty"
                    >
                      This organization has no Business Domain yet. Switch Item Type to Root
                      Business Domain and create one first — a classification hangs off it.
                    </p>
                  ) : (
                    <NativeSelect
                      id="parent-domain"
                      value={domainId}
                      onChange={(e) => setDomainId(e.target.value)}
                    >
                      <option value="">Choose a Business Domain…</option>
                      {domains.map((domain) => (
                        <option key={domain.id} value={domain.id}>
                          {domain.name}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="classification-type">Classification Type</Label>
                  <Input
                    id="classification-type"
                    placeholder="product_line"
                    value={classificationType}
                    onChange={(e) => setClassificationType(e.target.value)}
                  />
                </div>
              </>
            )}

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="domain-name">Item Name</Label>
                <Input
                  id="domain-name"
                  placeholder="e.g. Finance, Sales, Marketing"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  aria-required="true"
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="domain-slug">Short Code</Label>
                <Input
                  id="domain-slug"
                  placeholder="Derived from the name if left empty"
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="domain-desc">Description</Label>
              <Textarea
                id="domain-desc"
                placeholder="Scope description for this domain or classification..."
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>

            {/* Not optional, and not a formality: the registry records it with
                the change, and the endpoint refuses the write without it.
                `aria-required` rather than `required`: the native constraint
                answers with the browser's own sentence ("Please fill out this
                field"), which names no gesture and is not the product's one
                alert language. The requirement is still announced, and the
                refusal is the Status block above. */}
            <div className="space-y-1.5">
              <Label htmlFor="domain-reason">Reason for this change</Label>
              <Input
                id="domain-reason"
                placeholder="Why this item is being registered"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                aria-required="true"
              />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={handleClose} disabled={submitting}>
              Cancel
            </Button>
            <Button type="submit" disabled={submitting || noDomainYet}>
              {submitting ? "Creating…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
