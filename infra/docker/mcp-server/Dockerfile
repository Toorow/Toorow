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

WORKDIR /build

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
ENV UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --package toorow-server --python /usr/local/bin/python3.12

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal image, non-root, exposes $PORT.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Create non-root user.
RUN useradd -m appuser

WORKDIR /app

# Copy the virtual environment from builder (contains all installed deps
# and the toorow-server package registered at its source path).
COPY --from=builder /build/.venv /app/.venv

# Copy server source so that the editable path /build/server (registered in
# the .pth file) is resolved. We copy it to the same relative location to
# match the .pth entry, then rewrite the path via PYTHONPATH.
# Simpler: copy server/ and set PYTHONPATH=/app/server — python -m core.main works.
COPY --from=builder /build/server /app/server

# Runtime DATA, not build inputs. Six core modules resolve these paths from
# `Path(__file__).parents[2]` -- i.e. /app -- and read them while serving:
# country_vocabulary, fx_helper, money, metric_semantics, metrics and
# report_dictionary. Without the seeds the container starts and answers /health
# perfectly, then fails the moment real work happens: creating a project
# returned 503 country_vocabulary_unavailable on the deployed service, which
# blocked the entire journey (an org could be created, a project never could).
# The builder had them all along; only the runtime stage was missing them.
COPY --from=builder /build/dbt/seeds /app/dbt/seeds

# Same class of miss: adaptation_executor spawns infra/sandbox/worker.py at
# parents[2]/infra/sandbox. Copied from the build context (the builder stage
# never received infra/).
COPY infra/sandbox /app/infra/sandbox

# Fix ownership so appuser can read the package.
RUN chown -R appuser:appuser /app

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
