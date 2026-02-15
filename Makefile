.PHONY: install setup test lint serve pipeline clean help

# Default target
help:
	@echo "ProspectorAI - Development Commands"
	@echo "===================================="
	@echo ""
	@echo "  make install     Install Python dependencies"
	@echo "  make setup       Full setup (install + Playwright browser)"
	@echo "  make test        Run unit tests"
	@echo "  make lint        Run linter checks"
	@echo "  make serve       Start the API server on port 8000"
	@echo "  make pipeline    Run full pipeline in demo mode"
	@echo "  make stats       Show database statistics"
	@echo "  make top         Show top HOT leads"
	@echo "  make clean       Remove generated files"
	@echo ""

# Setup & install
install:
	pip install -r requirements.txt

setup: install
	playwright install chromium
	@echo "Setup complete. Run 'make pipeline' to get started."

# Development
test:
	python -m pytest tests/ -v --tb=short

lint:
	python -m py_compile config.py
	python -m py_compile models.py
	python -m py_compile database.py
	python -m py_compile insights.py
	python -m py_compile pipeline.py
	python -m py_compile scraper.py
	python -m py_compile server.py
	python -m py_compile main.py
	@echo "All modules compile successfully."

# Run
serve:
	python main.py serve --port 8000

pipeline:
	python main.py pipeline --demo

# Query shortcuts
stats:
	python main.py db -a stats

top:
	python main.py db -a top --min-score 7

search-nj:
	python main.py db -a search --state NJ

runs:
	python main.py db -a runs

# Cleanup
clean:
	rm -rf __pycache__
	rm -rf tests/__pycache__
	rm -rf .pytest_cache
	rm -f *.db-shm *.db-wal
	@echo "Cleaned generated files."

clean-all: clean
	rm -f gaf_contractors.db
	rm -rf output/
	@echo "Cleaned all data files."
