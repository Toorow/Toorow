# CHANGELOG

All notable changes to toorow are recorded here.

## Convention

- One entry per deploy / tag.
- Tags follow the `vYYYY.MM.DD` scheme (e.g. `v2026.07.12`).
  If multiple deploys occur on the same day, append a counter: `v2026.07.12-2`.
- Every future deploy or tag MUST add a CHANGELOG line before merging.
- Format: `YYYY-MM-DD -- <summary>` followed by bullet points as needed.

---

## 2026-09-04 -- La documentation publiee redit vrai : icone de marque, catalogue d'outils, note de version

- **L'icone servie n'etait pas l'icone de la marque.** `docs/favicon.ico` (et sa
  copie `docs/public/`) portait un nuage etire sur un canevas carre : deux ovales
  verticaux a la place des deux cercles, la base plate rognee. Un binaire pose a
  la main ne peut pas etre distingue d'un binaire juste. `scripts/sync_brand_favicon.py`
  derive desormais l'ICO du jeu canonique `logo/toorow_icon_<n>x<n>.png` sans
  reechantillonner, et le mirroir sur les trois surfaces servies ;
  `--check` est la porte, `server/tests/conformance/test_brand_favicon.py` la joue
  dans la suite (rouge verifie sur l'ancien fichier).
- **Un logo de connecteur manquait sur la vitrine.** La carte « Connect your source
  stack » dessinait ses trois pastilles avec `mask:url(/connectors/<file>)` : un
  masque ne garde que le canal alpha, donc `google-analytics.png`, opaque de bord
  a bord, rendait un carre orange sans marque dedans. Les pastilles rendent un
  vrai `<img>`, comme `ConnectorIdentity.astro` le fait deja. Deux gardes dans
  `web/src/lib/homepage-source.test.ts` : le balayage de la classe, et sa sonde.
- **`docs/agent-tools.mdx` se disait exhaustif et documentait 9 outils sur 131** ;
  trois n'existaient pas (`get_morning_briefing`, `connector_activate`,
  `connector_verify`) et une famille entiere, un `<source>_report` par connecteur,
  n'a jamais ete construite -- `mailgun_report` compris, pour un transport qui
  n'est pas une source. Page reecrite contre le registre du serveur (trois profils,
  54/43/34), et `test_every_tool_a_published_page_names_exists_on_the_server`
  refuse desormais tout appel invente dans l'onglet Documentation.
- **`docs/data-quality.mdx` annoncait 5 moniteurs pour 10** (`DQ_MONITORS`) : les
  cinq nouveaux sont ecrits (arrival timeliness, null rate, zero rows, unresolved
  values, unresolved geography) et deux cles etaient fausses.
- **Nouvelle page `renders-and-sharing.mdx`** : Render, Dossier, Share -- la plus
  grosse surface livree depuis juillet n'etait documentee nulle part. Declaree
  dans `docs.json`.
- **Quatre captures d'ecran retirees des pages publiees** : console francaise,
  navigation retiree (`Modules`, `Connector Admin`), « 4 moniteurs universels ».
  Les fichiers restent sur disque ; il faut des captures anglaises fraiches.
- **`docs/changelog.mdx`** gagne `v1.5.0`, qui couvre 2026-07-26 -> 2026-09-04.
  Aucune entree par deploiement n'a ete tenue sur cet intervalle ; l'histoire fine
  vit dans `git log` et `_bmad-output/implementation-artifacts/story-log.md`, et
  n'a pas ete reconstruite ici -- une note de version inventee ne vaut rien.

---

## 2026-07-12 -- Phase A complete (7 epics / 44 stories), global gap review + fixes batch

- Phase A delivery: Epics 1-7 complete (44 stories), covering MCP server skeleton,
  Nango OAuth, auth layer, admin console, data pipeline, queue, alerting, anomaly
  detection, tracing, report pack, multi-tenancy, per-tenant encryption, and
  cross-project isolation.
- Global gap review applied: ops/deployment fixes (Dockerfile HEALTHCHECK, uv pin,
  deploy workflow CI-gate + prod auth hardening, .env.example auth default,
  infra/README CI table, CONTRIBUTING runbook, CHANGELOG bootstrap).
- See full findings and rationale in:
  `_bmad-output/implementation-artifacts/reviews/review-global-gaps.md`
