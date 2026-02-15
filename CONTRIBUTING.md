# Contributing to ProspectorAI

Thanks for your interest in contributing! This document covers the setup and workflow.

## Development Setup

```bash
# Clone and create environment
git clone https://github.com/YOUR_USERNAME/prospector-ai.git
cd prospector-ai
conda create -n prospector python=3.12 -y
conda activate prospector

# Install dependencies
pip install -r requirements.txt
pip install pytest

# Install Playwright browser
playwright install chromium
```

## Running Tests

```bash
# All tests
make test

# Or directly
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_models.py -v

# Specific test class
python -m pytest tests/test_database.py::TestSearch -v
```

## Linting

```bash
make lint
```

This verifies all Python modules compile without syntax errors.

## Project Structure

| File | Purpose |
|------|---------|
| `config.py` | Configuration dataclass (search params, rate limits) |
| `models.py` | Pydantic data models (Contractor, Insight, Report) |
| `scraper.py` | Playwright-based web scraping engine |
| `database.py` | SQLite database layer (schema, UPSERT, queries) |
| `insights.py` | OpenAI GPT integration + local analytics |
| `pipeline.py` | End-to-end orchestrator |
| `main.py` | CLI entry point |
| `server.py` | FastAPI REST API |
| `static/dashboard.html` | Interactive browser dashboard |

## Making Changes

1. **Fork** the repo and create a feature branch
2. **Write tests** for new functionality in `tests/`
3. **Run the test suite** to make sure nothing breaks
4. **Update documentation** if you change the API or add features
5. **Submit a pull request** with a clear description

## Running the Pipeline Locally

```bash
# Demo mode (no API key needed)
python main.py pipeline --demo

# Start the dashboard
python main.py serve --port 8000
```

## Code Style

- Type hints on all function signatures
- Docstrings on public classes and functions
- Pydantic models for data validation
- Logging via `logging` module (not print statements)

## Adding New Data Sources

The scraper architecture supports adding new data sources:

1. Create a new scraper class in `scraper.py` (or a new file)
2. Map extracted fields to the `Contractor` model in `models.py`
3. The database layer handles storage automatically via UPSERT

## Questions?

Open an issue on GitHub.
