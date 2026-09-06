import { useState } from "react";
import { Button, Input, NativeSelect, Panel, PanelHeader, Status } from "../../ui";
import { apiFetch } from "../../lib/apiFetch";
import type { FormEvent } from "react";

export default function ExternalBqVocabularyPanel({
  projectId,
  datastreamId,
}: {
  projectId: string;
  datastreamId: string;
}) {
  const [declName, setDeclName] = useState("");
  const [declKind, setDeclKind] = useState<"dimension" | "metric">("dimension");
  const [declAggregation, setDeclAggregation] = useState("sum");
  const [declValueType, setDeclValueType] = useState("string");
  const [declBusy, setDeclBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const declareField = async (e: FormEvent) => {
    e.preventDefault();
    setDeclBusy(true);
    setMessage(null);
    try {
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/file-source-templates/canonical-fields`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            datastream_id: datastreamId,
            fields: [
              declKind === "metric"
                ? {
                    canonical_name: declName,
                    concept_kind: "metric",
                    value_type: declValueType,
                    aggregation: declAggregation,
                  }
                : {
                    canonical_name: declName,
                    concept_kind: "dimension",
                    value_type: declValueType,
                  },
            ],
          }),
        },
      );
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.message ?? "The canonical field could not be declared.");
      }
      // This route writes `app.mdm_canonical_fields`, so the object is a
      // CANONICAL FIELD and never a Semantic Concept (glossary, 2026-08-14).
      setMessage("Canonical field declared. It is now available for mapping.");
      setDeclName("");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "The canonical field could not be declared.");
    } finally {
      setDeclBusy(false);
    }
  };

  return (
    <div className="my-6">
      <Panel>
        <PanelHeader
          title="External BigQuery Vocabulary"
          description="Declare how to project External BigQuery canonical fields to semantic models."
        />
        {message && (
          <div className="p-3">
            <Status as="block" tone={message.includes("successfully") ? "success" : "error"}>
              {message}
            </Status>
          </div>
        )}
        <div className="flex flex-col gap-2 p-5 bg-ui bg-surface-secondary border-t border-divider-base">
          <span className="text-ui text-text-primary font-medium">
            Declare a new canonical concept
          </span>
          <div className="flex flex-wrap items-center gap-3">
            <Input
              placeholder="canonical_name"
              value={declName}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setDeclName(e.target.value)}
              disabled={declBusy}
            />
            <NativeSelect
              value={declKind}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setDeclKind(e.target.value as "dimension" | "metric")}
              disabled={declBusy}
            >
              <option value="dimension">Dimension</option>
              <option value="metric">Metric</option>
            </NativeSelect>
            <NativeSelect
              value={declValueType}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setDeclValueType(e.target.value)}
              disabled={declBusy}
            >
              <option value="string">string</option>
              <option value="int64">int64</option>
              <option value="float64">float64</option>
              <option value="bool">bool</option>
              <option value="date">date</option>
              <option value="timestamp">timestamp</option>
            </NativeSelect>
            {declKind === "metric" && (
              <NativeSelect
                value={declAggregation}
                onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setDeclAggregation(e.target.value)}
                disabled={declBusy}
              >
                <option value="sum">Sum</option>
                <option value="average">Average</option>
                <option value="count">Count</option>
                <option value="count_distinct">Count Distinct</option>
                <option value="max">Max</option>
                <option value="min">Min</option>
              </NativeSelect>
            )}
            <Button
              variant="secondary"
              disabled={!declName.trim() || declBusy}
              onClick={declareField}
            >
              {declBusy ? "Declaring..." : "Declare"}
            </Button>
          </div>
        </div>
      </Panel>
    </div>
  );
}
