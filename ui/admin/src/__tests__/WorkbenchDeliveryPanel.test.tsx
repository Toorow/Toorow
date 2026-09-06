/**
 * The delivery address, on screen -- AI-113.
 *
 * A person could create an inbound Datastream and had no way to obtain the
 * address it exists for: the issuing surface was written and mounted nowhere.
 * These tests pin what the ported panel must do, and the two things it must
 * refuse to do.
 *
 * `fetch` is stubbed rather than `apiFetch`, matching the workbench tests: the
 * seam guard (`apiSeamGuard.test.ts`) is what proves the bearer is attached, and
 * stubbing one level lower would hide it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchDeliveryPanel from "../datastreams/workbench/WorkbenchDeliveryPanel";
import type { MappingSourceColumn } from "../datastreams/workbench/inboundDeliveryApi";
import contract from "../datastreams/workbench/__contracts__/inbound-server-shapes.json";

/**
 * THE SERVER CONTRACT, READ AND NOT RETYPED.
 *
 * `inbound-server-shapes.json` is written by
 * `server/tests/core/test_inbound_console_contract.py` from the REAL server
 * functions. Both sides read this file, so a console fixture can no longer
 * describe a payload the server does not send -- which is exactly how
 * `source_columns: ["date", "campaign", "clicks"]` stayed green while the
 * screen rendered `[object Object]`.
 */
type ContractShape = {
  mapping_repair_context: {
    source_columns: { __item__: Record<string, unknown> };
  };
  health_metrics: Record<string, unknown>;
};

/** One complete source column, in the shape the server serialises. */
function sourceColumnFixture(
  overrides: Partial<MappingSourceColumn> = {},
): MappingSourceColumn {
  return {
    name: "date",
    index: 0,
    detected_type: "string",
    null_count: 0,
    source_label: null,
    masked_samples: ["####-##-##"],
    ...overrides,
  };
}

const CREDENTIAL = {
  credential_id: "dic_1",
  datastream_id: "ds_1",
  channel: "email",
  safe_suffix: "a1b2c3",
  state: "ACTIVE",
  version: 1,
  expires_at: null,
  overlap_until: null,
  issued_by: "operator@example.com",
  created_at: "2026-08-01T09:00:00Z",
};

const REFUSED_ATTACHMENT = {
  raw_import_id: "inbraw_1",
  ordinal: 0,
  filename: "bomb.zip",
  media_type_declared: "text/csv",
  media_type_detected: "application/zip",
  size_bytes: 42,
  content_hash: "a".repeat(64),
  state: "REJECTED",
  error_code: "archive_compression_ratio_exceeded",
  import_ledger_id: null,
  created_at: "2026-08-01T10:00:00Z",
  scan_verdict: {
    accepted: false,
    reason: "archive_compression_ratio_exceeded",
    declared_type: "text/csv",
    detected_type: "application/zip",
    size_bytes: 42,
    malware: "not_run",
    malware_engine: "clamav:1.4.3/db-2026-08-01",
    malware_detail: "not-scanned-after-archive-rejection",
    policy: {
      version: "inbound-scan-v2",
      max_bytes: 10_000,
      max_uncompressed_bytes: 100_000,
      max_compression_ratio: 50,
      max_archive_entries: 20,
      max_rows: 1_000,
      max_columns: 200,
      max_scan_seconds: 30,
      max_memory_bytes: 2_000_000,
    },
    evidence: {
      size_bytes: 42,
      encoding: "utf-8",
      row_estimate: 900,
      row_limit: 1_000,
      column_estimate: 180,
      column_limit: 200,
      archive_entries: 1,
      archive_entry_limit: 20,
      archive_uncompressed_bytes: 90_000,
      archive_uncompressed_limit: 100_000,
      archive_central_directory_bytes: 2_048,
      archive_central_directory_limit: 4_096,
      archive_worst_ratio: "infinite",
      archive_ratio_limit: 50,
      spreadsheet_rows: 900,
      spreadsheet_columns: 180,
    },
  },
  receipt: {
    receipt_id: "inbrx_1",
    channel: "email",
    state: "REJECTED",
    provider_event_id: "evt-1",
    created_at: "2026-08-01T10:00:00Z",
  },
};

function stubFetch(routes: Record<string, unknown>, status = 200) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    const key = Object.keys(routes).find((fragment) => url.includes(fragment));
    return {
      ok: status < 400,
      status,
      headers: { get: () => "application/json" },
      json: async () => (key ? routes[key] : {}),
      text: async () => JSON.stringify(key ? routes[key] : {}),
    } as unknown as Response;
  });
}

function renderPanel(
  channels = ["inbound_email"],
  onRepairMapping?: (rawImportId: string) => void,
) {
  return render(
    <WorkbenchDeliveryPanel
      datastreamId="ds_1"
      connectorName="managed_feed"
      channels={channels}
      onRepairMapping={onRepairMapping}
    />,
  );
}

