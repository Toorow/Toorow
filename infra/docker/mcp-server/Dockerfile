# toorow — mcp-server container image.
#
# Build context: repo root  (docker build -f infra/docker/mcp-server/Dockerfile .)
# Two-stage build: builder installs deps via uv; runtime is minimal python:3.12-slim.
#
# Story 2.1 AC1.
# See infra/docker/mcp-server/README.md for build + smoke-test commands.

# ---------------------------------------------------------------------------
# Stage 1: builder — install Python dependencies from the locked workspace.
# ---------------------------------------------------------------------------
FROM python:3.12 AS builder

# Install uv (fast Python package/project manager).
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

# /app, NOT /build — and the name is load-bearing (AI-166, 2026-08-18, measured
# on the first real nightly): uv writes every console script with an ABSOLUTE
# shebang pointing at this venv's python. Built under /build and copied to
# /app/.venv, `/app/.venv/bin/dbt` still began `#!/build/.venv/bin/python` —
# an interpreter that does not exist in the runtime stage — so exec'ing it
# raised the kernel's ENOENT, surfaced as `No such file or directory: 'dbt'`.
# The server never noticed because `python -m core.main` execs `python` (a
# symlink that survives), never a console script; dbt was the first script this
# image ever exec'd. Building at the SAME absolute path the runtime uses fixes
# every console script at once, not just dbt.
WORKDIR /app

# Copy workspace definition and lockfile first (maximise layer cache).
COPY pyproject.toml uv.lock ./

# Copy workspace members needed for the server package.
COPY server/ ./server/
COPY dbt/ ./dbt/

# Install all workspace dependencies and the toorow-server package into
# a project virtual environment.
# --frozen: abort if uv.lock is stale.  --no-dev: exclude dev/test extras.
# UV_PYTHON_DOWNLOADS=never: use the image's /usr/local/bin/python3.12 — a
# uv-managed interpreter would leave the venv's python symlink dangling in the
# runtime stage (same path exists in python:3.12-slim), silently falling back
# to the system python WITHOUT the venv packages.
# --package toorow-server: the workspace ROOT declares no dependencies
# (package = false) — syncing without a package selector installs nothing.
#
# THEN THE SECOND SYNC, and it is why nothing ever built a mart (AI-166,
# 2026-08-17). `--package toorow-server` installs that member's dependencies and
# ONLY those, so dbt-core / dbt-bigquery — declared by the `toorow-dbt` workspace
# member — never entered the image. `scheduler.run_dbt` shells out to a `dbt`
# binary the container did not carry, and `deploy.sh` states that absence as the
# reason `DBT_NIGHTLY_ENABLED` defaults to false. The decision recorded in
# `docs/product-architecture/execution-substrate.md` is that dbt runs INSIDE the
# served image: the orchestration, the per-project isolation, the meta-alerts and
# the nightly trigger already live in this service, so a separate Cloud Run Job
# either duplicates them or ships this same image twice.
#
# --inexact is load-bearing: without it the second sync REMOVES every package the
# first installed (uv makes the environment match the selected package exactly),
# and the image would carry dbt and no server. Measured 2026-08-17 on a scratch
# environment: the two lines together yield dbt-core 1.11.12 + dbt-bigquery
# 1.11.3 with fastmcp and google-cloud-bigquery still importable.
ENV UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --package toorow-server --python /usr/local/bin/python3.12 \
    && uv sync --frozen --no-dev --package toorow-dbt --inexact \
       --python /usr/local/bin/python3.12

# Build the exact MCP App bytes and the companion pin manifest.
FROM node:22-bookworm-slim AS ui-builder

RUN corepack enable
WORKDIR /build/ui

COPY ui/package.json ui/pnpm-lock.yaml ui/pnpm-workspace.yaml ./
COPY ui/shell/package.json ./shell/package.json
COPY ui/admin/package.json ./admin/package.json
COPY ui/cards/shell/package.json ./cards/shell/package.json

RUN corepack pnpm --version | grep '^9\.' \
    && corepack pnpm install --frozen-lockfile

COPY ui/tokens ./tokens
COPY ui/scripts ./scripts
COPY ui/shell ./shell
COPY ui/cards/shell ./cards/shell
COPY ui/admin/src/styles/theme.css ./admin/src/styles/theme.css

RUN corepack pnpm build:tokens \
    && corepack pnpm --filter @toorow/card-shell build \
    && node scripts/bundle-check.mjs cards/shell/dist/viz/mcp-app.html --max-bytes 1500000

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal image, non-root, exposes $PORT.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Create non-root user.
RUN useradd -m appuser

