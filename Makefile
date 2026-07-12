.PHONY: verify core lint types test infra worker-agent worker-execute inbox

# The zero-dependency invariant proof. Runs anywhere Python 3.13+ exists.
verify core:
	PYTHONPATH=src:tests python3 -m unittest discover -s tests -p 'test_*.py' -v
	python3 scripts/check_no_float_money.py

lint:
	uv run ruff check .

types:
	uv run pyright

test:
	uv run pytest -q

infra:
	docker compose up -d

worker-agent:
	uv run farthing-worker --role agent

worker-execute:
	uv run farthing-worker --role execute

inbox:
	uv run farthing-inbox