describe("WorkbenchDeliveryPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("offers to issue an address when none exists", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [] },
        "/inbox": { items: [] },
      }),
    );
    renderPanel();

    expect(await screen.findByText(/No credential issued yet/i)).toBeTruthy();
    // The blocker is stated as a next step, not as an error: nothing is wrong,
    // the Datastream simply cannot receive anything yet.
    expect(
      screen.getByRole("button", { name: /Issue email address/i }),
    ).toBeTruthy();
  });

  it("shows the address exactly once, and warns that it is a credential", async () => {
    const issued = { ...CREDENTIAL, full_secret: "ds_TOKEN123@example.com" };
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [] },
        "/inbox": { items: [] },
      }),
    );
    renderPanel();
    await screen.findByText(/No credential issued yet/i);

    // The issue call answers with the secret; every later GET does not.
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": issued,
        "/inbox": { items: [] },
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Issue email address/i }),
    );

    expect(await screen.findByText("ds_TOKEN123@example.com")).toBeTruthy();
    expect(screen.getByText(/Email address shown once/i)).toBeTruthy();
    expect(screen.getByText(/will not be shown again/i)).toBeTruthy();
    expect(screen.getByText(/Treat it as a credential/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Copy email address/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /I have stored it/i }),
    ).toBeTruthy();
    const blockedIssue = screen.getByRole("button", {
      name: /Issue email address/i,
    });
    expect((blockedIssue as HTMLButtonElement).disabled).toBe(true);
  });

  it("labels a webhook secret as a secret, not an address", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [] },
        "/inbox": { items: [] },
      }),
    );
    renderPanel(["webhook"]);
    await screen.findByRole("button", { name: /Issue webhook credential/i });

    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": {
          ...CREDENTIAL,
          channel: "webhook",
          full_secret: "whsec_TOKEN123",
        },
        "/inbox": { items: [] },
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Issue webhook credential/i }),
    );

    expect(await screen.findByText("whsec_TOKEN123")).toBeTruthy();
    expect(screen.getByText(/Webhook secret shown once/i)).toBeTruthy();
    expect(
      screen.getByText(/This webhook secret will not be shown again/i),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Copy webhook secret/i }),
    ).toBeTruthy();
  });

  it("uses a real ellipsis while issuance is pending", async () => {
    let finishIssue: ((response: Response) => void) | undefined;
    const pendingIssue = new Promise<Response>((resolve) => {
      finishIssue = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (init?.method === "POST" && url.includes("/credentials")) {
          return pendingIssue;
        }
        const payload = url.includes("/inbox")
          ? { items: [] }
          : { credentials: [] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => payload,
          text: async () => JSON.stringify(payload),
        } as unknown as Response;
      }),
    );
    renderPanel();
    fireEvent.click(
      await screen.findByRole("button", { name: /Issue email address/i }),
    );
    expect(
      await screen.findByRole("button", { name: "Issuing…" }),
    ).toBeTruthy();

    finishIssue?.({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({
        ...CREDENTIAL,
        full_secret: "ds_TOKEN123@example.com",
      }),
      text: async () => "",
    } as unknown as Response);
    expect(await screen.findByText("ds_TOKEN123@example.com")).toBeTruthy();
  });

  it("uses a real ellipsis while rotation is pending", async () => {
    let finishRotation: ((response: Response) => void) | undefined;
    const pendingRotation = new Promise<Response>((resolve) => {
      finishRotation = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (init?.method === "POST" && url.includes("/rotate")) {
          return pendingRotation;
        }
        const payload = url.includes("/inbox")
          ? { items: [] }
          : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => payload,
          text: async () => JSON.stringify(payload),
        } as unknown as Response;
      }),
    );
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(
      await screen.findByRole("button", { name: "Rotating…" }),
    ).toBeTruthy();

    finishRotation?.({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({
        ...CREDENTIAL,
        version: 2,
        full_secret: "ds_ROTATED@example.com",
      }),
      text: async () => "",
    } as unknown as Response);
    expect(await screen.findByText("ds_ROTATED@example.com")).toBeTruthy();
  });

  it("reuses an idempotency key after an uncertain failure and reloads a replay", async () => {
    const keys: string[] = [];
    let postAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (init?.method === "POST" && url.includes("/credentials")) {
          postAttempts += 1;
          const headers = init.headers as Record<string, string>;
          keys.push(headers["Idempotency-Key"]);
          if (postAttempts === 1) throw new Error("network outcome unknown");
          return {
            ok: true,
            status: 200,
            headers: { get: () => "application/json" },
            json: async () => ({ ...CREDENTIAL, secret_available: false }),
            text: async () => "",
          } as unknown as Response;
        }
        const payload = url.includes("/inbox")
          ? { items: [] }
          : { credentials: postAttempts > 1 ? [CREDENTIAL] : [] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => payload,
          text: async () => JSON.stringify(payload),
        } as unknown as Response;
      }),
    );

    renderPanel();
    const issue = await screen.findByRole("button", {
      name: /Issue email address/i,
    });
    fireEvent.click(issue);
    expect(await screen.findByText(/network outcome unknown/i)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /Issue email address/i }),
    );
    expect(
      await screen.findByText(/committed state has been reloaded/i),
    ).toBeTruthy();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("ignores a stale load from the prior Datastream", async () => {
    let releaseOld: ((response: Response) => void) | undefined;
    const oldCredentials = new Promise<Response>((resolve) => {
      releaseOld = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("ds_old") && url.includes("/credentials"))
          return oldCredentials;
        const payload = url.includes("/inbox")
          ? { items: [] }
          : { credentials: [] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => payload,
          text: async () => JSON.stringify(payload),
        } as unknown as Response;
      }),
    );

    const view = render(
      <WorkbenchDeliveryPanel
        datastreamId="ds_old"
        connectorName="managed_feed"
        channels={["inbound_email"]}
      />,
    );
    view.rerender(
      <WorkbenchDeliveryPanel
        datastreamId="ds_new"
        connectorName="managed_feed"
        channels={["inbound_email"]}
      />,
    );
    expect(await screen.findByText(/No credential issued yet/i)).toBeTruthy();
    releaseOld?.({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ credentials: [CREDENTIAL] }),
      text: async () => "",
    } as unknown as Response);
    await waitFor(() => {
      expect(screen.queryByText("a1b2c3")).toBeNull();
    });
  });

  it("asks before revoking, because revoking cannot be undone", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [CREDENTIAL] },
        "/inbox": { items: [] },
      }),
    );
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /^Revoke$/i }));

    // A confirmation step exists, and it states the consequence rather than
    // asking "are you sure" -- the Epic 46 review found this class missing.
    expect(await screen.findByText(/Revoke this address\?/i)).toBeTruthy();
    expect(screen.getByText(/cannot be restored/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Revoke permanently/i }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /Keep it/i })).toBeTruthy();
  });

  it("shows a refused delivery and WHY, without downloading it", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [CREDENTIAL] },
        "/inbox": { items: [REFUSED_ATTACHMENT] },
      }),
    );
    renderPanel();

    expect(await screen.findByText("bomb.zip")).toBeTruthy();
    expect(
      screen.getAllByText(/archive_compression_ratio_exceeded/),
    ).toHaveLength(2);
    // The claim/evidence mismatch is what makes the refusal understandable.
    expect(
      screen.getByText(/declared text\/csv, detected application\/zip/),
    ).toBeTruthy();
  });

  it("shows the complete redacted scan evidence and its governing limits", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [CREDENTIAL] },
        "/inbox": { items: [REFUSED_ATTACHMENT] },
      }),
    );
    renderPanel();

    expect(await screen.findByText("Redacted scan evidence")).toBeTruthy();
    expect(screen.getByText("inbound-scan-v2")).toBeTruthy();
    expect(screen.getByText("not_run")).toBeTruthy();
    expect(screen.getByText("clamav:1.4.3/db-2026-08-01")).toBeTruthy();
    expect(
      screen.getByText("not-scanned-after-archive-rejection"),
    ).toBeTruthy();
    expect(screen.getByText("42 bytes / limit 10,000 bytes")).toBeTruthy();
    expect(screen.getByText("900 / limit 1,000")).toBeTruthy();
    expect(screen.getByText("180 / limit 200")).toBeTruthy();
    expect(screen.getByText("1 / limit 20")).toBeTruthy();
    expect(screen.getByText("90,000 bytes / limit 100,000 bytes")).toBeTruthy();
    expect(screen.getByText("2,048 bytes / limit 4,096 bytes")).toBeTruthy();
    expect(screen.getByText("infinite / limit 50:1")).toBeTruthy();
    expect(screen.getByText("30 seconds")).toBeTruthy();
    expect(screen.getByText("2,000,000 bytes")).toBeTruthy();
    expect(screen.getByText("utf-8")).toBeTruthy();
  });

  it("renders an older partial verdict honestly instead of inventing evidence", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [CREDENTIAL] },
        "/inbox": {
          items: [{ ...REFUSED_ATTACHMENT, scan_verdict: { accepted: false } }],
        },
      }),
    );
    renderPanel();

    expect(await screen.findByText("Redacted scan evidence")).toBeTruthy();
    // SCOPED, AND EXACTLY ONE. `getAllByText("Rejected").length > 0` was what
    // this assertion became when the row badge started reading
    // `stateLabel(item.state)` and collided with the scan verdict. Two facts
    // under one word is the defect; loosening the count hid it. The verdict now
    // says what it decided about, and each half is asserted inside its own
    // block: the scan evidence here, the delivery state on the row.
    const evidence = within(screen.getByTestId("redacted-scan-evidence"));
    expect(evidence.getByText("Scan rejected")).toBeTruthy();
    expect(evidence.queryByText("Rejected")).toBeNull();
    expect(screen.getByText("Rejected")).toBeTruthy();
    expect(screen.getAllByText("Not recorded").length).toBeGreaterThanOrEqual(
      4,
    );
    expect(screen.getByText("None recorded")).toBeTruthy();
  });

  it("distinguishes an unreadable address state from having no address", async () => {
    // The failure mode that matters: a 500 must not render as "issue one".
    vi.stubGlobal(
      "fetch",
      stubFetch({ "/credentials": {}, "/inbox": {} }, 500),
    );
    renderPanel();

    expect(await screen.findByText(/Address state unreadable/i)).toBeTruthy();
    await waitFor(() => {
      expect(screen.queryByText(/No credential issued yet/i)).toBeNull();
    });
  });

  it("offers independent actions for configured email and webhook channels", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [] },
        "/inbox": { items: [] },
      }),
    );
    renderPanel(["inbound_email", "webhook"]);

    expect(
      await screen.findByRole("button", { name: /Issue email address/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Issue webhook credential/i }),
    ).toBeTruthy();
  });

  it("never offers Rotate for an overlap predecessor", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [{ ...CREDENTIAL, state: "ROTATING" }] },
        "/inbox": { items: [] },
      }),
    );
    renderPanel();

    // THE SENTENCE, NOT THE WIRE TOKEN. The panel printed `credential.state`
    // raw — a database value on screen — until 76-2 deleted its private tone
    // map; it reads the shared vocabulary now, which spells the word.
    expect(await screen.findByText("Rotating")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Rotate$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^Revoke$/i })).toBeTruthy();
  });

  it("says a retained delivery is waiting for review, not that it failed", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [CREDENTIAL] },
        "/inbox": {
          items: [
            {
              ...REFUSED_ATTACHMENT,
              state: "ACCEPTED",
              error_code: null,
              filename: "first.csv",
              scan_verdict: {
                ...REFUSED_ATTACHMENT.scan_verdict,
                accepted: true,
                reason: null,
                malware: "clean",
                malware_detail: "signature-db-current",
              },
            },
          ],
        },
      }),
    );
    renderPanel();

    expect(await screen.findByText("first.csv")).toBeTruthy();
    expect(screen.getByText(/Retained for setup review/i)).toBeTruthy();
    expect(screen.queryByText(/Refused/i)).toBeNull();
    expect(screen.getByText("Redacted scan evidence")).toBeTruthy();
    // Same scoping as the rejected half: the scan's own word inside the scan
    // block, the declared delivery state on the row, one occurrence each.
    const accepted = within(screen.getByTestId("redacted-scan-evidence"));
    expect(accepted.getByText("Scan accepted")).toBeTruthy();
    expect(accepted.queryByText("Accepted")).toBeNull();
    expect(screen.getByText("Accepted")).toBeTruthy();
    expect(screen.getByText("clean")).toBeTruthy();
    expect(screen.getByText("signature-db-current")).toBeTruthy();
  });

  it("shows dead-letter attempts and authorized recovery without raw bytes", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch({
        "/credentials": { credentials: [CREDENTIAL] },
        "/inbox": {
          items: [
            {
              ...REFUSED_ATTACHMENT,
              state: "SCANNING",
              scan_verdict: null,
              scan_job: {
                state: "DEAD_LETTER",
                attempt_count: 5,
                max_attempts: 5,
                error_code: "scan_time_budget_exceeded",
                recovery_count: 0,
                updated_at: "2026-08-01T12:00:00Z",
                recovery: {
                  version: "inbound-scan-recovery-v1",
                  command: "retry_inbound_scan_job",
                  job_id: "inbscan_" + "a".repeat(24),
                  requires_authorization: true,
                },
              },
            },
          ],
        },
      }),
    );
    renderPanel();

    const evidence = await screen.findByTestId("scan-job-evidence");
    expect(evidence.textContent).toContain("DEAD_LETTER");
    expect(evidence.textContent).toContain("5/5 attempts");
    expect(evidence.textContent).toContain("retry_inbound_scan_job");
    expect(evidence.textContent).not.toContain("raw bytes");
    const retry = screen.getByRole("button", { name: "Retry scan" });
    fireEvent.click(retry);
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining(
          "/scan-jobs/inbscan_aaaaaaaaaaaaaaaaaaaaaaaa/recover",
        ),
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({
            "Idempotency-Key": expect.any(String),
          }),
        }),
      ),
    );
  });
});

