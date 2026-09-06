# toorow — dev tasks.
#
# Works on Linux/macOS (GNU make) and on Windows (make from Git Bash / WSL2, or
# choco/scoop make). Commands use `uv` and `pnpm` directly to avoid venv/shell
# friction. On native PowerShell without make, run the underlying commands shown
# in CONTRIBUTING.md.

# PORT for the MCP server (Cloud Run injects PORT in prod; default 8000 locally).
PORT ?= 8000
export PORT

# The interpreter the dependency-free guards run on. Resolved rather than named:
# the CI `guards` job checks out and runs nothing else, so it has the runner's
# `python3` and no `uv`; Git Bash on Windows has `python`. A guard that needs a
# synced environment is a guard that will be dropped from the fast job.
PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)

.DEFAULT_GOAL := help

.PHONY: context context-check help dev install-server install-ui test lint check-migration-catalog apply-migrations build-widget bundle-check smoke tf-validate check-non-additive-guard check-metric-formula-parity check-mdm-invariants check-canonical-classification check-decomposed-citations check-narrative-no-raw check-core-source-agnostic check-mcp-tool-surface audit-public publish-public retention-apply retention-check

help: ## Show this help
	@echo "toorow targets:"
	@echo ""
	@echo "  TOOLBOX.md  <- ce que le depot sait faire, et ce que chaque outil EXIGE"
	@echo "              (genere : python scripts/toolbox_index.py)"
	@echo ""
	@echo "  make dev            Start the MCP server (streamable HTTP) on 0.0.0.0:$(PORT)"
	@echo "  make install-server Sync Python deps via uv"
	@echo "  make install-ui     Install UI deps via pnpm"
	@echo "  make test           Run server pytest suite"
	@echo "  make lint           Run ruff on server/"
	@echo "  make check-migration-catalog  Validate migration names and order"
	@echo "  make apply-migrations  Apply pending migrations with the ledger runner"
	@echo "  make build-widget   Build the sample single-file widget"
	@echo "  make bundle-check    Run the AD-11 bundle gate on the built widget"
	@echo "  make smoke          Build widget + bundle gate + server import check"
	@echo "  make tf-validate    terraform validate (no apply)"
	@echo "  make audit-public   Audit the public application allow-list (no write)"
	@echo "  make publish-public Sync the public projection into ../toorow-public (no push)"
	@echo "  make retention-check Show image/build-artefact retention (read-only)"
	@echo "  make retention-apply Apply retention so deploy artefacts stop billing"

install-server: ## Resolve + install Python deps into a uv-managed env
	uv sync

install-ui: ## Install UI workspace deps
	pnpm -C ui install

dev: ## Start the FastMCP server over streamable HTTP, bind 0.0.0.0:$PORT
	uv run --package toorow-server python -m core.main

test: ## Run the server test suite (SKIPS the pg-gated files unless a DSN is set)
	@if [ -z "$$TEST_POSTGRES_DSN" ]; then \
	  echo "WARNING: TEST_POSTGRES_DSN is unset."; \
	  echo "  216 test files under server/tests gate themselves on it and will SKIP."; \
	  echo "  A green run here does NOT mean the pg-gated guarantees hold."; \
	  echo "  For the full suite:  make test-full   (see scripts/disposable_postgres.py)"; \
	  echo ""; \
	fi
	uv run pytest server/tests -q

test-full: ## Run the server test suite against a live Postgres (REFUSES without a DSN)
	@if [ -z "$$TEST_POSTGRES_DSN" ]; then \
	  echo "TEST_POSTGRES_DSN is unset -- refusing to report a full run that is not one."; \
	  echo ""; \
	  echo "  python scripts/disposable_postgres.py up"; \
	  echo "  eval \"\$$(python scripts/disposable_postgres.py env)\""; \
	  echo "  make test-full"; \
	  echo ""; \
	  exit 1; \
	fi
	uv run pytest server/tests -q

lint: ## Lint the server package
	uv run ruff check server

check-migration-catalog: ## Validate migration names, unique IDs, and continuity
	uv run python scripts/check_migration_catalog.py


