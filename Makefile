.PHONY: help install dev-install test lint format clean check docs-contract build run run-http run-sse daemon audit watchdog

PYTHON ?= python3
PREVIOUS_CONTRACT ?= docs/standards/history/union-six-pr-contract-2026-08-11.3.json
BASE_REVISION ?=
SOURCE_REVISION ?=

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install core dependencies
	pip install -r requirements.txt

dev-install:  ## Install with dev dependencies
	pip install -e ".[dev]"

rust-build:  ## Build Rust core engine (requires Rust toolchain)
	cd rust/context-engine-core && pip install maturin && maturin develop

test:  ## Run tests with coverage
	pytest -n auto --cov=plastic_promise --cov-report=term-missing

test-fast:  ## Run tests without coverage (faster)
	pytest -n auto -q

lint:  ## Lint with ruff
	ruff check plastic_promise/

format:  ## Format with ruff
	ruff format plastic_promise/
	ruff check --fix plastic_promise/

check:  ## Full check: lint + type-check
	ruff check plastic_promise/
	mypy plastic_promise/ --ignore-missing-imports

docs-contract:  ## Verify the authoritative union six-PR contract and generated views
	$(PYTHON) scripts/render_union_six_pr_contract.py
	@if [ -n "$(BASE_REVISION)" ] || [ -n "$(SOURCE_REVISION)" ]; then \
		if [ -z "$(BASE_REVISION)" ] || [ -z "$(SOURCE_REVISION)" ]; then \
			echo "BASE_REVISION and SOURCE_REVISION must be supplied together" >&2; \
			exit 1; \
		fi; \
		$(PYTHON) scripts/verify_union_six_pr_contract.py --repo-root . \
			--base-revision "$(BASE_REVISION)" --source-revision "$(SOURCE_REVISION)"; \
	elif [ -n "$(PREVIOUS_CONTRACT)" ]; then \
		$(PYTHON) scripts/verify_union_six_pr_contract.py --repo-root . --previous-contract "$(PREVIOUS_CONTRACT)"; \
	else \
		$(PYTHON) scripts/verify_union_six_pr_contract.py --repo-root .; \
	fi
	$(PYTHON) -m pytest -q --no-cov tests/test_union_six_pr_contract.py

clean:  ## Remove build artifacts and caches
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf dist/ build/ 2>/dev/null || true
	@echo "Clean complete."

build: clean  ## Build distribution packages
	python -m build

run:  ## Start MCP server (stdio mode)
	python -m plastic_promise

run-http:  ## Start MCP server with Streamable HTTP (port 9020)
	python -m plastic_promise --streamable-http 9020

run-sse:  ## Start MCP server with legacy SSE alias (port 9020)
	python -m plastic_promise --sse 9020

pre-commit-install:  ## Install pre-commit hooks
	pre-commit install

pre-commit-run:  ## Run pre-commit on all files
	pre-commit run --all-files

daemon:  ## Start pi_daemon (autonomous pipeline)
	python daemons/pi_daemon.py

audit:  ## Run audit daemon once
	python daemons/audit_daemon.py

watchdog:  ## Start watchdog process monitor (Windows)
	powershell -File daemons/watchdog.ps1
