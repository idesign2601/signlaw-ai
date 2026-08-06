.DEFAULT_GOAL := help
SHELL := /bin/bash
BACKEND := backend
COMPOSE := docker compose

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Local development -------------------------------------------------------

.PHONY: install
install: ## Create the backend virtualenv and install dev dependencies
	cd $(BACKEND) && python -m venv .venv && \
		.venv/bin/pip install --upgrade pip && \
		.venv/bin/pip install -e ".[dev,local]"

.PHONY: dev
dev: ## Run the API with autoreload against local services
	cd $(BACKEND) && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: worker
worker: ## Run the ingestion worker
	cd $(BACKEND) && .venv/bin/arq app.ingestion.worker.WorkerSettings

# --- Quality -----------------------------------------------------------------

.PHONY: lint
lint: ## Ruff lint + format check
	cd $(BACKEND) && .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests

.PHONY: format
format: ## Autoformat and autofix
	cd $(BACKEND) && .venv/bin/ruff format app tests && .venv/bin/ruff check --fix app tests

.PHONY: typecheck
typecheck: ## mypy strict
	cd $(BACKEND) && .venv/bin/mypy app

.PHONY: test
test: ## Run unit + e2e tests (no external services required)
	cd $(BACKEND) && .venv/bin/pytest tests/unit tests/e2e -v

.PHONY: test-integration
test-integration: ## Run integration tests (requires Postgres with pgvector)
	cd $(BACKEND) && .venv/bin/pytest tests/integration -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	cd $(BACKEND) && .venv/bin/pytest --cov=app --cov-report=term-missing --cov-report=xml

.PHONY: check
check: lint typecheck test ## Run every gate CI runs

.PHONY: verify
verify: ## Full local verification: docker build, migrations, lint, types, tests
	bash scripts/verify.sh

.PHONY: verify-fast
verify-fast: ## Verification without Docker builds or Postgres
	bash scripts/verify.sh --no-docker --no-db

.PHONY: fetch-models
fetch-models: ## Download OCR, embedding and reranker models into data/models
	bash scripts/fetch-models.sh

# --- Local demo (no web API) -------------------------------------------------

.PHONY: health
health: ## Check Postgres, pgvector, embedding model and Ollama
	cd $(BACKEND) && .venv/bin/signlaw health

.PHONY: ingest
ingest: ## Index PDFs: make ingest p=documents/bylaws/burnaby_sign_bylaw.pdf
	@[ -n "$(p)" ] || { echo "usage: make ingest p=documents/bylaws/"; exit 2; }
	# abspath resolves p against the repo root, so the documented relative paths
	# work despite the cd into backend/.
	cd $(BACKEND) && .venv/bin/signlaw ingest "$(abspath $(p))"

.PHONY: ask
ask: ## Ask a question: make ask q="What is the maximum fascia sign area?"
	cd $(BACKEND) && .venv/bin/signlaw ask "$(q)" --trace

.PHONY: eval
eval: ## Run the golden evaluation suite (verified cases only)
	cd $(BACKEND) && .venv/bin/signlaw eval

.PHONY: demo
demo: ## End-to-end smoke test: health, ingest, ask
	@bash scripts/demo.sh

.PHONY: validate
validate: ## Milestone 1: full production validation against the real corpus
	@bash scripts/validate.sh

# --- Database ----------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply migrations to head
	cd $(BACKEND) && .venv/bin/alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add foo"
	cd $(BACKEND) && .venv/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	cd $(BACKEND) && .venv/bin/alembic downgrade -1

# --- Docker ------------------------------------------------------------------

.PHONY: up
up: ## Start the full stack
	$(COMPOSE) up -d --build

.PHONY: up-worker
up-worker: ## Start the stack including the ingestion worker
	$(COMPOSE) --profile worker up -d --build

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete all volumes (DESTROYS DATA)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail API logs
	$(COMPOSE) logs -f api

.PHONY: psql
psql: ## Open a psql shell
	$(COMPOSE) exec postgres psql -U $${DB__USER:-signlaw} -d $${DB__NAME:-signlaw}

# --- Backup / restore --------------------------------------------------------
# Postgres holds the only unrecoverable data: the corpus, the embeddings and the
# retrieval traces. On ephemeral hosting these are not optional.

.PHONY: backup
backup: ## Dump the database to backups/ and verify it
	@bash scripts/backup.sh

.PHONY: restore
restore: ## Restore the newest backup: make restore [force=1]
	@bash scripts/restore.sh --latest $(if $(force),--force,)

.PHONY: restore-file
restore-file: ## Restore a specific dump: make restore-file f=backups/x.dump
	@bash scripts/restore.sh "$(f)" $(if $(force),--force,)
