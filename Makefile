.PHONY: help install dev-install test lint format clean check build run run-sse daemon audit watchdog

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

run-sse:  ## Start MCP server with SSE (port 9020)
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