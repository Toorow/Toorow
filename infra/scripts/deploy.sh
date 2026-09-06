#!/usr/bin/env bash
#
# Deploy toorow to production, by hand. THIS is the deployment path.
#
#   ./infra/scripts/deploy.sh            # backend + front
#   ./infra/scripts/deploy.sh backend    # Cloud Run only
#   ./infra/scripts/deploy.sh front      # Firebase Hosting only
#
# WHY A SCRIPT AND NOT A WORKFLOW. This replaces `.github/workflows/deploy.yml`,
# deleted 2026-08-02. That workflow had NEVER deployed once: its
# `google-github-actions/auth` step fails in 22s because
# GCP_WORKLOAD_IDENTITY_PROVIDER / GCP_SA_EMAIL are not set, and it was gated on
# a CI that has been red on every push since at least 2026-07-25. Every real
# deployment -- 2026-07-27, 2026-08-02 -- was manual. Jean, 2026-08-02: "AUCUN
# deploiement ne passe par github". A file that looks like the official path and
# has never run is a trap; this one is the thing that actually runs.
#
# VALUES COME FROM .env when it exists (gitignored, per-machine), then from the
# defaults below. The deploy-specific keys are documented in .env.example.
#
# LES DEUX SURFACES PUBLIQUES QUE CE SCRIPT NE PUBLIE PAS. `front` designe la
# CONSOLE (`ui/admin` -> app.toorow.com). Les deux autres surfaces publiques ont
# chacune leur geste, et il n'etait ecrit nulle part -- mesure du 2026-09-06 :
# docs.toorow.com servait encore << 37 Built-in Modules >> et Mailgun comme
# connecteur, l'etat d'avant le 2026-08-03, pendant que le depot etait juste
# depuis un mois.
#
#   * La VITRINE (`web/`, Astro -> toorow.com) : `cd web && ./deploy.sh`.
#     Son build LIT Sanity, donc toute correction de copie passe d'abord par
#     `node studio/seed-homepage.mjs` (jeton `editor` requis) -- deployer avant
#     republie l'ancienne phrase.
#   * Les DOCS (`docs/`, Mintlify -> docs.toorow.com) : PAS `git push origin`.
#     Mintlify lit `docs/` sur `main` du depot PUBLIC `github.com/Toorow/Toorow`
#     (tableau de bord Mintlify : org `toorow`, repo `toorow`, branche `main`,
#     << docs.json is in a subdirectory >> = `docs`). `origin` est le monorepo
#     PRIVE `jlalbany/toorow` et Mintlify ne le lit pas. Le public se rafraichit
#     par la projection en liste blanche, jamais par un push du monorepo :
#         python scripts/publish_public_app.py            # stage + diff, n'ecrit rien
#         python scripts/publish_public_app.py --push -m "..."
#     `docs/repository-boundary.mdx` en fait une PORTE HUMAINE : relire le diff
#     et passer un scanner de secrets avant de pousser. Mesure du 2026-09-06 :
#     dernier push public le 2026-07-26, donc docs.toorow.com servait encore
#     << 37 Built-in Modules >> six semaines plus tard, et pousser `origin`
#     n'y change rien (verifie : 2 h apres, page inchangee).
#
set -euo pipefail
cd "$(dirname "$0")/../.."

# .env is READ, never sourced. `. ./.env` executes it: a value containing a `$`
# or a backtick is expanded, and under `set -u` an unset name aborts the deploy
# before it starts (measured 2026-08-02 -- line 89 of the live .env does exactly
# that). A deploy script must not depend on a per-machine file being valid shell.
envval() {
  local key="$1" default="${2-}" line
  line="$(grep -m1 -E "^[[:space:]]*${key}[[:space:]]*=" .env 2>/dev/null || true)"
  line="${line#*=}"
  line="${line%%$'\r'*}"          # CRLF: this repo is edited on Windows
  line="${line%%[[:space:]]#*}"   # trailing ` # comment`
  line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                                    -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
  printf '%s' "${line:-$default}"
}

PROJECT="$(envval GCP_PROJECT toorow)"
REGION="$(envval GCP_REGION europe-west1)"
SERVICE="$(envval CLOUD_RUN_SERVICE mcp-server)"
AR_REPO="$(envval ARTIFACT_REGISTRY_REPO connector)"
HOSTING_TARGET="$(envval FIREBASE_HOSTING_TARGET app)"
TASKS_QUEUE="$(envval CLOUD_TASKS_QUEUE_NAME toorow-work)"
FACTS_TOPIC="$(envval PUBSUB_TOPIC "projects/${PROJECT}/topics/toorow-facts")"