context: ## Regenerer TOUT le contexte derive (index, etats, attentes)
	@python scripts/bmad_index.py
	@python scripts/surface_state.py
	@python scripts/screen_expectations.py
	@python scripts/toolbox_index.py

context-check: ## Verifier que le contexte derive n'est pas perime (ce que le hook Stop lance)
	@python scripts/bmad_index.py --gate
	@python scripts/surface_state.py --gate
	@python scripts/screen_expectations.py --gate
	@python scripts/toolbox_index.py --gate

apply-migrations: ## Apply pending migrations with PLATFORM_DB_URL
	uv run python scripts/apply_migrations.py

build-widget: ## Build the sample widget to a single self-contained HTML file
	pnpm -C ui --filter @toorow/widget-sample build

bundle-check: ## AD-11 gate: fail if the bundle has any external http(s) reference
	node ui/scripts/bundle-check.mjs ui/widgets/sample/dist/index.html

smoke: build-widget bundle-check ## Local smoke: build widget + gate
	uv run python -c "import core.main; print('server import OK:', core.main.mcp.name)"

tf-validate: ## Validate the Terraform (never applies)
	cd infra/terraform && terraform init -backend=false && terraform validate

audit-public: ## Audit the public application allow-list projection (read-only)
	python scripts/export_public_app.py

publish-public: ## Sync the projection into ../toorow-public and show the diff (never pushes; add --push manually)
	python scripts/publish_public_app.py

retention-check: ## Show current image / build-artefact retention (read-only)
	bash infra/scripts/apply_retention.sh --dry-run

retention-apply: ## Apply retention so deploy artefacts stop billing forever
	bash infra/scripts/apply_retention.sh

check-non-additive-guard: ## AD-4 guard: no non-additive metric is summed on its own in mart SQL
	@$(PYTHON) scripts/check_non_additive_guard.py --gate

check-metric-formula-parity: ## Epic 66 guard: a ratio's formula says the same thing in dim_metric.csv and in its mart
	@$(PYTHON) scripts/check_metric_formula_parity.py --gate

check-canonical-classification: ## AI-288 ratchet: a canonical target is classified, or it is refused
	@$(PYTHON) scripts/check_canonical_target_classification.py --gate

check-decomposed-citations: ## AI-250 ratchet: no NEW `path:line` citation into a file whose content moved out
	@$(PYTHON) scripts/check_line_citations.py --ratchet

check-mdm-invariants: ## Epic 66 guard: a common key version is immutable, and one module writes it
	@echo "Checking MDM common key invariants (static)..."
	@if grep -rnE "(UPDATE|DELETE FROM)\s+app\.mdm_common_key_versions" server/core/ 2>/dev/null; then \
		echo "ERROR: a common key version is immutable -- a new version replaces it, never an edit."; \
		echo "       The DB trigger trg_mdm_common_key_versions_immutable (migration 258) refuses it too;"; \
		echo "       code that tries is code that will fail in production instead of at review."; \
		exit 1; \
	fi
	@if [ "$$(grep -rl --include='*.py' 'INSERT INTO app.mdm_common_key_versions' server/core/ | wc -l)" -gt 1 ]; then \
		echo "ERROR: more than one module writes app.mdm_common_key_versions."; \
		echo "       Ordered components + content_hash are the identity: two writers is two identities."; \
		grep -rl --include='*.py' 'INSERT INTO app.mdm_common_key_versions' server/core/; \
		exit 1; \
	fi
	@echo "OK: common key versions are append-only in code, with a single writer."

check-narrative-no-raw: ## AD-1 guard: narrative.py never READS data (no data-access import, no SQL, no raw_/stg_ refs) — the citation token stays allowed (Story 6.4, AC8)
	@$(PYTHON) scripts/check_narrative_no_raw.py --gate

check-core-source-agnostic: ## AD-2 guard: core never imports a module, loads dynamically only in adjudicated seams, and never branches on a module slug
	@$(PYTHON) scripts/check_core_source_agnostic.py --gate

check-mcp-tool-surface: ## MCP catalog growth bound: assembled/wire/bytes stay under the budgets in scripts/mcp_tool_surface_report.py
	@$(PYTHON) scripts/mcp_tool_surface_report.py --gate
