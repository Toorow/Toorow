# toorow — dev tasks.
#
# Works on Linux/macOS (GNU make) and on Windows (make from Git Bash / WSL2, or
# choco/scoop make). Commands use `uv` and `pnpm` directly to avoid venv/shell
# friction. On native PowerShell without make, run the underlying commands shown
# in CONTRIBUTING.md.

# PORT for the MCP server (Cloud Run injects PORT in prod; default 8000 locally).
PORT ?= 8000
export PORT

.DEFAULT_GOAL := help

.PHONY: help dev install-server install-ui test lint build-widget bundle-check smoke tf-validate check-non-additive-guard check-narrative-no-raw audit-public publish-public

help: ## Show this help
	@echo "toorow targets:"
	@echo "  make dev            Start the MCP server (streamable HTTP) on 0.0.0.0:$(PORT)"
	@echo "  make install-server Sync Python deps via uv"
	@echo "  make install-ui     Install UI deps via pnpm"
	@echo "  make test           Run server pytest suite"
	@echo "  make lint           Run ruff on server/"
	@echo "  make build-widget   Build the sample single-file widget"
	@echo "  make bundle-check    Run the AD-11 bundle gate on the built widget"
	@echo "  make smoke          Build widget + bundle gate + server import check"
	@echo "  make tf-validate    terraform validate (no apply)"
	@echo "  make audit-public   Audit the public application allow-list (no write)"
	@echo "  make publish-public Sync the public projection into ../toorow-public (no push)"

install-server: ## Resolve + install Python deps into a uv-managed env
	uv sync

install-ui: ## Install UI workspace deps
	pnpm -C ui install

dev: ## Start the FastMCP server over streamable HTTP, bind 0.0.0.0:$PORT
	uv run --package toorow-server python -m core.main

test: ## Run the server test suite
	uv run pytest server/tests -q

lint: ## Lint the server package
	uv run ruff check server

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

check-non-additive-guard: ## AD-4 guard: fail if SUM(average_position) appears in mart SQL (non-comment lines)
	@echo "Checking for naive SUM(average_position) in mart SQL (non-comment lines)..."
	@if grep -rP "^[^-].*SUM\(average_position\)" dbt/models/marts/ 2>/dev/null; then \
		echo "ERROR: average_position must NEVER be summed in marts. Use semantic_avg_position view."; \
		exit 1; \
	fi
	@echo "OK: no SUM(average_position) in non-comment SQL lines in dbt/models/marts/"

check-narrative-no-raw: ## AD-1 guard: narrative.py must not import warehouse or reference raw tables (Story 6.4, AC8)
	@echo "Checking narrative.py for AD-1 violations (no warehouse import, no raw table refs)..."
	@if grep -E "^(import|from)\s+(server\.core\.warehouse|core\.warehouse|warehouse)" server/core/narrative.py; then \
		echo "ERROR: narrative.py must not import warehouse. AD-1 violation."; \
		exit 1; \
	fi
	@if grep -nE "fact_daily_kpi|raw_gsc|raw_meta|raw_ga4|\.rows\b" server/core/narrative.py; then \
		echo "ERROR: narrative.py must not reference raw table names. AD-1 violation."; \
		exit 1; \
	fi
	@echo "OK: narrative.py has no warehouse import and no raw table references."
