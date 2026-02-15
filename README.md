# ProspectorAI

**AI-Powered B2B Sales Intelligence Platform for Contractor Lead Generation**

ProspectorAI is an end-to-end pipeline that scrapes public contractor directories, stores structured + unstructured data in a queryable database, generates AI-powered sales intelligence per contractor, and serves everything through a REST API with an interactive dashboard.

One command. 71 contractors. 71 actionable sales insights. ~90 seconds.

```
python main.py pipeline --demo
```

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green)
![SQLite](https://img.shields.io/badge/SQLite-WAL_Mode-orange)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o--mini-purple)
![Playwright](https://img.shields.io/badge/Playwright-Stealth-red)

---

## What It Does

| Stage | What Happens | Technology |
|-------|-------------|------------|
| **Scrape** | Collects contractor data from a JS-rendered SPA protected by bot detection | Playwright + Stealth + Coveo API Interception |
| **Store** | Persists structured + unstructured data with UPSERT merge semantics | SQLite with WAL mode, normalized schema |
| **Enrich** | Generates per-contractor sales intelligence and market analysis | OpenAI GPT-4o-mini (or demo heuristics) |
| **Serve** | REST API with search, filtering, and an interactive visual dashboard | FastAPI + Chart.js dashboard |

---

## Architecture

```
                      python main.py pipeline --demo
                                 │
                                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │                    PIPELINE ORCHESTRATOR                      │
 │                                                              │
 │   Stage 1          Stage 2         Stage 3         Stage 4   │
 │  ┌────────┐    ┌───────────┐    ┌──────────┐    ┌─────────┐ │
 │  │ SCRAPE │───>│   STORE   │───>│  ENRICH  │───>│  SERVE  │ │
 │  │        │    │  SQLite   │    │  GPT AI  │    │ FastAPI │ │
 │  └────────┘    └───────────┘    └──────────┘    └─────────┘ │
 │       │              │               │               │       │
 │       ▼              ▼               ▼               ▼       │
 │   Playwright     WAL mode        Structured       /docs      │
 │   + Stealth      + UPSERT       JSON output     /dashboard   │
 │   + Coveo API    + COALESCE     + batching      /contractors │
 └──────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites

```bash
# Create environment (conda/micromamba)
conda create -n prospector python=3.12 -y
conda activate prospector

# Install dependencies
pip install -r requirements.txt

# Install browser for Playwright
playwright install chromium
```

### Run the Full Pipeline (Demo Mode — No API Key Needed)

```bash
python main.py pipeline --demo
```

This will:
1. Scrape 71 contractors from the directory (~90 seconds)
2. Store them in `gaf_contractors.db`
3. Generate sales intelligence using rule-based heuristics
4. Export JSON + CSV files to `output/`

### Start the API Server + Dashboard

```bash
python main.py serve --port 8000
```

Then open:
- **Dashboard**: http://localhost:8000/dashboard
- **Swagger Docs**: http://localhost:8000/docs
- **API Root**: http://localhost:8000/

### With Real AI Insights (OpenAI)

```bash
export OPENAI_API_KEY=sk-your-key-here
python main.py pipeline
```

### Query the Database

```bash
python main.py db -a stats              # dashboard statistics
python main.py db -a top --min-score 8  # HOT leads
python main.py db -a search --state NJ  # filter by state
python main.py db -a runs               # audit trail
```

---

## Dashboard

The interactive browser dashboard lets sales teams explore insights visually:

| Feature | Description |
|---------|------------|
| **Stats Bar** | Live KPIs: total contractors, insight coverage, avg score, HOT leads, avg rating |
| **Compare Tool** | Pick any two contractors — radar + bar charts showing why one ranks above the other |
| **Bubble Chart** | Interactive scatter: X = score, Y = rating, bubble size = reviews, color = HOT/WARM/COLD |
| **Score Distribution** | Bar chart of contractors per score bucket |
| **Geographic View** | Horizontal bar chart — contractor density by city |
| **Ranking Table** | All contractors, sortable by any column, filterable by state / score / rating |
| **Detail Panel** | Click any row — full insight: summary, talking points, pain points, signals, actions |
| **Market Intel** | Full market overview: clusters, top performers, opportunities, competitive landscape |

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/stats` | GET | Dashboard statistics |
| `/contractors` | GET | Search with filters: `?state=NJ&min_rating=4.5&min_score=7` |
| `/contractors/{id}` | GET | Single contractor + certifications + full sales insight |
| `/insights/market` | GET | Latest market intelligence report |
| `/insights/top` | GET | Top leads by sales readiness score |
| `/pipeline/run` | POST | Trigger pipeline in background (returns `run_id` immediately) |
| `/pipeline/runs` | GET | List all pipeline runs with status |
| `/pipeline/runs/{id}` | GET | Detail for a specific run |
| `/docs` | GET | Interactive Swagger UI |

---

## AI Insights Generated Per Contractor

| Field | Example |
|-------|---------|
| **Executive Summary** | "Jersey Roofing LLC has a perfect 5.0 rating from 434 reviews with President's Club certification..." |
| **Sales Readiness Score** | 10/10 (HOT) |
| **Talking Points** | "Your 5.0 rating across 434 reviews is outstanding — above the market average of 4.53" |
| **Pain Points** | "Standing out where every competitor also holds Master Elite status" |
| **Competitive Position** | "Leader in Garfield by review volume" |
| **Opportunity Signals** | "High volume signals established operations and growth capacity" |
| **Recommended Actions** | "Call (201) 982-2718 — lead with their President's Club status" |
| **Business Size** | Small / Mid-size / Micro |
| **Industry Tags** | Residential Roofing, Multi-Service Exterior |

---

## Database Schema

```
┌──────────────────┐     ┌─────────────────────┐
│   contractors     │     │  certifications      │
│                   │     │                     │
│  contractor_id PK │◄────│  contractor_id FK    │
│  name, city, state│     │  name, type         │
│  rating (REAL)    │     └─────────────────────┘
│  description (TEXT│ ← unstructured
│  photos (JSON)    │ ← semi-structured
│                   │     ┌─────────────────────┐
│                   │◄────│ contractor_insights  │
└──────────────────┘     │                     │
                          │  summary     (TEXT)  │ ← AI-generated
┌──────────────────┐     │  talking_pts (JSON)  │ ← semi-structured
│  pipeline_runs    │     │  score    (INTEGER)  │ ← structured
│  (audit trail)    │     │  rationale   (TEXT)  │ ← AI-generated
└──────────────────┘     └─────────────────────┘
                          ┌─────────────────────┐
                          │  market_insights     │
                          │  market_summary(TEXT)│ ← AI-generated
                          │  cert_dist   (JSON) │
                          │  avg_rating  (REAL) │
                          └─────────────────────┘
```

### Storage Strategy

| Data Type | Approach | Example |
|-----------|----------|---------|
| Identity / contact | Typed columns + indexes | `name TEXT`, `rating REAL` |
| Lists | JSON array in TEXT | `pain_points: '["challenge 1", ...]'` |
| Nested objects | JSON array of dicts in TEXT | `talking_points: '[{"hook": "...", "detail": "..."}]'` |
| Free-form AI text | Plain TEXT column | `summary`, `competitive_position` |
| Relationships | Normalized table + FK | `certifications` table |

---

## Technical Decisions

| Decision | Rationale |
|----------|-----------|
| **Playwright over requests** | Target site uses JS rendering + Akamai CDN bot protection — HTTP clients get 403 |
| **API interception over HTML parsing** | Coveo search API returns structured JSON — more reliable, complete, and faster than DOM parsing |
| **playwright-stealth** | Patches `navigator.webdriver` and other automation fingerprints to evade bot detection |
| **SQLite over PostgreSQL** | Zero-config, single-file portability; WAL mode handles concurrent reads; easy upgrade path |
| **UPSERT + COALESCE** | Re-running pipeline merges data — never duplicates, never overwrites with nulls |
| **Pydantic models** | Type safety, validation, and automatic JSON/CSV serialization across the pipeline |
| **FastAPI over Flask** | Native async/await, automatic Swagger docs, type-checked endpoints |
| **Batched GPT calls (15/batch)** | Fits context window while giving the model comparative market context |
| **response_format: json_object** | Enforces valid JSON output from GPT — reliable structured parsing |
| **Demo mode** | Full pipeline demo without API key — zero-friction evaluation |

---

## Project Structure

```
prospector-ai/
├── main.py           CLI entry point — scrape, enrich, pipeline, serve, db
├── pipeline.py       Orchestrator — chains scrape → store → enrich → store
├── scraper.py        Playwright + stealth + Coveo API interception + pagination
├── insights.py       OpenAI integration — prompts, batching, structured output
├── database.py       SQLite — schema, UPSERT, search, WAL mode, thread safety
├── server.py         FastAPI REST API — concurrent access, background pipeline
├── models.py         Pydantic models — Contractor, Insight, Report
├── config.py         Configuration dataclass — search params, rate limits
├── static/
│   └── dashboard.html   Interactive sales intelligence dashboard
├── requirements.txt  Dependencies
├── .env.example      Environment variable template
├── gaf_contractors.db   SQLite database (auto-created by pipeline)
└── output/           Exported JSON and CSV files
```

---

## Sample Results (ZIP 10013, 25-mile radius)

| Metric | Value |
|--------|-------|
| Total Contractors | 71 |
| Avg Rating | 4.53 stars |
| Avg Reviews | 135 |
| States Covered | NJ (48), NY (23) |
| HOT Leads (score >= 8) | 10 |
| Pipeline Runtime | ~90 seconds |

### Top HOT Leads

| Contractor | Rating | Reviews | Score | Location |
|------------|--------|---------|-------|----------|
| Jersey Roofing LLC | 5.0 | 434 | 10/10 | Garfield, NJ |
| Matute Roofing | 5.0 | 452 | 9/10 | Wayne, NJ |
| Complete Roof Systems | 5.0 | 362 | 9/10 | Dumont, NJ |
| AK Gatsios Inc | 5.0 | 316 | 9/10 | Bronx, NY |
| Brothers Aluminum | 4.9 | 358 | 9/10 | Valley Stream, NY |

---

## License

MIT