# Le registre des migrations dit-il la meme chose que le catalogue ? (AI-241)
#
# `apply_migrations.py --verify-complete` existe depuis toujours et RIEN ne
# l'appelait : deux passages a la main dans SESSIONS.md, aucun automatique. Le
# 2026-08-08 le registre s'est retrouve a 227 alors que 228 et 229 etaient dans
# la base -- applique par une autre voie, qui n'ecrit pas `toorow_meta`. Il s'est
# referme parce qu'une session a lance le bon outil, pas parce qu'un mecanisme
# l'a vu.
#
# ET LE TROU QUE CHERCHE `_validate_ledger` NE POUVAIT PAS L'ATTRAPER : il liste
# les manquants SOUS le maximum enregistre, donc une migration posee AU-DESSUS
# lui est invisible. `--verify-complete` la voit, parce qu'il part du catalogue.
#
# Non bloquant SANS DSN, bloquant AVEC. Ce script tourne sur des postes qui n'ont
# pas forcement acces a la base ; refuser de deployer faute de DSN
# transformerait un garde en obstacle, et un obstacle finit contourne.
verify_migration_ledger() {
  local dsn="${PLATFORM_DB_URL:-$(envval PLATFORM_DB_URL '')}"
  if [ -z "$dsn" ]; then
    echo "==> ledger des migrations NON VERIFIE (pas de PLATFORM_DB_URL ici)"
    return 0
  fi
  echo "==> ledger des migrations : catalogue == registre ?"
  python scripts/apply_migrations.py --dsn "$dsn" --verify-complete
}

# LE REGISTRE DE RENDU LIVRE ENTRE DANS LE LEDGER AU DEPLOIEMENT (2026-08-25).
#
# `app.renderer_runtime_builds` est ce que les epingles `renderer_build` et
# `runtime_build` d'un Render designent : sans ligne, l'epingle nomme un build que
# le deploiement ne peut pas dire avoir livre, et le Render est refuse.
# `scripts/register_renderer_builds.py` projette le manifeste emis depuis le
# registre code (`ui/cards/shell/src/viz/rendererBuilds.generated.json`) -- il
# marchait, et RIEN ne l'appelait : mesure le 2026-08-24, `grep -rn
# register_renderer_builds infra/ .github/ Makefile` ne rendait qu'un commentaire.
# Le 2026-08-13 le ledger tenait UNE ligne pour HUIT familles declarees, et il s'est
# referme parce qu'une session a lance l'outil a la main. C'est ce pas-la.
#
# LE MANIFESTE PROJETE EST CELUI DE L'IMAGE. `assert_build_context_is_the_commit_we_tag`
# tourne juste avant et couvre `ui/cards/shell` : l'arbre y est propre et egal a
# HEAD, donc le manifeste ecrit dans le ledger est exactement celui que l'image
# embarque. Dans l'autre ordre, on enregistrerait l'identite d'un build qui n'est
# pas celui qu'on expedie.
#
# LE `--check` BLOQUE, IL N'AVERTIT PAS. Un `MISSING` est une famille livree que
# personne ne peut epingler -- exactement le defaut du 2026-08-13 -- et un
# `UNDECLARED` est une ligne du ledger nommant une famille ou un renderer que ce
# build ne declare pas. Les deux sont les moities ratifiees de
# `visualization-and-rendering.md:319-322`. Deployer par-dessus, c'est livrer un
# rendu dont les Renders seront refuses sans que rien ne le dise.
#
# Non bloquant SANS DSN, bloquant AVEC -- meme raison que le ledger des migrations
# au-dessus : ce script tourne sur des postes qui n'ont pas forcement acces a la
# base, et un garde plus large que son objet finit contourne.
project_renderer_builds() {
  local dsn="${PLATFORM_DB_URL:-$(envval PLATFORM_DB_URL '')}" sha="$1"
  if [ -z "$dsn" ]; then
    echo "==> ledger des builds de rendu NON PROJETE (pas de PLATFORM_DB_URL ici)"
    return 0
  fi
  echo "==> ledger des builds de rendu : projection du registre livre"
  if ! python scripts/register_renderer_builds.py --dsn "$dsn" --git-sha "$sha"; then
    echo "FAIL: la projection du registre de rendu a refuse. Refus de deployer."
    echo "      Geste qui repare : lire les lignes UNDECLARED ci-dessus -- une ligne"
    echo "      du ledger nomme une famille ou un renderer que ce build ne declare"
    echo "      plus. Rien ne se supprime (un Render peut l'epingler) : soit le"
    echo "      renderer revient dans ui/cards/shell/src/viz/renderers/index.ts,"
    echo "      soit la ligne est actee comme retiree du produit."
    exit 1
  fi
  echo "==> ledger des builds de rendu : verification"
  if ! python scripts/register_renderer_builds.py --dsn "$dsn" --git-sha "$sha" --check; then
    echo "FAIL: le ledger des builds de rendu ne correspond pas au registre livre."
    echo "      Un MISSING est une famille que ce build dessine et qu'aucun Render"
    echo "      ne pourra epingler ; un UNDECLARED est une ligne que le code ne"
    echo "      declare plus. Refus de deployer."
    echo "      Geste qui repare : regenerer les deux fichiers generes d'un seul"
    echo "      geste -- pnpm --filter @toorow/card-shell generate:build-identity --"
    echo "      committer le manifeste, puis relancer."
    exit 1
  fi
}

