PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
# Pass the project-local .env (gitignored) to the app if it exists.
ENVFILE := $(if $(wildcard .env),--env-file .env,)

.PHONY: install run demo test clean

$(BIN)/bbtui: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e '.[dev]'
	@touch $@

install: $(BIN)/bbtui

run: install
	$(BIN)/bbtui $(ENVFILE) $(ARGS)

demo: install
	$(BIN)/bbtui --demo $(ARGS)

test: install
	$(BIN)/python -m pytest -q $(ARGS)

clean:
	rm -rf $(VENV) .pytest_cache build dist *.egg-info
