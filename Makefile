.PHONY: install browsers run test clean

install:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -U pip && pip install -e '.[dev]'

browsers:
	. .venv/bin/activate && playwright install --with-deps chromium

run:
	. .venv/bin/activate && python -m jobagent

test:
	.venv/bin/pytest -q

clean:
	rm -rf .pytest_cache __pycache__ src/jobagent/__pycache__ tests/__pycache__