# LE CATALOGUE DE CHART TEMPLATES LIVRE ENTRE DANS SA TABLE AU DEPLOIEMENT (72.4).
#
# Meme patron, meme raison, meme fichier que le ledger des builds de rendu
# ci-dessus : le CODE est le catalogue
# (`server/core/visualization_template_seeds.py`), la table en est la projection.
# `CLAUDE.md` -- « un catalogue livre avec le produit ne se lit pas dans une
# table » -- et l'invariant ratifie de l'epic 72 en donne la consequence : une
# table qu'un connecteur pourrait ecrire serait de la metadonnee de presentation
# executable, et AD-2 l'interdit.
#
# APRES `assert_build_context_is_the_commit_we_tag`, et c'est necessaire :
# `server/` est dans IMAGE_PATHS, donc l'arbre y est propre et egal a HEAD quand
# ce pas tourne. On projette exactement le catalogue que l'image embarque. Dans
# l'autre ordre, la table decrirait des gabarits qui ne sont pas ceux qu'on
# expedie.
#
# ADDITIF, JAMAIS REECRIT. Une version deja presente est laissee telle quelle :
# un Report peut l'epingler (migration 333 a donne a `presentation_version_id` la
# cle etrangere qu'il n'avait pas), et la ligne est de toute facon insert-once par
# trigger. Une declaration qui a bouge AJOUTE une version n+1 avec son
# predecesseur ; les anciennes restent et s'affichent en HISTORY.
#
# LE `--check` BLOQUE, IL N'AVERTIT PAS. Un MISSING est un gabarit que ce build
# declare et qu'aucun Projet ne porte -- donc une lentille Templates vide alors
# que le produit en livre ; un UNDECLARED est une tete `platform_seed` que le code
# ne declare plus. Aucun des deux ne se repare en supprimant.
#
# Non bloquant SANS DSN, bloquant AVEC -- meme regle que les deux pas au-dessus.
project_template_seeds() {
  local dsn="${PLATFORM_DB_URL:-$(envval PLATFORM_DB_URL '')}"
  if [ -z "$dsn" ]; then
    echo "==> catalogue de Chart Templates NON PROJETE (pas de PLATFORM_DB_URL ici)"
    return 0
  fi
  echo "==> catalogue de Chart Templates : projection du catalogue livre"
  if ! python scripts/register_visualization_template_seeds.py --dsn "$dsn"; then
    echo "FAIL: la projection du catalogue de Chart Templates a refuse. Refus de deployer."
    echo "      Geste qui repare : lire les lignes ci-dessus. Un UNDECLARED nomme une"
    echo "      tete platform_seed que ce build ne declare plus : rien ne se supprime"
    echo "      (un Report peut l'epingler), soit le gabarit revient dans"
    echo "      server/core/visualization_template_seeds.py, soit son retrait est acte."
    exit 1
  fi
  echo "==> catalogue de Chart Templates : verification"
  if ! python scripts/register_visualization_template_seeds.py --dsn "$dsn" --check; then
    echo "FAIL: la table des Chart Templates ne correspond pas au catalogue livre."
    echo "      Un MISSING est un gabarit livre qu'aucun Projet ne porte ; un UNDECLARED"
    echo "      est une tete que le code ne declare plus. Refus de deployer."
    exit 1
  fi
}

