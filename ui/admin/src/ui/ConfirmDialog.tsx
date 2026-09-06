/**
 * ConfirmDialog — standard confirmation dialog primitive for irreversible or high-consequence operations.
 *
 * Eliminates ad-hoc dialogs built across screens for confirmation.
 */
import type { ReactNode } from "react";
import { Button } from "../components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "../components/ui/dialog";
import { Status } from "./Data";
import { EvidenceRows } from "./Evidence";

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  evidence?: Record<string, unknown>;
  evidenceLabel?: string;
  confirmLabel: string;
  /**
   * The way OUT, when "Cancel" is the wrong word for it.
   *
   * Default and existing behaviour is `Cancel`. It is overridable because the
   * two Render revoke confirmations say "Keep the share" and "Keep the link":
   * next to an irreversible destructive action, a label that names what
   * SURVIVES is not decoration — it is the sentence that stops the wrong click,
   * and a person reading "Cancel" beside "Revoke it" has to work out which verb
   * cancels which. Those two dialogs were hand-rolled partly to keep this word,
   * so the primitive absorbs it rather than flattening it.
   */
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  /**
   * Test hooks, and the only props here that are not design decisions — the same
   * exemption `Status` documents beside its own. A confirmation dialog is exactly
   * what a screen's test identifies ("the disconnect confirmation is showing, and
   * cancelling leaves the connection alone"), and without them every caller has
   * to hand-roll its dialog to keep its own ids — which is how the ad-hoc dialogs
   * this component replaced came to exist.
   */
  "data-testid"?: string;
  cancelTestId?: string;
  confirmTestId?: string;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  evidence,
  evidenceLabel,
  confirmLabel,
  cancelLabel = "Cancel",
  destructive = false,
  busy = false,
  error = null,
  onConfirm,
  "data-testid": testId,
  cancelTestId,
  confirmTestId,
}: ConfirmDialogProps) {
  if (!open) return null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid={testId}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {evidence && evidenceLabel ? (
          <EvidenceRows source={evidence} label={evidenceLabel} />
        ) : null}
        {error ? <Status tone="error">{error}</Status> : null}
        <DialogFooter>
          <Button
            type="button"
            variant="secondary"
            onClick={() => onOpenChange(false)}
            data-testid={cancelTestId}
          >
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={destructive ? "destructive" : "default"}
            disabled={busy}
            onClick={onConfirm}
            data-testid={confirmTestId}
          >
            {busy ? "Processing…" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