WORKDIR /app

# Copy the virtual environment from builder (contains all installed deps
# and the toorow-server package registered at its source path).
COPY --chown=appuser:appuser --from=builder /app/.venv /app/.venv

# Copy server source so that the editable path /app/server (registered in
# the .pth file) is resolved. We copy it to the same relative location to
# match the .pth entry, then rewrite the path via PYTHONPATH.
# Simpler: copy server/ and set PYTHONPATH=/app/server — python -m core.main works.
COPY --chown=appuser:appuser --from=builder /app/server /app/server

# Runtime DATA, not build inputs. Six core modules resolve these paths from
# `Path(__file__).parents[2]` -- i.e. /app -- and read them while serving:
# country_vocabulary, fx_helper, money, metric_semantics, metrics and
# report_dictionary. Without the seeds the container starts and answers /health
# perfectly, then fails the moment real work happens: creating a project
# returned 503 country_vocabulary_unavailable on the deployed service, which
# blocked the entire journey (an org could be created, a project never could).
# The builder had them all along; only the runtime stage was missing them.
COPY --chown=appuser:appuser --from=builder /app/dbt/seeds /app/dbt/seeds

# The REST of the dbt project — same class of miss as the seeds above, found the
# same way (AI-166, 2026-08-17). `scheduler.run_dbt` passes `--project-dir` at
# `Path(__file__).parents[2]/dbt`, i.e. /app/dbt, and dbt refuses to start without
# `dbt_project.yml`; the models and macros are what it would build. The module
# staging models live under /app/server/modules/<name>/dbt and are already present
# via the server copy above — `dbt_project.yml` reaches them by relative
# model-paths, which is why the two trees must keep this exact layout.
COPY --chown=appuser:appuser --from=builder /app/dbt/dbt_project.yml /app/dbt/dbt_project.yml
COPY --chown=appuser:appuser --from=builder /app/dbt/models /app/dbt/models
COPY --chown=appuser:appuser --from=builder /app/dbt/macros /app/dbt/macros
COPY --chown=appuser:appuser --from=builder /app/dbt/tests /app/dbt/tests

# The profile, under the name dbt looks for. The real `dbt/profiles/profiles.yml`
# is gitignored (it is generated by the local DuckDB loop and would point the
# service at a developer's machine), so the served one is a distinct committed
# file renamed here. It carries no secret: the BigQuery target reads the GCP
# project from the environment and authenticates as the runtime service account.
COPY --chown=appuser:appuser --from=builder /app/dbt/profiles/runtime-profiles.yml \
     /app/dbt/profiles/profiles.yml

# `render_analyze_result` is registered only when these exact files exist.
COPY --chown=appuser:appuser --from=ui-builder /build/ui/cards/shell/dist/viz /app/ui/cards/shell/dist/viz

# Same class of miss: adaptation_executor spawns infra/sandbox/worker.py at
# parents[2]/infra/sandbox. Copied from the build context (the builder stage
# never received infra/).
COPY --chown=appuser:appuser infra/sandbox /app/infra/sandbox

# dbt writes its compiled SQL, its manifest and its log file while it runs. The
# image layer is read-only for appuser in practice and /app/dbt's parent dirs are
# created by COPY, so both are pointed at the writable tmpfs instead — a build
# that cannot write its target/ aborts before it opens a single connection.
ENV DBT_TARGET_PATH=/tmp/dbt-target \
    DBT_LOG_PATH=/tmp/dbt-logs
RUN chown -R appuser:appuser /app/dbt

USER appuser

# Activate the virtualenv.
ENV PATH="/app/.venv/bin:$PATH"

# Add server/ to PYTHONPATH so `python -m core.main` resolves the `core` package.
ENV PYTHONPATH="/app/server"

# PORT is injected by Cloud Run (default 8080 on Cloud Run; 8000 for local dev).
# The main() function reads os.environ.get("PORT", "8000").
ENV PORT=8000

EXPOSE $PORT

# Health check: poll the REST health proxy (GET /api/health, Story 5.2 AC4).
# Uses python urllib so curl need not be present in the slim image.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request, sys; urllib.request.urlopen('http://localhost:' + __import__('os').environ.get('PORT','8000') + '/api/health').read() or sys.exit(1)"

# Run the MCP server.
CMD ["python", "-m", "core.main"]
