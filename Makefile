DATE ?= yesterday

.PHONY: install ingest test clean

install:
	pip install -e ".[dev]"

ingest:
	python -m inhouse ingest --date $(DATE)

test:
	pytest -q

clean:
	rm -rf data/ .pytest_cache/ **/__pycache__/
