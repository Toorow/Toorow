/**
 * MediaplansShell — Story 22.7 (câblage interne liste ↔ détail).
 *
 * Reçoit le projectId du shell App (pattern identique aux autres pages).
 * Gère la navigation interne : liste des plans → détail d'un plan.
 * Aucun react-router-dom (décision 2.4 de routage interne).
 */
import { useState } from "react";
import MediaplansPage from "./MediaplansPage";
import MediaplanDetailPage from "./MediaplanDetailPage";

interface MediaplansShellProps {
  projectId: string;
  apiBase?: string;
}

export default function MediaplansShell({ projectId, apiBase = "" }: MediaplansShellProps) {
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);

  if (selectedPlanId) {
    return (
      <MediaplanDetailPage
        planId={selectedPlanId}
        onBack={() => setSelectedPlanId(null)}
        apiBase={apiBase}
      />
    );
  }

  return (
    <MediaplansPage
      projectId={projectId}
      onSelectPlan={setSelectedPlanId}
      apiBase={apiBase}
    />
  );
}