/**
 * La reprise d'une livraison retenue — le chemin que le serveur offrait et que
 * la console n'atteignait pas.
 *
 * `inbound_reprocess_api.py` sert `GET`/`POST .../raw-imports/{id}/reprocess`
 * depuis la story 38.18. Mesuré le 2026-08-08 : `grep -rn "reprocess"
 * ui/admin/src` ne rendait qu'un COMMENTAIRE — aucun appelant. Sur un flux réel,
 * trois livraisons étaient arrêtées en `PROCESSING`, leurs octets conservés, et
 * un opérateur n'avait rien à cliquer.
 *
 * Ces tests tiennent l'EFFET : la proposition est lue avant d'agir, un refus
 * porte sa raison, et l'exécution ne part pas sans la raison que le serveur
 * exige.
 */
describe("WorkbenchDeliveryPanel — reprise d'une livraison retenue", () => {
  const RETAINED = {
    ...REFUSED_ATTACHMENT,
    raw_import_id: "inbraw_retained",
    state: "ACCEPTED",
    error_code: null,
    scan_verdict: null,
  };

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("lit la proposition AVANT d'agir, et n'écrit rien pour cela", async () => {
    const calls: { url: string; method: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), method: init?.method ?? "GET" });
        const target = String(url);
        const body = target.includes("/reprocess")
          ? { availability: { available: true, reason: null }, raw_import_id: "inbraw_retained", datastream_id: "ds_1" }
          : target.includes("/inbox")
            ? { items: [RETAINED] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));

    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("/reprocess") && c.method === "GET")).toBe(true),
    );
    // L'inspection n'écrit rien : aucun POST tant que rien n'est confirmé.
    expect(calls.some((c) => c.url.includes("/reprocess") && c.method === "POST")).toBe(false);
  });

  it("rend la RAISON du refus au lieu d'un bouton qui échouera", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        const body = target.includes("/reprocess")
          ? {
              availability: {
                available: false,
                reason: "retention_expired",
                detail: "the retained object is past its retention window",
              },
              raw_import_id: "inbraw_retained",
              datastream_id: "ds_1",
            }
          : target.includes("/inbox")
            ? { items: [RETAINED] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));

    // L'indisponibilité est une VALEUR que le serveur rend, et l'écran la LIT.
    // Un bouton grisé sans phrase laisserait l'opérateur deviner ce qui manque.
    expect(await screen.findByText(/retention_expired/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Confirm reprocess/i })).toBeNull();
  });

  it("refuse de rejouer sans raison, parce que le serveur l'exige", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        const body = target.includes("/reprocess")
          ? { availability: { available: true, reason: null }, raw_import_id: "inbraw_retained", datastream_id: "ds_1" }
          : target.includes("/inbox")
            ? { items: [RETAINED] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));

    // Demander la raison ICI plutôt que laisser partir une chaîne vide évite de
    // rendre `missing_reason` à quelqu'un qui n'a jamais vu la question.
    const confirm = await screen.findByRole("button", { name: /Confirm reprocess/i });
    expect(confirm).toBeDisabled();
  });
});

