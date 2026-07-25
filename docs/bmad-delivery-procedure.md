# BMAD Delivery Procedure

This procedure is the repository gate for turning product work into reviewed,
documented changes. Every implementation chantier starts from an accepted epic
and an executable story file. Epic prose alone is not an implementation brief.

## Lifecycle

1. **Correct course when priorities or scope change.** Use `bmad-correct-course`
   to document the trigger, affected contracts, epic impact, recommended path
   and decisions requiring approval. Do not silently rewrite accepted epics.
2. **Agree the epic together.** Reconcile product, architecture, UX and current
   implementation. Record dependencies, non-goals and delivery order.
3. **Create one story in a fresh context.** Use `bmad-create-story` for the next
   story only. Move it to `ready-for-dev` only after the Definition of Ready.
4. **Develop from the story file in a fresh context.** Use `bmad-dev-story` with
   the exact story path. Stay inside its acceptance criteria and record every
   discovery or deviation in the story before ending the session.
5. **Review in a fresh context.** Use `bmad-code-review`; add adversarial and
   edge-case review for data, security, architecture or cross-cutting work.
6. **Return findings to development.** Fix accepted findings through the same
   story, update its Dev Agent Record and rerun proportional verification.
7. **Checkpoint the result.** Use `bmad-checkpoint-preview` for UX and workflows
   where human interpretation matters.
8. **Commit atomically.** Stage explicit paths, inspect the staged diff, run the
   story gates, then commit one coherent concern. Sprint and story status must
   describe the state actually reached.
9. **Close the epic deliberately.** Run its review and retrospective before
   declaring it done or opening the next expansion wave.

## Definition of Ready

A story is ready for development only when it contains:

- the user outcome, current behavior and concrete acceptance criteria;
- explicit non-goals and upstream/downstream dependencies;
- binding capability and architecture-decision references;
- persisted contracts, migrations and compatibility expectations;
- for data work: grain, writer ownership, identity, watermark, mapping,
  replay/idempotency, schema-drift and double-counting rules;
- project isolation, authorization and secret-handling requirements;
- success, empty, partial, stale and failure states for user-facing workflows;
- required unit, seam, integration, reconciliation and live-service evidence;
- documentation and project-memory updates required by the change.

If one decision materially changes product scope or architecture, stop and use
`bmad-correct-course` or the relevant planning workflow first.

## Definition of Done

A story is done only when:

- every acceptance criterion is evidenced in the story;
- focused tests and required broader gates pass;
- analytical changes prove stable grain, unchanged unrelated totals and no
  accidental double counting;
- live external-service verification is recorded when required;
- user-facing and operator documentation is current;
- findings are fixed or explicitly accepted as tracked follow-up work;
- the Dev Agent Record lists changed files, commands, results and risks;
- the working tree contains no unexplained files attributable to the story;
- sprint status and story status agree.

## No undocumented work rule

Every discovered issue must end in one of four places before the session ends:

1. fixed inside the current story when required by an acceptance criterion;
2. added to the story risks/follow-up section with impact and evidence;
3. added as a proposed story under an accepted epic;
4. escalated through `bmad-correct-course` when it changes scope, sequencing or
   an architecture invariant.

Do not leave decisions only in chat history, code comments or agent memory. Do
not expand a story silently to absorb adjacent cleanup.

## Recommended prompts

### Epic or priority change

> Use `bmad-correct-course` to assess this change against the current spec,
> architecture, epics, sprint status and implementation. Produce a proposal and
> wait for approval before changing accepted planning artifacts.

### Story creation

> Use `bmad-create-story` to create the next executable story for Epic 12. Read
> the accepted epic, architecture decisions, UX artifact, current code and prior
> story learnings. Apply the repository Definition of Ready and do not implement.

### Story development

> Use `bmad-dev-story` on `<absolute story path>`. Implement only the accepted
> scope, preserve unrelated changes, update the Dev Agent Record and run every
> gate required by the story.

### Review

> Use `bmad-code-review` on `<absolute story path>` and its implementation diff.
> Verify acceptance criteria, seams, failure states, tenant boundaries, data
> reconciliation and documentation. Report actionable findings with evidence.

## Git discipline for stories

- Never stage with `git add .` in a dirty worktree.
- Write the intended file list before staging and inspect `git diff --cached`.
- Keep planning approval, implementation and unrelated cleanup in separate
  commits when each is independently reviewable.
- A story commit includes the story record and sprint-status update describing
  that implementation state; it does not absorb another unfinished story.
- Do not push, deploy or provision without the corresponding authorization and
  environment gate.