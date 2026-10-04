PYTHON ?= python

.PHONY: setup format format-check lint compile typecheck test quality

setup:
	$(PYTHON) -m pip install -r requirements-dev.txt

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check .

lint:
	$(PYTHON) -m ruff check .

compile:
	$(PYTHON) -m compileall -q opportunity_scout.py opportunity_scout tests

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m coverage run --branch -m unittest -v
	$(PYTHON) -m coverage report

quality: format-check lint compile typecheck test
