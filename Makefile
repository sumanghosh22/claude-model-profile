# Developer commands. Run `make help`.
PYTHON ?= python3
RUFF_VERSION := 0.16.1
PY_SOURCES := src/bin/model-profile scripts tests
MYPY_TARGETS := src/bin/model-profile scripts/install.py scripts/build_bundle.py

.PHONY: help check lint typecheck test bundle install uninstall tools

help:            ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

tools:           ## Install the pinned linters (ruff, mypy) with pip
	$(PYTHON) -m pip install "ruff==$(RUFF_VERSION)" "mypy==2.3.0"

lint:            ## Lint and check formatting
	ruff check $(PY_SOURCES)
	ruff format --check $(PY_SOURCES)

typecheck:       ## Strict type check of every Python tool
	@for target in $(MYPY_TARGETS); do mypy --strict $$target || exit 1; done

test:            ## Run the full test suite
	$(PYTHON) -I -m unittest discover -s tests -v

check: lint typecheck test   ## Everything CI runs

bundle:          ## Build the single-file installer in dist/
	$(PYTHON) scripts/build_bundle.py

install:         ## Install into ~/.claude and switch routing on
	./install.sh

uninstall:       ## Switch routing off and remove everything installed
	./install.sh uninstall