/**
 * La chronologie d'une livraison — l'autre moitié de l'AC2 de 38-15.
 *
 * `GET .../deliveries/{receipt_id}` est servi depuis 38.14 et n'avait aucun
 * appelant côté console. L'inbox liste les pièces jointes d'un flux ; ceci
 * répond à la question qu'un opérateur pose devant une livraison arrêtée —
 * « qu'est-ce que CELLE-CI a produit ? »
 */
describe("WorkbenchDeliveryPanel — ce qu'une livraison a produit", () => {
  const DELIVERED = {
    ...REFUSED_ATTACHMENT,
    raw_import_id: "inbraw_delivered",
    state: "ACCEPTED",
    error_code: null,
    scan_verdict: null,
    receipt: {
      receipt_id: "inbrx_1",
      channel: "email",
      state: "PROCESSING",
      provider_event_id: null,
      created_at: "2026-08-08T02:00:00Z",
    },
  };

  function stubTimeline(
    published: boolean,
    landed: number,
    attachments: unknown[] = [],
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        const body = target.includes("/deliveries/")
          ? {
              receipt: { receipt_id: "inbrx_1", state: "PROCESSING", import_ledger_id: "mfl_1" },
              attachments,
              attachment_count: 2,
              landed_count: landed,
              published_count: published ? 1 : 0,
              published_data_changed: published,
            }
          : target.includes("/inbox")
            ? { items: [DELIVERED] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
  }

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("rend ce que le serveur DECLARE, sans le deduire d'un etat", async () => {
    stubTimeline(true, 1);
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /What did this delivery produce/i }),
    );

    expect(await screen.findByText(/1 of 2 attachment\(s\) landed/)).toBeTruthy();
    expect(screen.getByText(/published data changed/)).toBeTruthy();
    // La ligne de registre est NOMMEE : c'est la trace que l'operateur suit.
    expect(screen.getByText(/mfl_1/)).toBeTruthy();
  });

  it("dit `unchanged` quand le serveur le dit, meme si des pieces sont arrivees", async () => {
    // Le cas qui interdit la deduction : des pieces jointes traitees et RIEN de
    // publie. Un ecran qui inferait << des pieces => publie >> mentirait ici, et
    // c'est exactement le desaccord entre deux surfaces que le contrat serveur
    // existe pour empecher.
    stubTimeline(false, 0);
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /What did this delivery produce/i }),
    );

    expect(await screen.findByText(/published data unchanged/)).toBeTruthy();
  });

  /**
   * L'AC2 de 38.14 demande une colonne vertébrale qui va jusqu'à la
   * PUBLICATION. Le serveur s'arrêtait à l'analyse, et l'écran affichait
   * « atterri » comme si c'était l'arrivée. Un opérateur devant une livraison
   * arrêtée a besoin de trois chiffres que « atterri » ne porte pas : combien
   * de lignes sont passées, combien ont été refusées, et si quoi que ce soit
   * est en ligne.
   */
  it("montre le mapping, la qualite et l'issue de publication de chaque piece", async () => {
    stubTimeline(false, 1, [
      {
        raw_import_id: "inbraw_delivered",
        ordinal: 0,
        filename: "march.csv",
        state: "LANDED",
        error_code: null,
        import_ledger_id: "mfl_1",
        created_at: null,
        scan_verdict: null,
        downstream: {
          mapping: {
            mapping_version_id: "map_v3",
            plan_version_id: "plan_v1",
            write_mode: "replace",
          },
          dq: {
            accepted_row_count: 180,
            rejected_row_count: 20,
            rejected_row_pct: 10,
          },
          publication: {
            outcome: "written",
            published: false,
            execution_id: "dse_1",
            superseded_ledger_id: null,
            error_code: null,
            snapshot_observed_at: null,
          },
        },
      },
    ]);
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /What did this delivery produce/i }),
    );

    const line = await screen.findByTestId("downstream-0");
    expect(line.textContent).toContain("180 row(s) accepted, 20 rejected");
    expect(line.textContent).toContain("10%");
    // L'ISSUE EST TRADUITE, PAS RECOPIEE. « written » ne dit rien à personne ;
    // « un candidat existe, rien n'est en ligne » est ce qu'un opérateur doit
    // savoir pour décider s'il publie.
    expect(line.textContent).toContain("a candidate exists, nothing is live yet");
    expect(line.textContent).toContain("mapping map_v3");
    // Et l'en-tête distingue ce qui est atterri de ce qui est publié.
    expect(screen.getByText(/1 of 2 attachment\(s\) landed, 0 published/)).toBeTruthy();
  });

  it("n'affiche pas 0 % quand rien n'a ete compte", async () => {
    // 0 rejetée sur un total inconnu n'est pas 0 % — c'est « rien compté ». Un
    // taux propre sur un import qui n'a jamais lu une ligne rassure à tort.
    stubTimeline(false, 1, [
      {
        raw_import_id: "inbraw_delivered",
        ordinal: 0,
        filename: "march.csv",
        state: "LANDED",
        error_code: null,
        import_ledger_id: "mfl_1",
        created_at: null,
        scan_verdict: null,
        downstream: {
          mapping: { mapping_version_id: null, plan_version_id: null, write_mode: null },
          dq: { accepted_row_count: null, rejected_row_count: 0, rejected_row_pct: null },
          publication: {
            outcome: "opened",
            published: false,
            execution_id: null,
            superseded_ledger_id: null,
            error_code: null,
            snapshot_observed_at: null,
          },
        },
      },
    ]);
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /What did this delivery produce/i }),
    );

    const line = await screen.findByTestId("downstream-0");
    expect(line.textContent).toContain("no rows counted yet");
    expect(line.textContent).not.toContain("%");
  });
});

/**
 * Le contexte de réparation d'un mapping — 38-16 AC1, « rien n'est monté ».
 *
 * `get_mapping_repair_context` était servi et lu par personne. Il répond aux
 * trois questions d'un opérateur devant une livraison qui ne mappe pas : ce que
 * le fichier contenait, ce à quoi il est épinglé, comment en changer.
 */
