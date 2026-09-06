/**
 * Tone-driven Alert / Callout component.
 *
 * Bridges MUI `Alert` and ad-hoc inline callouts to the console's unified `Status`
 * primitive with `as="block"`. Supports both `tone` ("error" | "warning" | "success" |
 * "neutral" | "accent") and legacy MUI `severity` ("error" | "warning" | "info" | "success").
 */
import type { ReactNode } from "react";
import { Status } from "./Data";
import type { Fill } from "./tone";

export interface AlertProps {
  severity?: "error" | "warning" | "info" | "success" | Fill;
  tone?: Fill;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
  "data-testid"?: string;
}

export function Alert({
  severity,
  tone,
  title,
  children,
  action,
  className,
  "data-testid": testId,
}: AlertProps) {
  let resolvedTone: Fill = tone ?? "neutral";
  if (severity) {
    if (severity === "error") resolvedTone = "error";
    else if (severity === "warning") resolvedTone = "warning";
    else if (severity === "info") resolvedTone = "accent";
    else if (severity === "success") resolvedTone = "success";
    else resolvedTone = severity as Fill;
  }
  return (
    <Status
      as="block"
      tone={resolvedTone}
      title={title}
      action={action}
      className={className}
      data-testid={testId}
    >
      {children}
    </Status>
  );
}
