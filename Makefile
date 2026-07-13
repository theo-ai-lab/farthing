.PHONY: help verify core lint types test infra worker-agent worker-execute inbox

help:  ## Show this help
	@echo "Farthing — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Start here: 'make verify' proves the money invariant in ~1s with zero installs."

.DEFAULT_GOAL := help

# The zero-dependency invariant proof. Runs anywhere Python 3.13+ exists.
verify:  ## Prove the money invariant (78 tests, zero installs)
	PYTHONPATH=src:tests python3 -m unittest discover -s tests -p 'test_*.py' -v
	python3 scripts/check_no_float_money.py

core: verify  ## Alias for verify

lint:  ## Lint with ruff (needs uv sync)
	uv run ruff check .

types:  ## Strict type-check the money core (needs uv sync)
	uv run pyright

test:  ## Full test suite via pytest (needs uv sync)
	uv run pytest -q

infra:  ## Start Temporal + Postgres locally
	docker compose up -d

worker-agent:  ## Run the agent worker (PREPARE key + LLM)
	uv run farthing-worker --role agent

worker-execute:  ## Run the execute worker (EXECUTE key + signing secret)
	uv run farthing-worker --role execute

inbox:  ## Run the approval inbox
	uv run farthing-inbox
