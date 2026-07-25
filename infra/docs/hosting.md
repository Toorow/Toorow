# Hosting — how the toorow surfaces are deployed (maintainers)

The showcase site and the application are **separated** — different runtimes,
different deploy pipelines, different cost profiles.

| Surface | What | Runtime | Idle cost |
| ------- | ---- | ------- | --------- |
| **Vitrine** `toorow.com` | `web/` Astro **static** site | **Firebase Hosting** | **€0** — serverless CDN, no instances |
| **App / API** `app.toorow.com` | `mcp-server` (FastMCP) | **Cloud Run** (europe-west1) | **€0** — `--min-instances=0` (scale to zero) |
| **Docs** `docs.toorow.com` | Mintlify | Mintlify hosting | n/a |

All three are subdomains of `toorow.com`, so GA4 (`_ga`) and the consent cookie
(`toorow_consent`, `Domain=.toorow.com`) are shared — one funnel, asked once.

## Scale to zero (no idle instances)
Cloud Run defaults to `min-instances=0`, but `.github/workflows/deploy.yml` now sets
it **explicitly** on both dev and prod, plus `--max-instances=4` as a cost guard:

```
gcloud run deploy mcp-server … --min-instances=0 --max-instances=4 …
```

Trade-off: min=0 means a cold start on the first request after idle. Acceptable for
an occasionally-hit MCP server; it guarantees €0 when nobody is using it. Raise
`--min-instances` only if cold-start latency becomes a real problem.

Firebase Hosting has **no instance concept** at all — it is a static CDN, so the
vitrine costs nothing when idle by nature (only egress/storage, within the free tier).

## Deploy the vitrine (Firebase Hosting)
Config lives in `web/firebase.json` (serves `web/dist`) + `web/.firebaserc`
(project `toorow`).

**Deploy = a script you run** (`web/deploy.sh`), not CI. Chosen over a GitHub Actions
workflow on purpose: it is simpler, gives full control over *when* you ship, and reuses
your local `web/.env` (so no GitHub Variables and no "commit the generated files" dance).

```
cd web
firebase login        # once — your Google account (project owner)
./deploy.sh           # builds Astro → dist, then firebase deploy --only hosting --project toorow
```

`deploy.sh` fails fast if `dist/consent.js` is missing (the GTM "Consent — Banner" tag
loads `toorow.com/consent.js`, so it must live on the vitrine origin).

> A keyless CI path is already wired in GCP if we ever want it: WIF pool `github-pool`
> + provider `github-provider` (repo `jlalbany/toorow`) + SA
> `github-web-deploy@toorow.iam.gserviceaccount.com` (`roles/firebasehosting.admin`).
> Unused by the script; drop a workflow in later to use it.

### Custom domain
Add `toorow.com` (and `www.toorow.com`) in the Firebase console → Hosting → custom
domain. Pick the apex `toorow.com` as canonical and 301 `www` → apex (or vice-versa);
keep `PUBLIC_ANALYTICS_HOSTS` in `web/.env` aligned with the hostnames you serve.

## Deploy the app (Cloud Run)
Unchanged: `.github/workflows/deploy.yml` builds the image, pushes to Artifact
Registry, deploys to Cloud Run (dev → prod behind the GitHub `production` gate).

## Not decided yet
- Whether `app.toorow.com` (the console UI) is a separate Firebase Hosting site or is
  served by Cloud Run — TBD when the console frontend exists.
- Custom domain apex vs www canonical (set in the Firebase console).