describe("WorkbenchDeliveryPanel — contre quoi une livraison est mappée", () => {
  const UNMAPPED = {
    ...REFUSED_ATTACHMENT,
    raw_import_id: "inbraw_unmapped",
    state: "ACCEPTED",
    error_code: null,
    scan_verdict: null,
  };

  function stubContext(body: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        const payload = target.includes("/mapping-context")
          ? body
          : target.includes("/inbox")
            ? { items: [UNMAPPED] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => payload,
          text: async () => JSON.stringify(payload),
        } as unknown as Response;
      }),
    );
  }

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("montre les colonnes du fichier et ce a quoi il est epingle", async () => {
    // THE SHAPE COMES FROM THE SERVER, NOT FROM THIS FILE. This test used to
    // stub `source_columns: ["date", "campaign", "clicks"]` -- a list of
    // STRINGS, which the server has never produced. It was therefore green
    // while the screen rendered `[object Object], [object Object]` on every
    // real answer, and `tsc --noEmit` saw nothing: the type was false, not
    // invalid.
    //
    // `sourceColumnFixture` is built from the contract recorded from the server
    // (`__contracts__/inbound-server-shapes.json`), so a shape drift turns this
    // test red AND its Python twin.
    stubContext({
      available: true,
      reason: null,
      raw_import_id: "inbraw_unmapped",
      datastream_id: "ds_1",
      source_columns: [
        sourceColumnFixture({ name: "date", index: 0, detected_type: "date" }),
        sourceColumnFixture({ name: "campaign", index: 1, detected_type: "string" }),
        sourceColumnFixture({
          name: "clicks",
          index: 2,
          detected_type: "integer",
          masked_samples: ["##"],
        }),
      ],
      pinned_versions: { plan_version_id: "dsp_9", mapping_version_id: "dmap_9" },
      governed_path: { engine: "datastream_change", prepare: "/p", confirm: "/c" },
    });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /What is this mapped against/i }));

    // The NAMES, one per line -- and above all never `[object Object]`.
    expect(await screen.findByTestId("source-column-date")).toBeTruthy();
    expect(screen.getByTestId("source-column-campaign")).toBeTruthy();
    expect(screen.getByTestId("source-column-clicks")).toBeTruthy();
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
    // AC1 asks for three things: columns, TYPES, SAFE samples.
    expect(screen.getByTestId("source-column-clicks").textContent).toContain("integer");
    expect(screen.getByTestId("source-column-clicks").textContent).toContain("##");
    expect(screen.getByText(/dsp_9/)).toBeTruthy();
  });

  it("ne rend jamais [object Object] sur la forme que le serveur envoie", async () => {
    // THE TEST OF THE CLASS, not of the instance. It consumes the contract
    // recorded from the server: if `inbound_mapping_entry` changes shape without
    // the TS interface following, the Python twin turns red; if the screen goes
    // back to a rendering that flattens objects, this one turns red.
    const columnShape = (contract as ContractShape).mapping_repair_context
      .source_columns.__item__;
    expect(Object.keys(columnShape).sort()).toEqual(
      ["detected_type", "index", "masked_samples", "name", "null_count", "source_label"],
    );

    stubContext({
      available: true,
      reason: null,
      raw_import_id: "inbraw_unmapped",
      datastream_id: "ds_1",
      // A column built FIELD BY FIELD from the contract: no key invented here,
      // no server key forgotten.
      source_columns: [sourceColumnFixture({ name: "respondent_email" })],
      pinned_versions: { plan_version_id: null, mapping_version_id: null },
      governed_path: { engine: "datastream_change", prepare: "/p", confirm: "/c" },
    });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /What is this mapped against/i }));

    expect(await screen.findByTestId("source-column-respondent_email")).toBeTruthy();
    expect(screen.queryByText(/\[object Object\]/)).toBeNull();
  });

  it("RENVOIE vers le moteur gouverne au lieu de publier lui-meme", async () => {
    // La story refuse explicitement un second chemin de publication -- << ce
    // serait le quatrieme moteur que cette epique existe pour eviter >>. Cet
    // ecran NOMME le chemin et dit qu'il ne publie pas.
    stubContext({
      available: true,
      reason: null,
      raw_import_id: "inbraw_unmapped",
      datastream_id: "ds_1",
      source_columns: [],
      pinned_versions: { plan_version_id: null, mapping_version_id: null },
      governed_path: { engine: "datastream_change", prepare: "/p", confirm: "/c" },
    });
    const onRepair = vi.fn();
    renderPanel(["inbound_email"], onRepair);

    fireEvent.click(await screen.findByRole("button", { name: /What is this mapped against/i }));

    expect(await screen.findByText(/datastream_change/)).toBeTruthy();
    expect(screen.getByText(/This screen does not publish/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Repair this file's mapping/i }));
    expect(onRepair).toHaveBeenCalledWith("inbraw_unmapped");
    // Aucune colonne lisible n'est un ETAT, pas un vide silencieux.
    expect(screen.getByText(/not readable from the retained bytes/)).toBeTruthy();
  });

  it("rend la raison quand il n'y a pas de contexte du tout", async () => {
    stubContext({
      available: false,
      reason: "context_not_found",
      raw_import_id: "inbraw_unmapped",
      datastream_id: "ds_1",
    });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /What is this mapped against/i }));

    expect(await screen.findByText(/context_not_found/)).toBeTruthy();
  });
});

/**
 * La santé du connecteur entrant — 38-14 AC5, « `/health` n'a aucun consommateur ».
 *
 * Le serveur calculait une réponse à trois états et personne ne la lisait. Ces
 * tests tiennent les deux choses que l'écran ne doit surtout pas faire :
 * replier `unknown` sur vert, et recalculer `overall` depuis les couches.
 */
