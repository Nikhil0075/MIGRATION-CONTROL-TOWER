# Reproducibility entrypoints (Day 10 hardening, Phase 6). Every target
# just wraps a command already documented in README.md — this file adds
# nothing new, it makes the existing ones one word to type and one place
# to look. Run from the repo root.
#
# Requires GNU Make + bash (Git Bash on Windows covers both). If `make`
# isn't available, every recipe below is a single line you can paste
# directly from this file.

.PHONY: setup seed test test-one run harness assess second-estate onboarding-gate teardown clean-clone-check help

help:
	@echo "Targets:"
	@echo "  make setup              - create venv, install deps, scaffold .env"
	@echo "  make seed               - seed the Agent Registry and the estate registry (idempotent)"
	@echo "  make test               - pytest tests/ -v"
	@echo "  make test-one NAME=...  - pytest tests/ -k NAME -v"
	@echo "  make run                - agents/orchestrator/run_full_migration.py (the flagship demo)"
	@echo "  make harness            - evaluation/run_harness.py (all 14 Appendix D scenarios)"
	@echo "  make assess PACK=...    - agents/discovery/run_assessment.py (default: wwi_sqlserver_v1)"
	@echo "  make teardown           - infrastructure/teardown.sh (asks to confirm; use ARGS=--dry-run to preview)"
	@echo "  make second-estate      - start the PostgreSQL fixture (the §32.11 onboarding proof)"
	@echo "  make onboarding-gate    - the release gate: a second estate with zero agent edits"
	@echo "  make clean-clone-check  - verifies README's setup steps work from an empty directory"

setup:
	python -m venv .venv
	@echo "Activate it, then run:"
	@echo "  pip install -r requirements.txt"
	@echo "  cp .env.example .env   # fill in real values"

# Idempotent. Nothing can resolve an agent capability until the Agent
# Registry is seeded, and nothing can resolve a source connection until an
# estate exists — seed_estates.py skips (rather than reverts) any estate
# since edited in the console; pass ARGS=--force to replace those too.
seed:
	python infrastructure/seed_registry.py
	python infrastructure/seed_finance_agent.py
	python infrastructure/seed_estates.py $(ARGS)

test:
	pytest tests/ -v

test-one:
	pytest tests/ -k "$(NAME)" -v

run:
	python agents/orchestrator/run_full_migration.py

harness:
	python evaluation/run_harness.py

assess:
	python agents/discovery/run_assessment.py packs/$(or $(PACK),wwi_sqlserver_v1)/pack.yaml

# The §32.11 proof: a second estate, a different database engine, onboarded
# by adding one adapter + one pack + one estate YAML + one registry line.
second-estate:
	cd simulator/source_setup/postgres && docker compose up -d

onboarding-gate:
	pytest tests/test_clean_estate_onboarding.py -v

teardown:
	bash infrastructure/teardown.sh $(ARGS)

clean-clone-check:
	bash scripts/clean_clone_check.sh
