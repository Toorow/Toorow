/**
 * The frame the scope surfaces share — Organization Settings, Project Access,
 * User Account and Getting Started.
 *
 * It used to draw its sections as a CARD OF ROWS on the left, which no other
 * page of the console does. `ProjectSettings` — the fifth surface of the same
 * family — already renders the validated band: a `PageHeader` whose eyebrow
 * carries the governed path, then the underline tabs with the 3px rose mark
 * (`application-v3.css:403-431`). Two shapes for one family, and four of the
 * five were the odd one out. Jean, looking at them: *"pourquoi pour project et
 * pour organisation ça suit pas cette organisation"*.
 *
 * So the band, from the library's `Tabs` — not a second implementation of it.
 * The section DESCRIPTIONS the row list showed are not dropped: a tab is a
 * label, so the active section's description moves under the band, where it
 * reads as the page's own subtitle instead of seven subtitles at once.
 *
 * The "Current scope" panel is gone with the row list. It boxed what the
 * eyebrow already says — and on Getting Started what it said was a raw ULID.
 * The scope now sits in the eyebrow, exactly as `ProjectSettings` writes it:
 * `Organization · Project`.
 */
import type { ReactNode } from "react";
import { PageFrame, PageHeader, Tabs, TabsContent, TabsList, TabsTrigger } from "../ui";

export interface GlobalScopeSection<T extends string> { key: T; label: string; description?: string; }
export default function GlobalScopeLayout<T extends string>({
  eyebrow,
  title,
  description,
  scopeLabel,
  sections,
  activeSection,
  onSectionChange,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  /** The governed path this surface acts on. Joined to the eyebrow, never boxed. */
  scopeLabel: string;
  sections: readonly GlobalScopeSection<T>[];
  activeSection: T;
  onSectionChange: (section: T) => void;
  children: ReactNode;
}) {
  const active = sections.find((item) => item.key === activeSection);
  return (
    <PageFrame>
      <PageHeader
        eyebrow={scopeLabel ? `${eyebrow} · ${scopeLabel}` : eyebrow}
        title={title}
        description={description}
      />
      <Tabs value={activeSection} onValueChange={(value) => onSectionChange(value as T)}>
        <TabsList className="mb-6" aria-label={`${title} sections`}>
          {sections.map((item) => (
            <TabsTrigger key={item.key} value={item.key}>
              {item.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value={activeSection} className="space-y-6" aria-live="polite">
          {active?.description ? (
            <p className="text-body text-text-secondary">{active.description}</p>
          ) : null}
          {children}
        </TabsContent>
      </Tabs>
    </PageFrame>
  );
}