describe("WorkbenchDeliveryPanel — la santé entrante", () => {
  function stubHealth(health: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        const body = target.includes("/health")
          ? health
          : target.includes("/inbox")
            ? { items: [] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
  }

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("nomme la cause bloquante ET qui peut agir", async () => {
    // Une cause sans son autorité envoie quelqu'un réparer ce qu'il n'a pas le
    // droit de toucher.
    stubHealth({
      connector_name: "managed_feed",
      overall: "blocked",
      authority: "platform_operator",
      blocking_cause: "domain_unverified",
      layers: [
        { layer: "domain", state: "PENDING", healthy: false, authority: "platform_operator", blocking_cause: "domain_unverified", next_action: "verify the DNS records" },
      ],
    });
    renderPanel();

    const banner = await screen.findByTestId("inbound-health");
    expect(banner.textContent).toContain("domain_unverified");
    expect(banner.textContent).toContain("platform_operator");
    expect(banner.textContent).toContain("verify the DNS records");
  });

  /**
   * L'AC4 demande qu'une alerte « link to an authorized console/MCP recovery
   * action ». Le bandeau rendait la PHRASE et jetait l'action : un opérateur
   * lisait ce qui devrait arriver sans jamais savoir quoi appeler.
   */
  it("pointe la reparation, pas seulement la phrase qui la decrit", async () => {
    stubHealth({
      connector_name: "managed_feed",
      overall: "blocked",
      authority: "datastream_operator",
      blocking_cause: "scan_dead_letter",
      layers: [
        {
          layer: "delivery",
          state: "BLOCKED",
          healthy: false,
          authority: "datastream_operator",
          blocking_cause: "scan_dead_letter",
          next_action: "recover the dead-lettered scan",
          recovery: {
            api: "/api/connectors/{connector_name}/x/recover",
            method: "POST",
            mcp_tool: "list_inbound_attachments",
            console: null,
          },
        },
      ],
    });
    renderPanel();

    const banner = await screen.findByTestId("inbound-health");
    expect(banner.textContent).toContain("recover the dead-lettered scan");
    // Le NOM de ce qu'on appelle, et qui a le droit de l'appeler.
    expect(banner.textContent).toContain("list_inbound_attachments");
    expect(banner.textContent).toContain("datastream_operator");
  });

  it("n'offre aucune reparation quand la couche n'en a pas", async () => {
    // Une base injoignable ne se répare pas en appelant quelque chose. Un
    // bouton là inviterait à le presser en boucle pendant qu'il faudrait
    // appeler quelqu'un — et l'autorité, elle, reste affichée.
    stubHealth({
      connector_name: "managed_feed",
      overall: "unknown",
      authority: "platform_operator",
      blocking_cause: "delivery_state_unreadable",
      layers: [
        {
          layer: "delivery",
          state: "UNKNOWN",
          healthy: null,
          authority: "platform_operator",
          blocking_cause: "delivery_state_unreadable",
          next_action: "retry; if it persists the platform database is unreachable",
          recovery: null,
        },
      ],
    });
    renderPanel();

    const banner = await screen.findByTestId("inbound-health");
    expect(banner.textContent).toContain("platform_operator");
    expect(banner.textContent).not.toContain("runs ");
  });

  it("garde `unknown` comme un TROISIEME etat, jamais replie sur vert", async () => {
    // Le contrat serveur : « nothing is ever reported green on missing
    // evidence ». Un ecran qui rendrait `unknown` en succes inventerait une
    // sante que personne n'a mesuree -- exactement ce que les trois valeurs de
    // `healthy` existent pour empecher.
    stubHealth({
      connector_name: "managed_feed",
      overall: "unknown",
      authority: "none",
      blocking_cause: null,
      layers: [{ layer: "data", state: "UNREAD", healthy: null, authority: "none", blocking_cause: null }],
    });
    renderPanel();

    const banner = await screen.findByTestId("inbound-health");
    expect(banner.textContent).toContain("unknown");
    expect(banner.className).not.toMatch(/success/);
  });

  it("n'invente aucun etat quand la sante est illisible", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        if (target.includes("/health")) throw new Error("unreachable");
        const body = target.includes("/inbox") ? { items: [] } : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
    renderPanel();

    // Une sante illisible n'est ni bonne ni mauvaise : rien ne s'affiche plutot
    // qu'un etat fabrique.
    await screen.findByText(/Declared channels/);
    expect(screen.queryByTestId("inbound-health")).toBeNull();
  });
});

/**
 * Le test de routage — 38-15 AC5.
 *
 * La question qu'un opérateur pose AVANT de dire à un fournisseur d'envoyer :
 * « si un fichier arrive maintenant, atteint-il mon Datastream ? » La santé du
 * connecteur ne répond pas à celle-là — elle peut être verte pendant que ce
 * Datastream-ci ne recevrait rien, et de l'extérieur les deux pannes se
 * ressemblent : rien n'arrive.
 */
describe("WorkbenchDeliveryPanel — une livraison atteindrait-elle ce Datastream", () => {
  function stubRouting(report: Record<string, unknown> | null, ok = true) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const target = String(url);
        if (target.includes("/routing-test")) {
          return {
            ok,
            status: ok ? 200 : 500,
            headers: { get: () => "application/json" },
            json: async () => report ?? { code: "server_error", message: "unavailable" },
            text: async () => JSON.stringify(report ?? { code: "server_error" }),
          } as unknown as Response;
        }
        const body = target.includes("/inbox")
          ? { items: [] }
          : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
  }

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("ne se lance PAS au montage", async () => {
    // Il dépense un événement de cadencement. Un écran qui le déclencherait à
    // l'ouverture consommerait le quota de livraison de l'opérateur sans qu'il
    // ait rien demandé.
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ items: [] }),
      text: async () => "{}",
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchSpy);
    renderPanel();

    await screen.findByTestId("routing-test");
    const calls = (fetchSpy as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    expect(calls.every((call) => !String(call[0]).includes("/routing-test"))).toBe(true);
  });

  it("nomme le maillon casse, et laisse les suivants NON ATTEINTS", async () => {
    // Une étape que personne n'a essayée n'est pas une étape en panne.
    // L'afficher comme telle enverrait réparer un maillon peut-être intact.
    stubRouting({
      synthetic: true,
      datastream_id: "ds_1",
      channel: "email",
      routes: false,
      blocking_step: "datastream_receivable",
      blocking_reason: "delivery channel is not configured",
      steps: [
        { step: "datastream_exists", passed: true, reason: null },
        { step: "connector_binding", passed: true, reason: null },
        { step: "datastream_receivable", passed: false, reason: "delivery channel is not configured" },
        { step: "domain_ready", passed: null, reason: "not_reached" },
        { step: "credential_active", passed: null, reason: "not_reached" },
        { step: "credential_resolves_back", passed: null, reason: "not_reached" },
      ],
    });
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /Would a delivery reach this Datastream/i }),
    );

    expect(
      await screen.findByText(/would NOT arrive: delivery channel is not configured/),
    ).toBeTruthy();
    expect(
      (await screen.findByTestId("routing-step-datastream_receivable")).textContent,
    ).toContain("blocked");
    const notReached = await screen.findByTestId("routing-step-domain_ready");
    expect(notReached.textContent).toContain("not reached");
    expect(notReached.textContent).not.toContain("blocked");
  });

  it("dit ce que le test synthetique COUTE, et ne pretend pas qu il est gratuit", async () => {
    // « clearly distinguished from provider deliveries » : l'opérateur doit
    // savoir qu'aucun fichier fantôme n'apparaîtra dans son inbox.
    //
    // AND THEY MUST KNOW WHAT IT COSTS. This screen displayed "nothing was
    // written". The test calls `resolve_by_token_hash`, which RECORDS a
    // rate-limit event, and the server commits it (`inbound_mcp.py`:
    // `conn.commit()` right after). Repeated, it exhausts the resolution budget
    // and `_enforce_resolution_rate_limit` then refuses REAL deliveries. Two
    // places in the repository already said this correctly
    // (`inbound_routing_test`, `inbound_health_api`); this screen and the MCP
    // docstring said the opposite.
    stubRouting({
      synthetic: true,
      datastream_id: "ds_1",
      channel: "email",
      routes: true,
      blocking_step: null,
      blocking_reason: null,
      steps: [
        { step: "datastream_exists", passed: true, reason: null },
        { step: "connector_binding", passed: true, reason: null },
        { step: "datastream_receivable", passed: true, reason: null },
        { step: "domain_ready", passed: true, reason: null },
        { step: "credential_active", passed: true, reason: null },
        { step: "credential_resolves_back", passed: true, reason: null },
      ],
    });
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /Would a delivery reach this Datastream/i }),
    );

    // No delivery evidence: that is true, and it stays said.
    expect(await screen.findByText(/no delivery evidence was created/)).toBeTruthy();
    // But the cost is said too, and it is not nil.
    const cost = await screen.findByTestId("routing-test-cost");
    expect(cost.textContent).toContain("spends one resolution event");
    expect(cost.textContent).toContain("rate limit");
    // The sentence that lied must no longer exist anywhere on the screen.
    expect(screen.queryByText(/nothing was written/)).toBeNull();
    expect(
      (await screen.findByTestId("routing-step-credential_resolves_back")).textContent,
    ).toContain("that key routes back to this Datastream");
  });

  it("n'invente pas une panne de routage quand le test n'a pas pu tourner", async () => {
    stubRouting(null, false);
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: /Would a delivery reach this Datastream/i }),
    );

    const message = await screen.findByText(/could not run/);
    expect(message.textContent).toContain("That is not a routing failure");
    expect(screen.queryByTestId("routing-step-domain_ready")).toBeNull();
  });
});