# L'IMAGE PORTE UN SHA DE COMMIT ET DOIT LE MERITER (2026-08-17).
#
# `gcloud builds submit … .` televerse le REPERTOIRE DE TRAVAIL, pas HEAD. Le tag
# de l'image, lui, est `git rev-parse HEAD`. Les deux ne coincident que si l'arbre
# est propre sur les chemins qui entrent dans l'image -- sinon on publie du code
# absent du commit dont l'image porte le nom, et le tag MENT : plus rien ne permet
# de dire quel code tourne en production.
#
# Ce depot est travaille par plusieurs sessions en parallele. Mesure le
# 2026-08-17 : 22 fichiers de `server/core`, `server/inbound` et `ui/cards/shell`
# portaient des modifications NON COMMITEES d'autres sessions. Un deploiement a ce
# moment-la aurait expedie en production du travail que personne n'avait ni relu ni
# validé, sous un tag qui ne le contient pas.
#
# LA PORTEE EST CELLE DU Dockerfile, pas l'arbre entier. Un brouillon dans
# `_bmad-output/` ou un `.tsx` de la console n'entre pas dans l'image du backend et
# ne doit donc pas bloquer son deploiement : un garde plus large que son objet
# devient un obstacle, et un obstacle finit contourne (meme raison que le DSN
# ci-dessus). Les chemins ci-dessous sont exactement les `COPY` de
# `infra/docker/mcp-server/Dockerfile` ; s'il en gagne un, il rejoint cette liste.
#
# NON SUIVI COMPTE AUSSI. Un fichier non suivi est televerse comme les autres et
# peut masquer un module ; il est par construction absent du commit.
IMAGE_PATHS=(
  server
  dbt
  pyproject.toml
  uv.lock
  ui/tokens
  ui/scripts
  ui/shell
  ui/cards/shell
  ui/admin/src/styles/theme.css
  infra/sandbox
)

assert_build_context_is_the_commit_we_tag() {
  local dirty untracked
  dirty="$(git diff --name-only HEAD -- "${IMAGE_PATHS[@]}" 2>/dev/null || true)"
  untracked="$(git ls-files --others --exclude-standard -- "${IMAGE_PATHS[@]}" 2>/dev/null || true)"
  if [ -z "$dirty" ] && [ -z "$untracked" ]; then
    echo "==> contexte de build == HEAD sur les chemins de l'image"
    return 0
  fi
  echo "FAIL: le contexte de build ne correspond PAS au commit qui taguerait l'image."
  echo "      L'image s'appellerait mcp-server:$(git rev-parse HEAD) et contiendrait"
  echo "      du code absent de ce commit. Refus."
  [ -n "$dirty" ] && { echo "      modifies et non commites :"; printf '        %s\n' $dirty; }
  [ -n "$untracked" ] && { echo "      non suivis :"; printf '        %s\n' $untracked; }
  echo "      Geste qui repare : commiter ces fichiers (ou demander a la session qui"
  echo "      les tient de le faire), puis relancer. Ne PAS deployer par-dessus le"
  echo "      travail non commite d'une autre session."
  exit 1
}

