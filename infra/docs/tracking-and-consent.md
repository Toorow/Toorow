# Tracking & consent — toorow marketing site

Scope: **our own toorow surfaces** — the marketing site (`web/`, `toorow.com`) and
the Mintlify docs (`docs.toorow.com`); the console (`app.toorow.com`) shares the
same container when it loads GTM. The product and connectors that other people
self-host carry **no** analytics — we never measure external deployments.

**Single source of truth is the GTM container `GTM-P6TC4GFG`.** Consent defaults,
the cookie banner, GA4, Klaviyo, conversions — all live in the container, not in
page code. Each surface only loads GTM; the container drives everything, so the
site and the docs behave identically. The marketing site additionally gates the
GTM loader:

1. **Env-gated** — GTM loads only if `PUBLIC_GTM_CONTAINER_ID` is a valid `GTM-…` id.
2. **Host-gated** — and only if `location.hostname` is in `PUBLIC_ANALYTICS_HOSTS`.
3. **Consent-gated** — Consent Mode v2 keeps analytics/ad `denied` everywhere until
   the visitor opts in (global opt-in).

(Mintlify loads GTM via `integrations.gtm`, so the docs are covered without page code.)

## Architecture

```
Each surface loads GTM  ──►  GTM container GTM-P6TC4GFG (258883748)
                               ├─ Consent — Init defaults  (Custom HTML, Consent Init)
                               │    global opt-in: all denied until consent + restore cookie
                               ├─ Consent — Banner         (Custom HTML, All Pages)
                               │    injects https://toorow.com/consent.js (web/public/consent.js)
                               │      · Banner: Accept all / Reject all / Manage
                               │      · Modal: Necessary (locked) · Analytics · Advertising
                               │      · cookie toorow_consent, Domain=.toorow.com (12mo)
                               │      · gtag('consent','update') + dataLayer 'consent_update'
                               │      · POSTs the GDPR proof row to Supabase
                               │      · pushes generate_lead / github_click / open_console
                               ├─ GA4 — Configuration (G-RSD6PC9DJV) + content_group
                               ├─ GA4 event tags: generate_lead · github_click · open_console
                               └─ Klaviyo — onsite (consent-gated on analytics_storage)
```

The banner lives in a versioned, self-contained file (`web/public/consent.js`,
hard-coded palette so it renders on Mintlify too) and is injected by GTM on every
surface — so there is exactly one banner everywhere and it is easy to maintain.

## Consent model (Consent Mode v2)