/**
 * 38-18 AC1 — l'operateur CHOISIT la version sous laquelle le fichier est rejoue.
 *
 * Le rejeu etait cable sur `current_*`. Il ne repondait donc qu'a « rejoue le
 * mapping d'aujourd'hui », ce qui ne recupere rien quand le mapping
 * d'aujourd'hui EST le probleme — c'est-a-dire dans le cas que la story existe
 * pour traiter.
 */
describe("WorkbenchDeliveryPanel — sous quelle version rejouer", () => {
  const RETAINED = {
    ...REFUSED_ATTACHMENT,
    raw_import_id: "inbraw_retained",
    state: "ACCEPTED",
    error_code: null,
    scan_verdict: null,
  };

  const VERSIONS = [
    {
      mapping_version_id: "dmv_9",
      version_number: 9,
      executable: false,
      blocking_count: 3,
      created_at: null,
      created_by: null,
      is_current: false,
    },
    {
      mapping_version_id: "dmv_8",
      version_number: 8,
      executable: true,
      blocking_count: 0,
      created_at: null,
      created_by: null,
      is_current: true,
    },
  ];

  function stubProposal(extra: Record<string, unknown> = {}) {
    const calls: { url: string; method: string; body?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({
          url: String(url),
          method: init?.method ?? "GET",
          body: typeof init?.body === "string" ? init.body : undefined,
        });
        const target = String(url);
        const body = target.includes("/reprocess")
          ? {
              availability: { available: true, reason: null },
              raw_import_id: "inbraw_retained",
              datastream_id: "ds_1",
              available_versions: VERSIONS,
              ...extra,
            }
          : target.includes("/inbox")
            ? { items: [RETAINED] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
    return calls;
  }

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("propose les versions, et garde AFFICHEES celles qu'on ne peut pas rejouer", async () => {
    // Un brouillon qu'on ne peut pas rejouer est exactement ce qu'on cherche
    // quand on se demande pourquoi son candidat n'est pas propose. Le retirer
    // transformerait un refus explicable en absence.
    stubProposal();
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));

    const picker = await screen.findByTestId("reprocess-target-inbraw_retained");
    expect(picker.textContent).toContain("v9");
    expect(picker.textContent).toContain("not executable, 3 blocking");
    expect(picker.textContent).toContain("v8 (in force)");
    const draft = picker.querySelector('option[value="dmv_9"]') as HTMLOptionElement;
    expect(draft.disabled).toBe(true);
  });

  it("relit la proposition sous la version choisie", async () => {
    // Ce qui va arriver CHANGE avec la version. Laisser l'ancienne proposition
    // a l'ecran ferait confirmer autre chose que ce qui est affiche.
    const calls = stubProposal();
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));
    const picker = await screen.findByTestId("reprocess-target-inbraw_retained");
    fireEvent.change(picker.querySelector("select")!, { target: { value: "dmv_8" } });

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === "GET" &&
            call.url.includes("target_mapping_version_id=dmv_8"),
        ),
      ).toBe(true),
    );
  });

  it("envoie la version choisie AVEC l'execution, pas seulement avec la proposition", async () => {
    // Deux rejeux du meme fichier sous deux versions sont deux actes differents,
    // et le serveur l'inscrit dans sa trace. Une console qui ne l'enverrait
    // qu'a la proposition ferait executer autre chose que ce qui a ete relu.
    const calls = stubProposal();
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));
    const picker = await screen.findByTestId("reprocess-target-inbraw_retained");
    fireEvent.change(picker.querySelector("select")!, { target: { value: "dmv_8" } });

    const reason = await screen.findByPlaceholderText(/Why this delivery is being replayed/i);
    fireEvent.change(reason, { target: { value: "the march file was mapped wrong" } });
    fireEvent.click(await screen.findByRole("button", { name: /Confirm reprocess/i }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === "POST" &&
            (call.body ?? "").includes('"target_mapping_version_id":"dmv_8"'),
        ),
      ).toBe(true),
    );
  });

  it("rend le refus d'une version au lieu d'un bouton qui echouera", async () => {
    // Un refus decouvert a l'execution serait un refus APRES confirmation,
    // c'est-a-dire apres qu'une personne a decide.
    stubProposal({
      target_refused: "the requested mapping version is not executable",
      creates_new_execution: false,
      may_move_published_pointer: false,
    });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Reprocess this delivery/i }));

    const refusal = await screen.findByTestId("reprocess-refused-inbraw_retained");
    expect(refusal.textContent).toContain("not executable");
  });
});

/**
 * 38-18 AC1, les deux morceaux qui manquaient : la PORTÉE GOUVERNÉE et la
 * version de TEMPLATE.
 *
 * L'AC dit « target mapping/template versions and a governed scope ». La console
 * offrait la version de mapping, rien pour le Template, et un rejeu qui ne
 * portait jamais que sur UNE livraison — donc réparer un mapping et devoir
 * cliquer douze fois, sans qu'aucune trace ne dise que c'était la même
 * réparation.
 *
 * Le patron est celui du dépôt, ratifié :
 * `datastream-workbench-and-wizard.md` — « Prepare > Review exact scope and
 * consequences > Confirm > Execute as a new durable operation » — et, sur la
 * même page, « le compte AVANT l'acte ». Mesuré 2026-08-10 aux lignes 2191 et
 * 1080 ; ces numéros bougent, la phrase non.
 */