deploy_backend() {
  local sha image url
  sha="$(git rev-parse HEAD)"
  image="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${SERVICE}:${sha}"

  assert_build_context_is_the_commit_we_tag
  verify_migration_ledger
  project_renderer_builds "$sha"
  project_template_seeds

  # The `connector` Artifact Registry repository (created 2026-07-26) carries
  # cleanup policies: keep the 2 newest, drop untagged after 1 day, drop anything
  # after 7 days. A repository WITHOUT a cleanup policy accrues storage cost
  # silently and forever -- if you ever point this at a new one, add it to
  # infra/scripts/apply_retention.sh in the same change.
  #
  # `make retention-apply` (infra/scripts/apply_retention.sh) is what SETS them,
  # and it is the only thing that does. infra/terraform/main.tf declares the same
  # three policies on a `google_artifact_registry_repository.connector`, but that
  # block lives in `google_project.dev` = `toorow-dev` -- a project that does not
  # exist (measured 2026-08-10: `gcloud projects list` knows `toorow` and nothing
  # else), and infra/terraform/ holds no state. Editing the Terraform and
  # expecting the live repository to change is a no-op; edit both, apply the
  # script.
  echo "==> build ${image}"
  gcloud builds submit --project "$PROJECT" \
    --config infra/docker/mcp-server/cloudbuild.yaml \
    --substitutions="_IMG=${image}" .

  # THE OIDC AUDIENCE IS DERIVED, NOT WRITTEN DOWN. Cloud Tasks mints its token
  # for the service's CANONICAL url (`status.url`), and _google_oidc_caller_is_ours
  # verifies the token against INTERNAL_OIDC_AUDIENCE / CLOUD_TASKS_WORKER_URL.
  # A Cloud Run service answers on TWO hostnames -- the classic
  # `<svc>-<hash>-<reg>.a.run.app` and the newer `<svc>-<projectnum>.<reg>.run.app`
  # -- and only one of them is what the token carries. Hardcoding the wrong one
  # 401s every task while every dashboard stays green. Measured 2026-08-02: that
  # is exactly what happened, revision 00034, until 00035 aligned them.
  url="$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
          --region "$REGION" --format='value(status.url)')"
  echo "==> canonical service url: ${url}"

  # THE TASK'S OIDC IDENTITY, derived like the url above and for the same reason:
  # a value written down here drifts from the service. Cloud Tasks mints the id
  # token AS this account and the worker verifies it against INTERNAL_OIDC_AUDIENCE.
  # Without it `_create_push_task` fails closed -- which is the point: before
  # 2026-08-07 it created tasks carrying NO credential, the worker answered 401 to
  # every one, and the queue reported RUNNING while no push job had ever executed.
  runtime_sa="$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
                 --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')"
  [ -n "$runtime_sa" ] || { echo "FAIL: no runtime service account on ${SERVICE}"; exit 1; }
  echo "==> task identity: ${runtime_sa}"

  # --update-env-vars and --update-SECRETS. NEVER the --set-* variants: each
  # REPLACES its whole mapping, and this service carries ~25 variables and EIGHT
  # secret bindings set out-of-band and absent from this file (PLATFORM_DB_URL,
  # TOOROW_STATIC_TOKEN, TOOROW_PROVISION_TOKEN, GOOGLE_OAUTH_CLIENT_SECRET,
  # GOOGLE_OAUTH_STATE_SECRET, NANGO_SECRET_KEY, TOOROW_INVITATION_PEPPER,
  # INTERNAL_ENDPOINTS_REQUIRE_HEADER). `--set-env-vars` wiped 13 variables off
  # the CRM service on 2026-07-25; `--set-secrets` sat in the old workflow ready
  # to do the same to seven secrets, starting with the database URL.
  #
  # NOT SET HERE, deliberately:
  #   ALLOWED_HOST / FASTMCP_HTTP_ALLOWED_HOSTS / HOST_HEADER_VALIDATION -- the
  #     live ALLOWED_HOST is FOUR hosts and the Cloud Scheduler jobs target the
  #     *.run.app one. Overwriting it with a single hostname and turning strict
  #     validation on 400s every clock.
  #   PORT -- Cloud Run reserves it and injects it.
  #   TOOROW_JWKS_URI / _ISSUER / _AUDIENCE -- live and correct; asserted below
  #     rather than rewritten, so a typo here cannot disable OAuth enforcement.
  #   TOOROW_DEPLOYMENT_MODE -- absent means hosted, which is what this is.
  #     self_hosted would make the instance single-tenant and refuse every new org.
  #   PLATFORM_CLOCK_* -- the clock -> Scheduler job mapping belongs to
  #     `app.platform_clocks`, not to an env var (arbitration Jean, 2026-08-02).
  #
  # QUEUE_BACKEND=cloud_tasks is what makes the three daemon threads stand down.
  # They cannot work here anyway: at --min-instances=0 Cloud Run allocates no CPU
  # between requests, which is why nothing had ever run nightly.
  #
  # NOTHING IS ARMED HERE, AND THAT IS THE POINT (2026-08-08).
  # A fresh install has zero Datastreams, and every nightly step is DATA-DRIVEN:
  # the alert evaluator counts rows there are none of, the briefing selects the
  # projects with an enabled connection and finds none, the notebook step selects
  # the scheduled notebooks and finds none. With nothing configured they read an
  # empty set and do nothing. So their guards defaulted to "false" for no reason
  # anyone still holds -- they were written when the steps were half-built -- and
  # a deployment that had to remember to switch six of them on was six chances to
  # forget. Measured that day: the service carried 36 variables, not one of them a
  # step guard, so six of eleven steps had never run.
  #
  # The repair is in the code, not here: those defaults are now "true", like
  # MEDIAPLAN_ALERTS_ENABLED always was. An off-switch stays for whoever wants to
  # silence a step; nobody has to arm one.
  #
  # WHAT REMAINS BELOW IS ONLY WHAT MUST BE OFF, each for a stated reason --
  # `tests/core/test_nightly_step_guards.py` fails if a step defaults to "false"
  # and is NOT named here, because that is the silence coming back:
  #   TOOROW_CACHE_ENABLED -- the cache is DuckDB on the container filesystem, and
  #     at --min-instances=0 that filesystem does not survive to the next request.
  #     Rebuilding it nightly writes a snapshot nobody can read.
  #   SCHEMA_CONTEXT_ENABLED -- it profiles EVERY relation of the warehouse. The
  #     scan cost is unbounded on a 10 EUR/month budget, and its source is the
  #     same ephemeral DuckDB.
  #
  # --memory=2Gi -- THE PRICE OF THE DECISION BELOW, MEASURED THE FIRST NIGHT IT
  # WAS ARMED (2026-08-23, 00:00:58Z): `Memory limit of 512 MiB exceeded with
  # 515 MiB used`, and the container was terminated MID-NIGHTLY. The dbt step had
  # just logged its two first lines (`dbt_per_project_no_raw_data`, then
  # `dbt_per_project_excludes … count=106`) and the whole march died there: no
  # alerts, no scheduled DQ, no notebooks, no briefings, and NO meta-alert --
  # a killed container writes nothing. Arming a step had silently taken the rest
  # of the nightly with it, and the only trace was a platform-level ERROR nobody
  # was reading.
  #
  # 512 MiB was never a decision: `--memory` was absent from this script, so the
  # service carried Cloud Run's default. `execution-substrate.md` decided that
  # dbt runs INSIDE the served image; the memory that decision costs was never
  # written anywhere. This line is that cost, stated.
  #
  # WHY 2Gi AND NOT 1Gi. Measured, not guessed, on the exact serving digest: `dbt
  # parse` of this project (106 models excluded, ~160 total) runs to `exit(0)` in
  # 38 s in a container of its OWN 512 MiB -- so dbt is not pathological, the SUM
  # is: the server's resident set plus a dbt subprocess does not fit in 512. A
  # real `dbt run` is heavier than a parse, and the next data point costs a whole
  # night: 1Gi would be a second guess with the same price. At `--min-instances=0`
  # this service is billed per request-second, so the memory ceiling is paid for
  # the seconds the nightly actually runs and for nothing else.
  #
  # DBT_NIGHTLY_ENABLED=true -- ARMED HERE, and it has to be here rather than by
  # hand (AI-166, 2026-08-22). Its code default stays "false" because a
  # self-hosted image is not required to carry dbt; THIS deployment's image is,
  # since 2026-08-17 (the Dockerfile syncs the `toorow-dbt` workspace member and
  # copies the models, macros, `dbt_project.yml` and the runtime profile into the
  # runtime stage), and every revision since `mcp-server-00183` is built from it.
  # WHY IT MOVED OUT OF THE OFF-LIST ABOVE: the flag WAS armed by hand on
  # 2026-08-18 on revision 00190 -- and `mcp-server-00191`, deployed seven hours
  # later by this very script, set it back to "false", because this line said
  # false. Measured 2026-08-22 on the four revisions: 00183 true, 00190 true,
  # 00191 false, 00192 false, and the serving 00193 false. Four nightlies ran
  # with the step disabled and NOTHING said so -- `step=dbt_per_project
  # duration_ms=0` is what a disabled step looks like in the logs of that image.
  # An arming a deploy erases is not an arming; it is a countdown.
  #
  # TOOROW_LOG_LEVEL -- without it, `logging.lastResort` handles core.* at
  # WARNING and every INFO the scheduler emits is dropped before stdout. That is
  # what made "did the nightly run?" unanswerable from the logs.
  # TOOROW_DB_MODE was set in NO deployment file, so it took its default --
  # `duckdb`, which on Cloud Run writes into a container destroyed between
  # requests. `execution-substrate.md` named it as the second of three reasons a
  # perfect substrate still evaporates its data, and the measurement agreed: on
  # 2026-08-08, after a Datastream had reached `active` with a published
  # execution and two landed rows, BigQuery held NO raw dataset for the project
  # at all. The rows were real and the warehouse had no table for them. After the
  # flip, `raw_proj_01KZ7ANYMN89GFPEZ05KTVEDRT` carries the landing relation with
  # its two rows.
  #
  # `load` and not a streaming transport: LOAD jobs are NOT billed for ingestion,
  # which is what keeps this deployment inside its 10 EUR/month budget. Streaming
  # is billed per byte and buys seconds of latency nothing here needs yet.
  # TENANT_KEY_BACKEND=secret_manager (AI-278). The per-tenant keys that seal
  # every Google authorization and every alert-destination secret CANNOT live on
  # this container's filesystem: it is ephemeral, so the key died with the
  # instance and the next request minted a replacement, leaving the stored
  # credential permanently unreadable while the error blamed the ciphertext.
  # Secret Manager is the only store here that outlives a revision.
  # TOOROW_FEEDBACK_CONTEXT_SECRET is a STARTUP requirement, not an option:
  # `feedback_context_secret()` fails closed for every authenticated mode, so a
  # container without it exits(1) before it can listen on PORT and the whole
  # revision is refused -- which is exactly how epic 65 landed in the repository
  # and never reached production. It is asserted below with the others: an
  # env-var contract the deploy path does not carry is a deploy path that stops
  # working the day the code starts needing it.
  echo "==> deploy ${SERVICE}"
  gcloud run deploy "$SERVICE" \
    --project "$PROJECT" --region "$REGION" --platform managed \
    --image "$image" \
    --allow-unauthenticated --min-instances=0 --max-instances=4 \
    --memory=2Gi \
    --update-secrets "INTERNAL_ENDPOINTS_REQUIRE_HEADER=toorow-internal-auth:latest,TOOROW_FEEDBACK_CONTEXT_SECRET=toorow-feedback-context-secret:latest,TOOROW_RENDER_SHARE_PEPPER=toorow-render-share-pepper:latest" \
    --update-env-vars "TENANT_KEY_BACKEND=secret_manager,TOOROW_AUTH_MODE=oauth,TOOROW_BQ_PROVISION_ENABLED=1,TOOROW_DB_MODE=bigquery,TOOROW_BQ_WRITE_MODE=load,GOOGLE_CLOUD_PROJECT=${PROJECT},QUEUE_BACKEND=cloud_tasks,CLOUD_TASKS_PROJECT=${PROJECT},CLOUD_TASKS_LOCATION=${REGION},CLOUD_TASKS_QUEUE_NAME=${TASKS_QUEUE},CLOUD_TASKS_WORKER_URL=${url},INTERNAL_OIDC_AUDIENCE=${url},CLOUD_TASKS_OIDC_SERVICE_ACCOUNT=${runtime_sa},INTERNAL_OIDC_SERVICE_ACCOUNT=${runtime_sa},PUBSUB_TOPIC=${FACTS_TOPIC},QUEUE_RECONCILE_GRACE_SECONDS=300,TOOROW_LOG_LEVEL=INFO,DBT_NIGHTLY_ENABLED=true,TOOROW_CACHE_ENABLED=false,SCHEMA_CONTEXT_ENABLED=false,TOOROW_RENDER_SHARE_ORIGIN=https://app.toorow.com" \
    --quiet

  # A missing JWT value would silently disable OAuth enforcement. Assert, do not set.
  #
  # LE DRAPEAU D'IDENTITE CANONIQUE A QUITTE CETTE LISTE LE 2026-08-24 (67-17),
  # apres n'y avoir passe qu'un jour. Il y etait entre le 2026-08-23 parce que son
  # defaut de code etait `0` : son absence n'etait pas un reglage manquant, c'etait
  # l'identite canonique desarmee en silence. Ce garde etait juste, et il est
  # devenu sans objet le jour ou la BRANCHE a ete retiree : le comportement
  # canonique est desormais le seul, aucun code ne lit plus la variable, et
  # refuser un deploiement sur l'absence d'une variable que rien ne lit
  # apprendrait au prochain lecteur qu'elle compte encore.
  #
  # Ce que le garde protegeait est protege ailleurs, et mieux :
  # `server/tests/conformance/test_no_identity_flag_returns.py` rougit si un
  # branchement sur ce drapeau revient. Un garde qui interdit la CAUSE bat un
  # garde qui verifie un reglage.
  #
  # La variable reste posee a `1` sur le service `mcp-server` ; elle est
  # inoffensive et sera retiree au prochain passage sur les variables du service.
  local missing=""
  for k in TOOROW_JWKS_URI TOOROW_JWT_ISSUER TOOROW_JWT_AUDIENCE PLATFORM_DB_URL \
           TENANT_KEY_BACKEND TOOROW_FEEDBACK_CONTEXT_SECRET; do
    gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
      --format="value(spec.template.spec.containers[0].env.list())" | grep -q "$k" \
      || missing="$missing $k"
  done
  [ -n "$missing" ] && { echo "FAIL: variables absentes du service:$missing"; exit 1; }
  echo "==> backend OK"
}

