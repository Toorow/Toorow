/**
 * RawMarkdown — the project's ONE lightweight Markdown surface (Story 44.1/44.4).
 *
 * Doctrine (R44-UX03, Atlas faithful-to-source rule): what the agent reads is
 * what the operator sees. We deliberately render `body_md` VERBATIM as
 * preformatted text rather than transforming it — no Markdown parser, no
 * sanitizer surface, no paraphrase, and no new dependency in ui/admin.
 *
 * It was born inline in KnowledgeBasePage's editor preview (44.1); the graph
 * drawer (44.4) needs exactly the same treatment for the same bodies, so it is
 * extracted here rather than duplicated. The rendered DOM is unchanged: a
 * single <pre> whose whitespace/typography come from the caller's CSS class.
 */

export default function RawMarkdown({
  text,
  className,
  placeholder = "",
  testId,
}: {
  /** The raw Markdown source, rendered verbatim. */
  text: string;
  /** Caller-owned class carrying the wrapping/typography (tokenized CSS). */
  className?: string;
  /** Shown when `text` is empty — never invented content, just a hint. */
  placeholder?: string;
  testId?: string;
}) {
  return (
    <pre className={className} data-testid={testId}>
      {text || placeholder}
    </pre>
  );
}