**Global opt-in**: denied everywhere by default until the visitor consents — so a
compliance scanner (e.g. Axeptio **Taste**, https://taste.axept.io) sees zero
trackers before consent regardless of where it scans from. Trade-off: no measurement
of non-EEA visitors until they opt in.

| Category            | Signals updated                                             | Default (all regions) |
| ------------------- | ---------------------------------------------------------- | --------------------- |
| Strictly necessary  | `functionality_storage`, `security_storage`                | granted (locked)      |
| Analytics           | `analytics_storage`                                        | denied                |
| Advertising         | `ad_storage`, `ad_user_data`, `ad_personalization`         | denied                |

- `ads_data_redaction: true` and `url_passthrough: true` are set so basic
  measurement survives a "reject" without cookies.
- The choice is stored as `toorow_consent={"v":1,"analytics":bool,"ad":bool,"ts":…}`.
- Returning visitors are **not** re-prompted: the head script re-applies the
  stored choice before GTM, and the banner stays hidden.
- All four v2 signals are provisioned now even though only GA4 + Klaviyo fire, so
  turning on Google Ads later needs no consent rework.

## Cookie inventory (keep `web/src/pages/cookies.astro` in sync)

| Cookie          | Category   | Provider | Purpose                                   |
| --------------- | ---------- | -------- | ----------------------------------------- |
| `toorow_consent`| Necessary  | toorow   | Remembers the consent choice              |
| `_ga`, `_ga_*`  | Analytics  | Google   | GA4 visitor/session measurement           |
| `__kla_id`      | Analytics  | Klaviyo  | Links on-site activity to a contact       |

Not cookies: theme/lang are `localStorage`. Google Fonts set no cookies. Sanity
is build-time only (no client-side cookies on visitor pages).

## Tracking plan (conversions of interest today: demo lead + GitHub)

The site pushes these events to `dataLayer`; create matching **Custom Event**
triggers + GA4 Event tags in GTM. Consent-gate the GA4 tags on `analytics_storage`.

| dataLayer event   | Fires when                              | GA4 event      | Mark as conversion |
| ----------------- | --------------------------------------- | -------------- | ------------------ |
| `generate_lead`   | Mailing-list / demo form submitted      | `generate_lead`| ✅ (primary)       |
| `github_click`    | Any outbound link to `github.com`       | `github_click` | ✅                 |
| `open_console`    | Any link to `app.toorow.com`            | `open_console` | optional           |
| `consent_update`  | Visitor accepts/saves/rejects           | `consent_update` (audit) | no       |

Event params available: `lead_source` + `deployment_interest` (generate_lead),
`link_url` (github_click / open_console), `consent_analytics` / `consent_ad`.

### Audience segmentation — managed vs self-hosted (`deployment_interest`)
Two-step, non-blocking flow in `MailingListForm.astro` (email is the lead; the
segmentation is a bonus follow-up):
1. **Subscribe** — the email submits via the Klaviyo **client subscribe API** (AJAX,
   stays on page) to the beta list `XbwNdF`; `generate_lead` fires. A **thank-you**
   panel replaces the form. No-JS fallback = classic kmail-lists POST.
2. **Choose** — the thank-you shows two boxes: **Run it for me** (`managed`) /
   **I'll self-host** (`self_hosted`). Clicking one calls the Klaviyo **client
   profiles API** to set the profile property `deployment_interest` on the (already
   subscribed) email, and fires `select_deployment_interest`.

- **Klaviyo**: `deployment_interest` profile property → segments "Interested in
  Managed" vs "Self-hosted" → different flows.
- **GA4**: `select_deployment_interest` tag (id 18) sends the `deployment_interest`
  event param **and** sets it as a GA4 **user property** (GTM var
  `DLV - deployment_interest` id 16, trigger 17). **Manual step**: register
  `deployment_interest` as a **user-scoped custom dimension** in GA4 Admin.
- Explicit only. Behavioural inference (github=self-hosted, console=managed) is
  intentionally NOT wired — a click ≠ a firm intent.

### Cross-subdomain measurement & page naming
- **One session across `toorow.com`, `docs.toorow.com`, `app.toorow.com`.** These are
  all subdomains of one registrable domain, so GA4 stitches the session
  automatically (the `_ga` cookie is written at `.toorow.com`). No cross-domain
  linker is needed — that is only for different registrable domains.
- **Shared consent.** The `toorow_consent` cookie is written with `Domain=.toorow.com`
  so the visitor is asked once and the choice follows them across all three
  subdomains (host-only on localhost / preview hosts).
- **Content grouping (page-name optimization).** GTM variable `JS - Content Group`
  (id 13) classifies every page from host + path into: `Docs`, `App`, `Connectors`,
  `Manifesto`, `Legal`, `Resources`, `Solutions`, `Marketing`. It is sent on every
  `page_view` via the GA4 config tag's `content_group` field, so GA4 reports segment
  cleanly by area instead of by raw URL. Adjust the taxonomy in that variable.
- **Page title convention.** GA4 auto-captures `page_title` from `<title>`; keep
  titles as `<Page> — toorow` for readable reports (already the pattern on most
  pages; the homepage title comes from Sanity `seo.title`).

### GTM container — built in the Default Workspace (draft, NOT published)
Container `GTM-P6TC4GFG` (id 258883748, "www.toorow.com"). Built via MCP 2026-07-20:

| Entity | id | Notes |
| ------ | -- | ----- |
| Tag `Consent — Init defaults` (Custom HTML) | 12 | Consent Init trigger; global opt-in defaults + restore cookie |
| Tag `Consent — Banner (consent.js)` (Custom HTML) | 14 | All Pages; injects `https://toorow.com/consent.js` |
| Tag `Klaviyo — onsite` (Custom HTML) | 15 | All Pages; consent-gated on `analytics_storage` |
| Tag `GA4 — Configuration` (Google tag) | 3 | `G-RSD6PC9DJV`, All Pages, `send_page_view`, `content_group` |
| Tag `GA4 - generate_lead` (gaawe) | 9 | trigger 4, param `lead_source` |
| Tag `GA4 - github_click` (gaawe) | 10 | trigger 5, param `link_url` |
| Tag `GA4 - open_console` (gaawe) | 11 | trigger 6, param `link_url` |
| Triggers `CE - *` (customEvent) | 4/5/6 | match the dataLayer event names |
| Variables `DLV - lead_source` / `DLV - link_url` | 7/8 | dataLayer v2 |
| Variable `JS - Content Group` | 13 | content group by host + path |

**PUBLISHED** as version 2 ("Consent + GA4 + Klaviyo + segmentation") on 2026-07-21
via MCP — the container is live.

Remaining (needs a human in the GTM/GA4 UI):
1. In **GA4 Admin → Events/Key events**, mark `generate_lead` (and `github_click`)
   as key events (conversions).
2. In **GA4 Admin → Custom definitions**, register `content_group` (user/event),
   `deployment_interest` (user-scoped) and optionally `lead_source`/`link_url`.
3. Merge the Mintlify PR (docs `integrations.gtm`) to cover `docs.toorow.com`.
4. Deploy the marketing site (so `toorow.com/consent.js` exists), then run
   https://taste.axept.io on the live URLs to confirm a clean (green) scan.
5. Optional: GTM Preview on `toorow.com` to eyeball the consent gating once deployed.

## Application (`app.toorow.com`) — server-side, GA4 Measurement Protocol

The self-hostable product (this repo: MCP server, connector modules, widgets)
carries **no client-side analytics** — deliberately. The only browser-served HTML
in the repo is the sandboxed MCP Apps widgets and a print-to-PDF report; neither is
a safe or appropriate place for GA, and both ship to every deployment. So we do
**not** inject GA into the app.

Instead the maintainers' **hosted** instance measures product usage with the GA4
**Measurement Protocol** (server → GA4 HTTP API):

- A small, generic telemetry emitter POSTs events to
  `https://www.google-analytics.com/mp/collect?measurement_id=…&api_secret=…`.
- Gated by env vars `GA4_MP_MEASUREMENT_ID` + `GA4_MP_API_SECRET`, **unset in git /
  `.env.example`**. Unset ⇒ no-op ⇒ self-hosted instances measure nothing
  ("pas de mesure à l'externe"). Same opt-in-by-env contract as Langfuse tracing.
- **AD-2**: the emitter is generic — event names come from the call site / config,
  never a hardcoded connector slug in `core/`.
- **Stitching**: pass the GA `client_id` from the `_ga` cookie (shared on
  `.toorow.com`) into MP events so the console journey joins the same GA4 user as
  the marketing site — one funnel from `toorow.com` → `docs` → `app`.
- **Privacy**: send aggregate product-usage events, no PII; the operator is the
  data controller under its own privacy policy. Honour the user's consent state
  when an event could carry personal data.

**Rule (extends "pas de mesure à l'externe"):** shipped/self-hostable code stays
analytics-free; measurement of toorow-hosted surfaces is injected only by a layer
the maintainers exclusively operate (marketing `web/` env+host gate; app = GA4 MP
env gate), never committed active, never present on a self-hosted deployment.

## Klaviyo
- Lead capture: the form POSTs to the Klaviyo list-subscribe endpoint
  `https://manage.kmail-lists.com/subscriptions/subscribe` with hidden fields
  `a` = company `SNrDaJ` (`PUBLIC_KLAVIYO_COMPANY_ID`) and `g` = the **beta-tester
  list `XbwNdF`** ("toorow Beta testers", `PUBLIC_MAILING_LIST_ID`); `consent.js`
  pushes `generate_lead` on submit. UX caveat: this endpoint does a full-page POST +
  redirect to a Klaviyo-hosted confirmation — upgrade to an AJAX submit (Klaviyo
  Client Subscribe API) later if we want to stay on-page.
- Onsite JS (`klaviyo.js`) is the `Klaviyo — onsite` GTM tag (id 15), **consent-gated
  on `analytics_storage`** — it loads only after opt-in. It is the `__kla_id` source.

## Consent proof — Supabase `public.consent_log` (BUILT 2026-07-20)
GDPR requires **provable** consent (who consented, to what, when). We keep an
append-only ledger.

- Table `public.consent_log` (migration `marketing_consent_log`): `id`, `visitor_id`
  (uuid stored in the `toorow_consent` cookie so a browser's history shares one id),
  `analytics`, `advertising`, `necessary`, `policy_version`, `source`, `page_url`,
  `language`, `timezone`, `user_agent`, `user_id` (FK `auth.users`, null for anon),
  `created_at`.
- **RLS = insert-only.** The publishable key can only `INSERT`; SELECT/UPDATE/DELETE
  are denied, and the insert `WITH CHECK` bounds payload sizes, pins
  `source='marketing_site'` and forbids spoofing `user_id`. Reads/exports go through
  the service role only. Verified: insert → 201, read → 42501, spoofed `user_id` → 401.
- `web/public/consent.js` POSTs a row to `…/rest/v1/consent_log` on every persisted
  choice (accept / reject / save), best-effort (`keepalive`, failures swallowed). The
  Supabase URL, publishable key and `POLICY_VERSION` are constants in that file.
- Bump `POLICY_VERSION` in `consent.js` (and the date in `cookies.astro`) whenever the
  policy materially changes.

### Where users/orgs live (answer to "je sais pas où sont stockés nos users/orgs")
- **Users**: `auth.users` (Supabase Auth) — empty today.
- **Org/tenant model today**: `app.projects` + `app.project_members` +
  `app.tenant_key_audit` (project/tenant-scoped). No standalone `organisations`
  table yet. When accounts exist, set `consent_log.user_id` to link a proof to a
  person, and add org linkage per the org-level access model.

### ⚠️ Pre-existing security debt (NOT caused by this work)
Supabase advisor flags **RLS disabled on 23 `app.*` tables** (`connection_ref`,
`projects`, `project_members`, `audit_log`, …). Anyone with the anon key could read
or modify every row if the `app` schema is REST-exposed. This predates the consent
work and needs a platform decision (enable RLS + add policies). See
`get_advisors('security')`.
