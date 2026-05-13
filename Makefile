.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---- setup ----------------------------------------------------------------
.PHONY: install
install: ## Install dev dependencies
	$(UV) sync --extra dev

.PHONY: lock
lock: ## Refresh uv lockfile
	$(UV) lock

# ---- code quality ---------------------------------------------------------
.PHONY: fmt
fmt: ## Format with ruff
	$(UV) run ruff format src tests

.PHONY: lint
lint: ## Lint with ruff
	$(UV) run ruff check src tests

.PHONY: lint-fix
lint-fix: ## Lint and apply safe fixes
	$(UV) run ruff check --fix src tests

.PHONY: typecheck
typecheck: ## Type-check with mypy
	$(UV) run mypy src

.PHONY: check
check: lint typecheck ## Lint + typecheck

# ---- tests ---------------------------------------------------------------
.PHONY: test
test: ## Unit tests
	$(UV) run pytest -m "not integration"

.PHONY: test-integration
test-integration: ## Integration tests (brings the stack up, always tears it down)
	@trap 'docker compose down -v' EXIT INT TERM; \
	docker compose up -d --wait && \
	POSTGRES_DSN=$${POSTGRES_DSN:-postgresql://bench:bench@localhost:5433/bench} \
		$(UV) run pytest -m integration

.PHONY: test-all
test-all: ## All tests
	$(UV) run pytest

.PHONY: smoke
smoke: ## End-to-end smoke: import every server, validate tool surface, ensure no DB/cluster/HTTP needed
	$(UV) run python -c "from devops_mcp_bundle.postgres.server import mcp as p; from devops_mcp_bundle.k8s.server import mcp as k; from devops_mcp_bundle.observability.server import mcp as o; print('postgres-dba:', p.name); print('k8s-inspector:', k.name); print('observability:', o.name)"
	$(UV) run python -c "from devops_mcp_bundle.postgres.safety import is_read_only_sql; assert is_read_only_sql('SELECT 1'); assert not is_read_only_sql('DROP TABLE x'); print('safety classifier ok')"
	$(UV) run python -c "from devops_mcp_bundle.observability.queries import render_logql, escape_logql_label; assert escape_logql_label('a\"b') == 'a\\\\\"b'; print('logql escape ok')"
	$(UV) run pytest -m "not integration" -q

# ---- containers ----------------------------------------------------------
.PHONY: up
up: ## Bring up local Postgres on :5433 (see docker-compose.yml)
	docker compose up -d --wait

.PHONY: down
down: ## Tear down + remove volumes
	docker compose down -v

# ---- demos ---------------------------------------------------------------
.PHONY: run-postgres
run-postgres: ## Run the Postgres MCP server (stdio)
	$(UV) run mcp-postgres-dba

.PHONY: run-k8s
run-k8s: ## Run the Kubernetes MCP server (stdio)
	$(UV) run mcp-k8s-inspector

.PHONY: run-observability
run-observability: ## Run the Observability MCP server (stdio)
	$(UV) run mcp-observability

# ---- hygiene -------------------------------------------------------------
.PHONY: clean
clean: ## Wipe caches + build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist
	find . -name __pycache__ -type d -exec rm -rf {} +
