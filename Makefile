.DEFAULT_GOAL := help
UV ?= uv

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Start Postgres + API with docker compose (migrates and seeds)
	docker compose up --build

down: ## Stop the compose stack (keep data)
	docker compose down

reset: ## Stop the compose stack and delete the database volume
	docker compose down -v

install: ## Install dependencies into .venv
	$(UV) sync

run: ## Run the API locally with reload (expects DATABASE_URL / .env)
	$(UV) run uvicorn app.main:app --reload --port 8000

migrate: ## Apply migrations
	$(UV) run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add thing"
	$(UV) run alembic revision --autogenerate -m "$(m)"

seed: ## Load reference data (idempotent)
	$(UV) run python -m scripts.seed

lint: ## Ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Auto-format
	$(UV) run ruff format .
	$(UV) run ruff check . --fix

typecheck: ## mypy
	$(UV) run mypy app scripts tests

test: ## Run tests (expects TEST_DATABASE_URL or local Postgres on 5432)
	$(UV) run pytest

check: lint typecheck test ## Everything CI runs

.PHONY: help up down reset install run migrate revision seed lint fmt typecheck test check loadtest loadtest-contention

loadtest: ## Concurrency + throughput check against a running API (URL, DB override-able)
	$(UV) run python -m scripts.loadtest --url $(or $(URL),http://localhost:8000) --requests 300 --concurrency 50 --mixed-cards $(if $(DB),--database-url $(DB),)

loadtest-contention: ## 200 orders race for 5 units; proves no oversell (needs DB)
	$(UV) run python -m scripts.loadtest --url $(or $(URL),http://localhost:8000) --scenario contention --stock 5 --requests 200 --concurrency 100 --database-url $(or $(DB),postgresql+asyncpg://orders:orders@localhost:5432/orders)
