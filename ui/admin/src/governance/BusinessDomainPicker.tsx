/**
 * BusinessDomainPicker — the Business Domains a Semantic Concept or View links to.
 *
 * `governance.md:72` makes a Semantic View "linkable to Business Domains", and
 * that link had no way in: measured 2026-08-03, `business_domain_refs` appeared
 * ZERO times in `ui/admin/src`, and the database carried 1 view version and 13
 * concept versions with not one link between them.
 *
 * IT OFFERS ONLY WHAT THE SERVER ACCEPTS, and that is the whole design. The
 * validator added in `server/core/semantic_model.py`
 * (`_validate_business_domain_refs`) refuses an id that resolves to nothing, one
 * belonging to another organization, an archived one, and a duplicate. This
 * reads `GET /api/context/business-taxonomy`, which returns the ACTIVE domains
 * of the authorized organization — the same set, from the same side of the
 * wire. A free-text id field would have let a person type something the server
 * then refuses, which is a worse screen than no screen.
 *
 * It does not invent a second store either: it selects existing Business
 * Domains, owned by Governance > Master Data (`README.md:97`). Creating one is
 * that surface's job, not this dialog's.
 *
 * UNAVAILABLE IS NOT EMPTY. If the taxonomy cannot be read, the picker says so
 * and disables itself rather than rendering an empty list that reads as "this
 * organization has no Business Domains" — `README.md` invariant 8.
 *
 * 2026-08-22, story 67.8 — IL DISAIT « INDISPONIBLE » A TOUT LE MONDE, TOUJOURS.
 * L'appel partait sans `project_id`, et `_run_scoped`
 * (`server/core/business_taxonomy_api.py:117`) refuse par un 422
 * `project_id is required` AVANT d'ouvrir quoi que ce soit. Le picker prenait
 * donc le refus, rendait son état indisponible et se désactivait — sur chaque
 * ouverture des deux dialogues, pour chaque organisation. L'honnêteté de l'état
 * vide masquait une porte qui n'était jamais ouverte : c'est précisément
 * pourquoi « indisponible » doit nommer sa cause, et pourquoi un écran qui la
 * nomme bien peut rester cassé longtemps.
 *
 * Les deux appelants portaient déjà `projectId` ; il ne descendait pas jusqu'ici.
 */
import { useEffect, useState } from "react";
import { apiGet } from "../lib/apiFetch";
import { Checkbox, Label, Spinner, Status } from "../ui";

interface TaxonomyDomain {
  id: string;
  name: string;
  status?: string;
}

interface BusinessDomainPickerProps {
  /**
   * The Project this dialog belongs to. REQUIRED: the taxonomy route is
   * project-scoped and refuses a call without it (422), which is how this
   * picker spent its life rendering "unavailable".
   */
  projectId: string;
  /** The selected ids, in selection order. The caller owns the state. */
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}

export default function BusinessDomainPicker({
  projectId,
  value,
  onChange,
  disabled = false,
}: BusinessDomainPickerProps) {
  const [domains, setDomains] = useState<TaxonomyDomain[] | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // `try/catch` around an awaited call rather than `.then().catch()`: the
    // chained form left the rejection unattached for one tick under the test
    // runner, which reported the failure as an unhandled error instead of the
    // unavailable state this component exists to render.
    const load = async () => {
      try {
        const payload = await apiGet<{ domains?: TaxonomyDomain[] }>(
          `/api/context/business-taxonomy?project_id=${encodeURIComponent(projectId)}`,
        );
        if (cancelled) return;
        // Active only, and that mirrors the server's refusal of an archived
        // domain: a name offered here must be one the change set can carry.
        setDomains(
          (payload.domains ?? []).filter((domain) => (domain.status ?? "active") === "active"),
        );
      } catch (err: unknown) {
        if (cancelled) return;
        setUnavailable(err instanceof Error ? err.message : "Business Domains could not be read.");
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // `projectId` EST une dépendance : sans elle, rouvrir le dialogue sur un
    // autre Projet listerait les domaines du premier.
  }, [projectId]);

  const toggle = (id: string) => {
    onChange(value.includes(id) ? value.filter((entry) => entry !== id) : [...value, id]);
  };

  if (unavailable) {
    return (
      <Status as="block" tone="warning" data-testid="business-domain-picker-unavailable">
        Business Domains could not be read ({unavailable}). No domain can be linked on this
        form; the object can still be created without one.
      </Status>
    );
  }

  if (domains === null) {
    return <Spinner label="Loading Business Domains…" showLabel />;
  }

  if (domains.length === 0) {
    return (
      <p className="m-0 text-caption text-text-secondary" data-testid="business-domain-picker-empty">
        This organization has no active Business Domain yet. They are created in
        Governance › Master Data.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="business-domain-picker">
      {domains.map((domain) => {
        const inputId = `bd-${domain.id}`;
        return (
          <div key={domain.id} className="flex items-center gap-2">
            <Checkbox
              id={inputId}
              checked={value.includes(domain.id)}
              disabled={disabled}
              onCheckedChange={() => toggle(domain.id)}
              data-testid={`business-domain-option-${domain.id}`}
            />
            <Label htmlFor={inputId} className="cursor-pointer text-ui font-normal">
              {domain.name}
            </Label>
          </div>
        );
      })}
    </div>
  );
}