# AI-149. The console served to users sat NINE DAYS behind the repository, and
# that single fact was the whole of "tous les ecrans sont morts": every screen
# looked broken because every screen was the old one. Nothing here noticed --
# `firebase deploy` exits 0 whether it uploaded this build, an older `dist/`
# someone left behind, or nothing at all.
#
# The guard is the only question worth asking after a front deploy: IS THE THING
# BEING SERVED THE THING I JUST BUILT? It is answered by comparing the entry
# bundle Vite hashed into `dist/index.html` with the one the live site's
# `index.html` names. A content hash is exactly the right instrument: it changes
# when the code changes and only then, so this cannot go green on a stale build.
#
# It runs against the live host over the public internet, which is also what
# makes it honest -- it reads what a person's browser reads, not what the CLI
# reported. `index.html` itself is served `no-cache` by Firebase (only
# `/assets/**` is immutable), so the fetch cannot be answered from an edge copy
# of the previous deploy.
assert_console_is_the_build_we_made() {
  local site url served built
  site="$(envval FIREBASE_HOSTING_SITE toorow-app)"
  url="https://${site}.web.app/index.html"

  built="$(grep -oE 'src="/assets/index-[^"]+\.js"' ui/admin/dist/index.html | head -1)"
  if [ -z "$built" ]; then
    echo "FAIL: ui/admin/dist/index.html names no hashed entry bundle -- the build did not produce one."
    exit 1
  fi

  # Firebase publishes the new version a moment after the CLI returns, and the
  # CDN keeps the previous index.html for longer than that: 2026-08-30 the new
  # bundle was live 30 s after a probe that had given up at 15 s and failed the
  # deploy for it (rc=1 on a deploy that had succeeded). Up to a minute, with a
  # cache-busting query so the probe reads the edge, not its cache.
  local attempt
  for attempt in 1 2 3 4 5 6; do
    served="$(curl -fsSL --max-time 20 -H 'Cache-Control: no-cache' "${url}?probe=$(date +%s)" 2>/dev/null \
              | grep -oE 'src="/assets/index-[^"]+\.js"' | head -1 || true)"
    [ "$served" = "$built" ] && break
    [ "$attempt" -lt 6 ] && sleep 10
  done

  if [ -z "$served" ]; then
    echo "FAIL: ${url} served no hashed entry bundle. Either the site is not reachable"
    echo "      or Hosting is serving something that is not this console."
    exit 1
  fi
  if [ "$served" != "$built" ]; then
    echo "FAIL: the console being served is NOT the build that was just made."
    echo "      built  : ${built}"
    echo "      served : ${served}   (${url})"
    echo "      This is AI-149. Do not walk away from a deploy in this state:"
    echo "      every screen a person opens is the old one, and nothing else says so."
    exit 1
  fi
  echo "==> console freshness OK: ${served} is live at ${url}"
}

deploy_front() {
  # The console is served at the ROOT of app.toorow.com by Firebase Hosting, which
  # also rewrites /api/** to this same Cloud Run service (ui/admin/firebase.json).
  # THE TOKENS FIRST, and they are not optional. `ui/tokens/dist/theme.ts` is
  # generated and gitignored, and `ui/cards/shell/src/vizTheme.ts` imports it --
  # so a checkout that has never run this step fails the build with
  # UNRESOLVED_IMPORT on a path no file in git will ever provide. A developer
  # machine that generated it once never sees this; a clean tree always does.
  echo "==> build tokens"
  (cd ui && pnpm install --frozen-lockfile && pnpm build:tokens)
  echo "==> build front"
  (cd ui/admin && pnpm build)
  echo "==> deploy hosting:${HOSTING_TARGET}"
  (cd ui/admin && firebase deploy --only "hosting:${HOSTING_TARGET}" --project "$PROJECT")
  assert_console_is_the_build_we_made
  echo "==> front OK"
}

case "${1:-all}" in
  backend) deploy_backend ;;
  front)   deploy_front ;;
  all)     deploy_backend; deploy_front ;;
  # AI-149, the reflex half: ASK THE HASH BEFORE READING THE CODE. When a screen
  # looks broken, this is the first command, not a code search -- it deploys
  # nothing and answers in seconds whether the console being served is even the
  # one in this working tree. Nine days were spent on the other order.
  check)   assert_console_is_the_build_we_made ;;
  *) echo "usage: $0 [all|backend|front|check]"; exit 2 ;;
esac

# One-time GCP resources this depends on (created 2026-08-01, not per-deploy):
#   gcloud services enable cloudtasks|cloudscheduler|pubsub .googleapis.com
#   gcloud tasks queues create toorow-work --location=europe-west1 \
#       --max-attempts=5 --max-concurrent-dispatches=8
#   gcloud pubsub topics create toorow-facts
#   gcloud secrets create toorow-internal-auth
#     ^ ONE PLATFORM secret: it authenticates the platform calling itself
#       (Scheduler/Tasks -> this service). Nothing per-user lives there.
# Plus the seven Cloud Scheduler jobs, each with --headers "X-Internal-Auth=<secret>":
#   toorow-dispatch-nightly   0 2 * * *     toorow-dispatch-hourly   0 * * * *
#   toorow-reconcile-queues   */10 * * * *  toorow-reconcile-clocks  17 * * * *
#   toorow-run-dq-monitors    */15 * * * *  toorow-poll-health       0 6 * * *
#   toorow-drain-outbox       */5 * * * *