describe("WorkbenchDeliveryPanel — la portée gouvernée du rejeu", () => {
  const SCOPE_A = {
    ...REFUSED_ATTACHMENT,
    raw_import_id: "inbraw_a",
    filename: "january.csv",
    state: "ACCEPTED",
    error_code: null,
    scan_verdict: null,
  };
  const SCOPE_B = {
    ...REFUSED_ATTACHMENT,
    raw_import_id: "inbraw_b",
    filename: "february.csv",
    state: "ACCEPTED",
    error_code: null,
    scan_verdict: null,
  };

  function stubScope(proposal: unknown) {
    const calls: { url: string; method: string; body?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const target = String(url);
        calls.push({
          url: target,
          method: init?.method ?? "GET",
          body: init?.body as string | undefined,
        });
        const body = target.includes("/reprocess-scope")
          ? (init?.method ?? "GET") === "POST"
            ? { status: "landed", operation_id: "op-1" }
            : proposal
          : target.includes("/inbox")
            ? { items: [SCOPE_A, SCOPE_B] }
            : { credentials: [CREDENTIAL] };
        return {
          ok: true,
          status: 200,
          headers: { get: () => "application/json" },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }),
    );
    return calls;
  }

  const TWO_MEMBERS = {
    schema: "inbound-reprocess-scope-v1",
    datastream_id: "ds_1",
    selection: {
      mode: "explicit",
      criterion: null,
      confirm_raw_import_ids: ["inbraw_a", "inbraw_b"],
    },
    scope: { state: "counted", examined: 2, reprocessable: 2, refused: 0 },
    members: [
      {
        raw_import_id: "inbraw_a",
        filename: "january.csv",
        content_hash: "a".repeat(64),
        size_bytes: 42,
        state: "ACCEPTED",
        created_at: "2026-08-01T10:00:00Z",
        available: true,
        reason: null,
        detail: null,
      },
      {
        raw_import_id: "inbraw_b",
        filename: "february.csv",
        content_hash: "b".repeat(64),
        size_bytes: 42,
        state: "ACCEPTED",
        created_at: "2026-08-02T10:00:00Z",
        available: true,
        reason: null,
        detail: null,
      },
    ],
    scan_truncated: false,
    scan_limit: 50,
    bound_versions: { plan_version_id: "dsp_1", mapping_version_id: "dmv_1" },
    template_version: {
      state: "bound",
      template_code: "media_plan",
      version: 4,
      template_id: "fst_A",
    },
    available_template_versions: [],
    creates_new_execution: true,
    may_move_published_pointer: true,
  };

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("prépare la portée avant de l'exécuter, et l'annonce par son COMPTE", async () => {
    const calls = stubScope(TWO_MEMBERS);
    renderPanel();

    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(await screen.findByTestId("scope-select-inbraw_b"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );

    const proposal = await screen.findByTestId("scope-proposal");
    // LE COMPTE AVANT L'ACTE, et les objets qu'il nomme.
    expect(proposal.textContent).toContain("2 of 2 selected file(s)");
    expect(
      (await screen.findByTestId("scope-member-inbraw_a")).textContent,
    ).toContain("january.csv");
    // La préparation N'ÉCRIT RIEN : aucun POST tant que rien n'est confirmé.
    expect(
      calls.some((c) => c.url.includes("/reprocess-scope") && c.method === "POST"),
    ).toBe(false);
  });

  it("confirme sur les objets ÉNUMÉRÉS, en UNE opération", async () => {
    const calls = stubScope(TWO_MEMBERS);
    renderPanel();

    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(await screen.findByTestId("scope-select-inbraw_b"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );
    await screen.findByTestId("scope-proposal");

    fireEvent.change(screen.getByPlaceholderText(/Why this scope/i), {
      target: { value: "mapping repaired for the whole quarter" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Confirm reprocess of 2 file/i }),
    );

    await waitFor(() => {
      const posts = calls.filter(
        (c) => c.url.includes("/reprocess-scope") && c.method === "POST",
      );
      // UNE seule requête pour deux fichiers : pas N actes muets.
      expect(posts).toHaveLength(1);
      const sent = JSON.parse(posts[0].body ?? "{}");
      expect(sent.raw_import_ids).toEqual(["inbraw_a", "inbraw_b"]);
      expect(sent.reason).toBe("mapping repaired for the whole quarter");
    });
  });

  it("refuse de confirmer sans raison", async () => {
    stubScope(TWO_MEMBERS);
    renderPanel();

    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );
    await screen.findByTestId("scope-proposal");

    expect(
      screen.getByRole("button", { name: /Confirm reprocess of/i }),
    ).toBeDisabled();
  });

  it("compte le membre refusé À PART, et confirme sur les objets ÉNUMÉRÉS", async () => {
    // LE CHANGEMENT DE RÉPONSE : mêmes deux fichiers cochés, un objet devenu
    // illisible. Le compte des rejouables BAISSE, le refus reste nommé, et la
    // confirmation part sur ce que le SERVEUR a énuméré, pas sur ce que l'écran
    // a coché. Confirmer la sélection d'écran rejouerait un fichier que la
    // proposition venait de déclarer non rejouable.
    const calls = stubScope({
      ...TWO_MEMBERS,
      selection: { ...TWO_MEMBERS.selection, confirm_raw_import_ids: ["inbraw_a"] },
      scope: { state: "counted", examined: 2, reprocessable: 1, refused: 1 },
      members: [
        TWO_MEMBERS.members[0],
        {
          ...TWO_MEMBERS.members[1],
          available: false,
          reason: "quarantine_object_unavailable",
          detail: "the retained object could not be read from quarantine",
        },
      ],
    });
    renderPanel();

    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(await screen.findByTestId("scope-select-inbraw_b"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );

    const proposal = await screen.findByTestId("scope-proposal");
    expect(proposal.textContent).toContain("1 of 2 selected file(s)");
    expect(proposal.textContent).toContain("1 cannot be");
    const refused = await screen.findByTestId("scope-member-inbraw_b");
    expect(refused.textContent).toContain("quarantine_object_unavailable");

    fireEvent.change(screen.getByPlaceholderText(/Why this scope/i), {
      target: { value: "mapping repaired" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Confirm reprocess of 1 file/i }),
    );

    await waitFor(() => {
      const posts = calls.filter(
        (c) => c.url.includes("/reprocess-scope") && c.method === "POST",
      );
      expect(posts).toHaveLength(1);
      // DEUX cochés à l'écran, UN énuméré par le serveur : c'est l'énuméré qui
      // part. L'autre lecture rejouerait le fichier illisible.
      expect(JSON.parse(posts[0].body ?? "{}").raw_import_ids).toEqual([
        "inbraw_a",
      ]);
    });
  });

  it("dit la BORNE au lieu de tronquer en silence", async () => {
    stubScope({ ...TWO_MEMBERS, scan_truncated: true, scan_limit: 50 });
    renderPanel();

    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );

    // Une troncature muette laisserait quelqu'un croire qu'un ensemble a été
    // rejoué alors qu'il ne l'a jamais été.
    expect(await screen.findByText(/Scope truncated/i)).toBeTruthy();
    expect((await screen.findByTestId("scope-proposal")).textContent).toContain(
      "were NOT included",
    );
  });

  it("nomme la version de TEMPLATE, et ne confond pas ses états", async () => {
    stubScope(TWO_MEMBERS);
    const bound = renderPanel();
    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );
    expect((await screen.findByTestId("scope-proposal")).textContent).toContain(
      "Template media_plan v4",
    );
    bound.unmount();

    // LE CHANGEMENT DE RÉPONSE. Même écran, un état différent : « n'épingle
    // aucun Template » ne doit pas se lire comme « v4 », et surtout pas comme
    // une cellule vide.
    stubScope({
      ...TWO_MEMBERS,
      template_version: {
        state: "not_applicable",
        reason: "this Datastream is not pinned to a file-source Template",
      },
    });
    renderPanel();
    fireEvent.click(await screen.findByTestId("scope-select-inbraw_a"));
    fireEvent.click(
      await screen.findByRole("button", { name: /Prepare a scoped reprocess/i }),
    );
    const none = await screen.findByTestId("scope-proposal");
    expect(none.textContent).toContain("not pinned to a file-source Template");
    expect(none.textContent).not.toContain("Template media_plan v4");
  });
});
