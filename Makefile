PYTHON ?= python

.PHONY: setup format format-check lint compile typecheck test map map-check quality

setup:
	$(PYTHON) -m pip install -r requirements-dev.txt

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check .

lint:
	$(PYTHON) -m ruff check .

compile:
	$(PYTHON) -m compileall -q opportunity_scout.py bountyscout tests scripts

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m coverage run --branch -m unittest -v
	$(PYTHON) -m coverage report

map:
	$(PYTHON) scripts/generate_codebase_map.py

map-check:
	$(PYTHON) scripts/generate_codebase_map.py --check

quality: format-check lint compile map-check typecheck test
