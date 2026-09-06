/**
 * The Test gate, stated in its own words, and the one gesture that passes it.
 *
 * A Concept or a Semantic View with no Test verdict is `unverifiable`, and the
 * server blocks publication on it -- correctly: missing coverage is not a pass.
 * It accepts a written, authorized override on `prepare` (`test_gate_override`),
 * records who wrote it and what state it replaced, and refuses a reason under 20
 * characters because "a blank override is indistinguishable from no gate".
 *
 * Neither dialog ever sent one. So the screen named a gate and offered nothing
 * to do about it: a dead end on the last step of publishing meaning. This panel
 * is that gesture, and it appears ONLY once the gate has spoken -- the question
 * does not exist until the answer to the previous one made it necessary.
 */
import { Field } from "../ui/Form";
import { Status, Textarea } from "../ui";

export const OVERRIDE_MINIMUM_REASON = 20;

export type TestGate = {
  state?: string;
  reason?: string;
  message?: string;
};

/** Is this what blocked publication, rather than a named field refusal? */
export function gateBlocksPublication(
  gate: TestGate | undefined,
  namedRefusals: unknown[],
): boolean {
  if (namedRefusals.length > 0) return false;
  const state = gate?.state;
  return Boolean(state) && state !== "pass" && state !== "overridden";
}

export function TestGateOverridePanel({
  gate,
  reason,
  onReasonChange,
  objectNoun,
}: {
  gate: TestGate;
  reason: string;
  onReasonChange: (value: string) => void;
  objectNoun: string;
}) {
  const remaining = OVERRIDE_MINIMUM_REASON - reason.trim().length;
  return (
    <Status as="block" tone="warning" title={`This ${objectNoun} has no Test verdict`}>
      {/* THE SERVER'S OWN SENTENCE. Writing a second one here would be a second
          answer that can disagree with the first. */}
      <p className="mb-0">{gate.message}</p>
      <div className="mt-3">
        <Field
          label="Why publish it anyway"
          required
          hint={
            remaining > 0
              ? `${remaining} more characters. This reason is recorded with your name and the state it replaced.`
              : "Recorded with your name and the state it replaced."
          }
        >
          {(fieldProps) => (
            <Textarea
              {...fieldProps}
              rows={3}
              value={reason}
              onChange={(event) => onReasonChange(event.target.value)}
              placeholder="What makes this safe to publish before it is covered by a test."
            />
          )}
        </Field>
      </div>
    </Status>
  );
}
