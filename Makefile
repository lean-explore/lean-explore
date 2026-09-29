.PHONY: help install lint format typecheck check-sizes test test-fast test-integration test-external test-all clean

help:
	@echo "LeanExplore Development Commands"
	@echo "================================="
	@echo "make install          Install package in editable mode with dev dependencies"
	@echo "make lint             Run ruff linter"
	@echo "make format           Run ruff formatter"
	@echo "make typecheck        Run mypy on the package"
	@echo "make check-sizes      Report files > 500 lines and functions > 40 code lines"
	@echo "make test             Run tests with coverage (excludes slow, integration, external)"
	@echo "make test-fast        Run only fast tests (excludes slow, integration, external)"
	@echo "make test-integration Run only integration tests"
	@echo "make test-external    Run only external tests"
	@echo "make test-all         Run all tests including slow, integration, and external"
	@echo "make clean            Remove cache and build artifacts"

install:
	pip install -e ".[dev]"
	pre-commit install
	cp CONTRIBUTING.md AGENTS.md
	cp CONTRIBUTING.md CLAUDE.md

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy src --ignore-missing-imports

check-sizes:
	python scripts/check_sizes.py

test:
	pytest --cov=lean_explore --cov-report=term-missing --cov-report=html -v \
		-m "not slow and not integration and not external"

test-fast:
	pytest -v -m "not slow and not integration and not external"

test-integration:
	pytest -v -m "integration"

test-external:
	pytest -v -m "external"

test-all:
	pytest --cov=lean_explore --cov-report=term-missing --cov-report=html -v

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
