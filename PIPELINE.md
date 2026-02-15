# GAF Contractor Intelligence Pipeline: How It Works

This document explains, step by step, how the system retrieves contractor data from the GAF roofing directory, what operations it performs on that data, and how it stores and serves the results.

---

## Table of Contents

1. [The Problem](#1-the-problem)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Stage 1 — Data Retrieval (Scraping)](#3-stage-1--data-retrieval-scraping)
4. [Stage 2 — Data Storage](#4-stage-2--data-storage)
5. [Stage 3 — Intelligence Enrichment](#5-stage-3--intelligence-enrichment)
6. [Stage 4 — Insight Persistence](#6-stage-4--insight-persistence)
7. [Stage 5 — Data Access (API & CLI)](#7-stage-5--data-access-api--cli)
8. [Data Flow Diagram](#8-data-flow-diagram)
9. [Running the Pipeline](#9-running-the-pipeline)
10. [Database Schema](#10-database-schema)
11. [Design Decisions](#11-design-decisions)

---

## 1. The Problem

GAF ([gaf.com](https://www.gaf.com)) is North America's leading roofing manufacturer. They maintain a public directory of certified roofing contractors at:

```
https://www.gaf.com/en-us/roofing-contractors/residential?distance=25&postalCode=10013&countryCode=us
```

The goal is to:

1. **Collect** structured contractor data from this directory (names, ratings, certifications, contact info, etc.)
2. **Store** it in a database that handles both structured fields and unstructured text
3. **Transform** the raw data into actionable sales intelligence (summaries, talking points, lead scores)
4. **Serve** the data through a REST API that handles concurrent requests

The challenge: GAF's website is a JavaScript-rendered single-page application protected by Akamai CDN bot detection. Standard HTTP requests (`curl`, `requests`) return `403 Access Denied`. The contractor data is loaded dynamically via a third-party search API (Coveo) and paginated using URL hash fragments.

---

## 2. High-Level Architecture

```
                         python main.py pipeline --demo
                                    |
                                    v
    ┌──────────────────────────────────────────────────────────┐
    │                    PIPELINE ORCHESTRATOR                  │
    │                      (pipeline.py)                        │
    │                                                          │
    │   Stage 1          Stage 2         Stage 3      Stage 4  │
    │  ┌────────┐    ┌───────────┐    ┌──────────┐  ┌───────┐ │
    │  │ SCRAPE │───>│   STORE   │───>│ ENRICH   │─>│ STORE │ │
    │  │        │    │ in SQLite │    │ with AI  │  │ INSIGHTS│ │
    │  └────────┘    └───────────┘    └──────────┘  └───────┘ │
    │       |              |               |             |      │
    │       v              v               v             v      │
    │   Coveo API     gaf_contractors   GPT / Demo   contractor │
    │   intercept        .db           heuristics    _insights  │
    │   + Playwright                                 table      │
    └──────────────────────────────────────────────────────────┘
                                    |
                         ┌──────────┴──────────┐
                         v                     v
                    JSON / CSV            REST API
                    file exports       (python main.py serve)
```

Every pipeline run is assigned an auto-incrementing `run_id` and tracked in the `pipeline_runs` table with status (`running` / `completed` / `failed`), timestamps, and a full configuration snapshot.

---

## 3. Stage 1 — Data Retrieval (Scraping)

**File**: `scraper.py`  
**Class**: `GAFScraper`

This is the most technically involved stage. Data retrieval happens in three phases:

### Phase 1: Browser Setup & Bot Evasion

The scraper launches a headless Chrome browser using [Playwright](https://playwright.dev/python/) with several anti-detection measures:

1. **`playwright-stealth`** — A library that patches browser APIs to remove automation fingerprints. It overrides `navigator.webdriver`, `navigator.platform`, and other properties that bot detectors check.

2. **Chrome channel** — Instead of using Playwright's bundled Chromium, the scraper prefers the system-installed Chrome (`channel="chrome"`) because it has a more realistic browser fingerprint (extensions, default settings, etc.).

3. **Realistic context** — The browser context is configured with:
   - A real-looking User-Agent string
   - `1920x1080` viewport
   - `America/New_York` timezone
   - English locale
   - Geolocation permissions

4. **Warm-up navigation** — Before hitting the contractor search page, the scraper navigates to `https://www.gaf.com` first. This establishes session cookies and passes Akamai's initial JavaScript challenge. Without this step, subsequent requests get blocked.

```python
# Simplified view of what happens
browser = playwright.chromium.launch(channel="chrome", headless=True)
context = browser.new_context(viewport=..., user_agent=..., timezone_id=...)
stealth.apply_stealth_async(context)     # patch automation fingerprints
page = context.new_page()
page.goto("https://www.gaf.com")         # warm-up — pass Akamai challenge
```

### Phase 2: Coveo API Interception (Primary Data Source)

When the GAF search page loads, the frontend JavaScript makes an API call to Coveo, a cloud search platform:

```
POST https://platform.cloud.coveo.com/rest/search/v2?...
```

This response contains **structured JSON** with all the contractor data for the current page (10 results per page). The scraper captures this response automatically:

1. An `APIInterceptor` object is registered as a response listener on the Playwright page.
2. Every HTTP response is checked. If the URL contains `coveo.com/rest/search`, the JSON body is captured and stored.
3. The `parse_coveo_results()` function extracts contractor fields from the Coveo response:

| Coveo Field | Mapped To |
|-------------|-----------|
| `raw.gaf_contractor_id` | `contractor_id` |
| `raw.gaf_contractor_dba` | `name` |
| `raw.gaf_f_city` | `city` |
| `raw.gaf_f_state_code` | `state` |
| `raw.gaf_postal_code` | `postal_code` |
| `raw.gaf_phone` | `phone` |
| `raw.gaf_rating` | `rating` |
| `raw.gaf_number_of_reviews` | `review_count` |
| `raw.gaf_f_contractor_certifications_and_awards_residential` | `certifications` |
| `clickUri` | `profile_url` |

This API interception approach is far more reliable than parsing HTML, because the data is already structured.

### Phase 3: Pagination via Hash Fragments

GAF's search results are paginated 10 at a time. The pagination uses **URL hash fragments**, not query parameters:

```
Page 1:  ...?distance=25&postalCode=10013&countryCode=us          (firstResult=0, implicit)
Page 2:  ...?distance=25&postalCode=10013&countryCode=us#firstResult=10
Page 3:  ...?distance=25&postalCode=10013&countryCode=us#firstResult=20
Page N:  ...?distance=25&postalCode=10013&countryCode=us#firstResult=(N-1)*10
```

The scraper:

1. Loads page 1 and reads `totalCount` from the Coveo response (e.g., 71).
2. Calculates the number of pages needed: `ceil(71 / 10) = 8`.
3. For each subsequent page, navigates to the URL with the appropriate `#firstResult=` hash.
4. Each navigation triggers a new Coveo API call, which the interceptor captures.
5. A random delay (1.5–4.0 seconds) is inserted between page loads to avoid rate limiting.
6. All captured Coveo responses are deduplicated by `contractor_id`.

```
Page 1: captured 10 results (total: 71)
Page 2: captured 10 results → cumulative: 20/71
Page 3: captured 10 results → cumulative: 30/71
...
Page 8: captured 1 result  → cumulative: 71/71
```

### Phase 4: Profile Enrichment (Optional)

If `--skip-profiles` is not set, the scraper visits each contractor's individual profile page to collect additional fields not available in the Coveo API:

- Full street address
- Website URL
- Company description ("About" text)
- Years in business
- Number of employees
- State license number
- Job photos

Each profile page is parsed with [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/) using regex patterns and DOM traversal. The enriched data is merged back into the Coveo-sourced contractor records using `COALESCE` logic (enriched fields overwrite only if the base field was empty).

This phase is slow (~2 seconds per contractor due to rate limiting) but provides richer data. It is skipped by default in the pipeline for speed.

### Output of Stage 1

A `ScrapingResult` object containing:
- A list of `Contractor` Pydantic models (71 for the 10013/25mi search)
- Search metadata (postal code, distance, timestamp)
- Error list (any failed profile scrapes)

---

## 4. Stage 2 — Data Storage

**File**: `database.py`  
**Class**: `Database`

The scraped contractors are persisted into a SQLite database (`gaf_contractors.db`). This stage handles the mix of structured and unstructured data:

### What Happens

1. **UPSERT operation** — Each contractor is inserted with `ON CONFLICT(contractor_id) DO UPDATE`. If a contractor already exists from a previous run, their record is updated. Fields use `COALESCE` so that non-null values from the new scrape overwrite, but existing data is preserved if the new scrape returned null.

2. **Certifications normalized** — Certifications are stored in a separate `certifications` table (many-to-one relationship with contractors) and deduplicated with `ON CONFLICT(contractor_id, name) DO NOTHING`.

3. **Unstructured text stored as-is** — The `description` field (free-text company bio) is stored directly as a `TEXT` column. No attempt is made to parse it further at this stage.

4. **JSON arrays in TEXT columns** — Fields like `profile_photo_urls` and `job_photo_urls` are serialized as JSON strings in TEXT columns. This keeps the schema simple while preserving structured list data.

5. **File exports** — Simultaneously, the data is exported to timestamped JSON and CSV files in the `output/` directory for portability.

### Consistency Guarantees

- **WAL mode** (`PRAGMA journal_mode=WAL`) — Allows concurrent reads while a write is in progress. Critical for the API server where multiple requests may hit the database simultaneously.
- **Foreign keys enforced** (`PRAGMA foreign_keys=ON`) — Certifications and insights reference valid contractor IDs.
- **Transaction wrapping** — Every batch of writes (all contractors in one run) is wrapped in a single transaction. Either all succeed or all roll back.
- **Thread-local connections** — Each thread gets its own SQLite connection via `threading.local()`, preventing cross-thread corruption.

---

## 5. Stage 3 — Intelligence Enrichment

**File**: `insights.py` (GPT mode), `pipeline.py` (demo mode)  
**Class**: `SalesIntelligenceEngine`

This stage transforms raw contractor data into actionable sales intelligence. There are two modes:

### Mode A: ChatGPT API (Production)

Activated when an OpenAI API key is provided (`--api-key` or `OPENAI_API_KEY` env var).

**Step 1: Local Pre-Analytics**

Before calling GPT, the system computes statistics from the scraped data to give the LLM concrete numbers:

| Metric | Example Value |
|--------|---------------|
| Total contractors | 71 |
| Average rating | 4.53 |
| Average review count | 135 |
| Certification distribution | `{GAF Master Elite: 71, President's Club Award: 11}` |
| State distribution | `{NJ: 48, NY: 23}` |
| Top 5 city clusters | Staten Island NY, Bronx NY, Wayne NJ, ... |
| Top 5 by review volume | A1 Affordable (489), Matute Roofing (452), ... |

These analytics are injected into every GPT prompt as "market context."

**Step 2: Market-Level Insights (1 API call)**

A single GPT call generates the market overview:
- Market summary (competitive landscape, trends, maturity)
- Geographic clusters with reasoning
- Top performers and why they stand out
- Market opportunities for the sales team
- Competitive dynamics analysis

The prompt includes the full analytics summary and a list of contractors grouped by certification tier.

**Step 3: Per-Contractor Insights (batched API calls)**

Contractors are sent to GPT in batches of 15 (fits comfortably within the context window). Each batch call generates, for every contractor:

| Field | Description |
|-------|-------------|
| `summary` | 2-3 sentence executive brief |
| `industry_tags` | Classification tags (Residential Roofing, Multi-Service Exterior, etc.) |
| `business_size_estimate` | Micro / Small / Mid-size / Large |
| `talking_points` | 3+ conversation starters with hooks and supporting data |
| `pain_points` | Likely challenges the contractor faces |
| `sales_readiness_score` | 1-10 score (HOT >= 8, WARM >= 5, COLD < 5) |
| `sales_readiness_rationale` | Why this score was assigned |
| `competitive_position` | How they rank vs. local competitors |
| `opportunity_signals` | Growth indicators, expansion signals, technology gaps |
| `recommended_actions` | Concrete next steps for the sales rep |

**Structured Output**: The GPT call uses `response_format: {"type": "json_object"}` to enforce valid JSON output. The response is parsed into Pydantic models for type safety.

**Prompt Engineering**: The system prompt includes:
- Role: "sales intelligence analyst specializing in the roofing and home improvement industry"
- GAF certification hierarchy (President's Club > Master Elite > Certified Plus > Certified)
- Instructions to be specific, reference actual data, and flag uncertainty
- Exact JSON schema for the response

### Mode B: Demo (No API Key)

Activated with `--demo` or when no API key is available. Uses rule-based heuristics:

- **Sales readiness score**: Base 5, +2 if reviews >= 300, +1 if reviews >= 100, +1 if rating = 5.0, +1 if President's Club
- **Business size**: Mid-size (reviews >= 400), Small (>= 150), Micro (< 150)
- **Industry tags**: Derived from the company name (e.g., "Home Improvement" in name adds "Multi-Service Exterior" tag)
- **Talking points**: Generated from rating/review data compared to market averages
- **Market insight**: Assembled from local analytics (certification distribution, geographic clusters, average metrics)

Demo mode produces the same output format as GPT mode, allowing the full pipeline to run without an API key.

### Optional: Web Context Enrichment

When `--web-enrich` is enabled, an additional GPT call per contractor generates a brief business context summary based on GPT's training data knowledge of the company's region, typical services, and local housing market. This context is injected into the main per-contractor prompt for richer insights.

---

## 6. Stage 4 — Insight Persistence

**File**: `database.py` (methods `upsert_insights`)

The generated sales intelligence report is stored in the database:

### Market Insights (`market_insights` table)

One row per pipeline run containing:
- `market_summary` — Free-text AI-generated market overview (TEXT column)
- `certification_distribution` — JSON object in a TEXT column
- `geographic_clusters` — JSON array in a TEXT column
- `avg_rating`, `avg_review_count` — Numeric columns
- `top_performers`, `market_opportunities` — JSON arrays in TEXT columns
- `competitive_landscape` — Free-text AI analysis (TEXT column)

### Contractor Insights (`contractor_insights` table)

One row per contractor, keyed by `contractor_id`:
- `summary` — Free-text AI-generated executive brief (TEXT column)
- `talking_points` — JSON array of objects, each with `hook`, `detail`, `source` (TEXT column)
- `pain_points`, `opportunity_signals`, `recommended_actions` — JSON arrays (TEXT columns)
- `sales_readiness_score` — Integer 1-10 (indexed for fast sorting)
- `sales_readiness_rationale`, `competitive_position` — Free-text (TEXT columns)
- `industry_tags` — JSON array (TEXT column)
- `business_size_estimate` — Short string (TEXT column)

Uses `ON CONFLICT(contractor_id) DO UPDATE` so re-running enrichment updates existing insights without creating duplicates.

### File Exports

Simultaneously, the full report is written to a timestamped JSON file:
```
output/sales_intelligence_10013_20260212_202202.json
```

---

## 7. Stage 5 — Data Access (API & CLI)

### REST API (`server.py`)

A FastAPI server provides concurrent HTTP access to all stored data:

```bash
python main.py serve --port 8000
```

**Concurrency model**: FastAPI uses async/await for request handling. The SQLite database uses WAL mode so multiple readers can operate concurrently without blocking each other. Pipeline runs are executed in a background thread pool so the API remains responsive during long-running scrapes.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/stats` | GET | Dashboard: total contractors, insight coverage, avg score, run count |
| `/pipeline/run` | POST | Trigger a full pipeline run in the background; returns `run_id` immediately |
| `/pipeline/runs` | GET | List all pipeline runs with status, counts, timestamps |
| `/pipeline/runs/{id}` | GET | Detail for a specific run |
| `/contractors` | GET | Search/filter: `?state=NJ&min_rating=4.5&min_score=7&certification=Master` |
| `/contractors/{id}` | GET | Single contractor with certifications and full sales insight joined |
| `/insights/market` | GET | Latest market intelligence report |
| `/insights/top` | GET | Top leads by sales readiness score: `?min_score=8&limit=10` |
| `/docs` | GET | Interactive Swagger UI (auto-generated by FastAPI) |

**Search query**: The `/contractors` endpoint joins `contractors` with `contractor_insights` and supports filtering by postal code, state, city, minimum rating, minimum readiness score, and certification name. Results are sorted by readiness score (desc), then rating (desc).

### CLI Database Queries (`main.py db`)

```bash
python main.py db -a stats              # dashboard statistics
python main.py db -a runs               # audit trail of pipeline runs
python main.py db -a top --min-score 7  # top leads
python main.py db -a search --state NJ  # filtered search
```

---

## 8. Data Flow Diagram

```
 GAF Website (gaf.com)
      │
      │  Playwright headless Chrome + stealth
      │  navigates to search page
      │
      ▼
 ┌─────────────────────────┐
 │  Coveo Search API       │  ◄── Browser JS triggers this automatically
 │  (coveo.com/rest/search)│
 │                         │
 │  Returns structured JSON│
 │  per page (10 results)  │
 └────────────┬────────────┘
              │
              │  APIInterceptor captures each response
              │  Pagination: #firstResult=0, 10, 20 ... N
              │
              ▼
 ┌─────────────────────────┐
 │  parse_coveo_results()  │
 │                         │
 │  Raw JSON ──► list of   │
 │  Contractor models      │
 │  (Pydantic validated)   │
 └────────────┬────────────┘
              │
              │  71 contractors (deduplicated by ID)
              │
              ▼
 ┌─────────────────────────┐         ┌──────────────────┐
 │  SQLite Database        │         │  File Exports     │
 │  gaf_contractors.db     │         │                  │
 │                         │         │  contractors_     │
 │  ┌─────────────────┐   │         │   10013_*.json   │
 │  │ contractors      │   │         │  contractors_     │
 │  │ (71 rows)        │   │──────►  │   10013_*.csv    │
 │  ├─────────────────┤   │         │                  │
 │  │ certifications   │   │         └──────────────────┘
 │  │ (many-to-one)    │   │
 │  └─────────────────┘   │
 └────────────┬────────────┘
              │
              │  contractors loaded from DB/memory
              │
              ▼
 ┌─────────────────────────┐
 │  Intelligence Engine    │
 │                         │
 │  1. compute_local_      │
 │     analytics()         │     ┌─────────────────────┐
 │     • avg rating: 4.53  │     │  OpenAI GPT API     │
 │     • cert distribution │────►│  (gpt-4o-mini)      │
 │     • geo clusters      │     │                     │
 │     • top performers    │     │  OR                  │
 │                         │     │                     │
 │  2. build prompts with  │     │  Demo heuristics    │
 │     market context      │     │  (no API key)       │
 │                         │     └──────────┬──────────┘
 │  3. parse structured    │                │
 │     JSON response       │◄───────────────┘
 └────────────┬────────────┘
              │
              │  SalesIntelligenceReport
              │  (market insight + 71 contractor insights)
              │
              ▼
 ┌─────────────────────────┐         ┌──────────────────┐
 │  SQLite Database        │         │  File Export      │
 │                         │         │                  │
 │  ┌─────────────────┐   │         │  sales_           │
 │  │ contractor_      │   │         │  intelligence_    │
 │  │  insights (71)   │   │──────►  │   10013_*.json   │
 │  ├─────────────────┤   │         │                  │
 │  │ market_insights  │   │         └──────────────────┘
 │  │  (1 row)         │   │
 │  ├─────────────────┤   │
 │  │ pipeline_runs    │   │
 │  │  (audit trail)   │   │
 │  └─────────────────┘   │
 └────────────┬────────────┘
              │
              │  Data is now queryable
              │
      ┌───────┴───────┐
      ▼               ▼
 ┌──────────┐   ┌───────────┐
 │ REST API │   │ CLI       │
 │ :8000    │   │ main.py   │
 │ /docs    │   │ db -a top │
 └──────────┘   └───────────┘
```

---

## 9. Running the Pipeline

### Prerequisites

```bash
# Create environment
micromamba create -n gaf-scraper python=3.12 -y -c conda-forge

# Install dependencies
micromamba run -n gaf-scraper pip install -r requirements.txt

# Install browser
micromamba run -n gaf-scraper playwright install chromium
```

### One-Command End-to-End (Demo Mode)

```bash
micromamba run -n gaf-scraper python main.py pipeline --demo
```

This runs all four stages automatically:
1. Scrapes all contractors (71 for 10013/25mi) — ~90 seconds
2. Stores them in `gaf_contractors.db`
3. Generates sales intelligence using demo heuristics
4. Stores insights in the database and exports JSON files

### With Real AI Insights

```bash
export OPENAI_API_KEY=sk-your-key-here
micromamba run -n gaf-scraper python main.py pipeline
```

### Custom Search Area

```bash
micromamba run -n gaf-scraper python main.py pipeline -p 90210 -d 50 --demo
```

### Start the API Server

```bash
micromamba run -n gaf-scraper python main.py serve --port 8000
# Open http://localhost:8000/docs for interactive API docs
```

### Query the Database

```bash
micromamba run -n gaf-scraper python main.py db -a stats
micromamba run -n gaf-scraper python main.py db -a top --min-score 8
micromamba run -n gaf-scraper python main.py db -a runs
micromamba run -n gaf-scraper python main.py db -a search --state NJ
```

---

## 10. Database Schema

```sql
-- Audit trail: every pipeline execution is tracked
pipeline_runs (
    id, postal_code, distance, status,
    total_found, total_scraped, total_enriched,
    errors,          -- JSON array
    started_at, completed_at,
    config_json      -- full configuration snapshot
)

-- Structured contractor data (primary key: contractor_id)
contractors (
    contractor_id PK, name, slug,
    address, city, state, postal_code, country,    -- structured location
    phone, website,
    description,                                    -- unstructured free-text
    rating, review_count,                           -- numeric metrics
    years_in_business, number_of_employees,
    state_license_number,
    profile_url,
    profile_photo_urls,  -- JSON array stored as TEXT
    job_photo_urls,      -- JSON array stored as TEXT
    scraped_at, updated_at, pipeline_run_id FK
)

-- Normalized certifications (many-to-one)
certifications (
    id PK, contractor_id FK, name, type
    UNIQUE(contractor_id, name)
)

-- AI-generated per-contractor insights (one-to-one with contractors)
contractor_insights (
    contractor_id PK FK,
    summary,                    -- unstructured AI text
    industry_tags,              -- JSON array as TEXT
    business_size_estimate,
    talking_points,             -- JSON array of objects as TEXT
    pain_points,                -- JSON array as TEXT
    sales_readiness_score,      -- indexed integer 1-10
    sales_readiness_rationale,  -- unstructured AI text
    competitive_position,       -- unstructured AI text
    opportunity_signals,        -- JSON array as TEXT
    recommended_actions,        -- JSON array as TEXT
    generated_at, pipeline_run_id FK
)

-- AI-generated market-level analysis (one per pipeline run)
market_insights (
    id PK,
    region, market_summary,              -- unstructured AI text
    certification_distribution,          -- JSON object as TEXT
    geographic_clusters,                 -- JSON array as TEXT
    avg_rating, avg_review_count,        -- numeric
    top_performers, market_opportunities,-- JSON arrays as TEXT
    competitive_landscape,               -- unstructured AI text
    generated_at, pipeline_run_id FK
)

-- Indexes for fast queries
idx_contractors_city_state, idx_contractors_rating,
idx_contractors_search, idx_certifications_contractor,
idx_insights_score
```

### Structured vs. Unstructured Storage Strategy

| Data Type | Storage Approach | Example |
|-----------|-----------------|---------|
| Identity / contact | Typed columns with indexes | `name TEXT`, `rating REAL`, `city TEXT` |
| Lists of primitives | JSON array in TEXT column | `pain_points: '["challenge 1", "challenge 2"]'` |
| Lists of objects | JSON array of dicts in TEXT | `talking_points: '[{"hook": "...", "detail": "..."}]'` |
| Free-form AI text | Plain TEXT column | `summary`, `competitive_position` |
| Enumerations | Plain TEXT column | `business_size_estimate: "Mid-size"` |
| Relationships | Normalized table with FK | `certifications` table |

---

## 11. Design Decisions

### Why Playwright + Stealth Instead of HTTP Requests?

GAF uses Akamai CDN bot protection. Direct HTTP requests (`curl`, Python `requests`) return `403 Access Denied`. A headless browser is required to:
- Execute the JavaScript that renders the page
- Pass Akamai's browser fingerprinting checks
- Trigger the Coveo API calls that load contractor data

`playwright-stealth` patches automation-detectable properties (`navigator.webdriver`, etc.) so the browser appears to be a real user.

### Why Intercept the Coveo API Instead of Parsing HTML?

The Coveo API returns structured JSON — already organized into fields like `gaf_contractor_id`, `gaf_rating`, `gaf_phone`. Parsing this is:
- **More reliable** — No fragile CSS selector dependencies
- **More complete** — Contains fields not visible in the HTML cards
- **Faster** — One JSON parse vs. DOM traversal of complex React-rendered HTML

### Why SQLite?

- **Zero configuration** — No database server to install or manage
- **Portable** — The entire database is a single file (`gaf_contractors.db`)
- **Sufficient for scale** — SQLite handles millions of rows; 71 contractors is trivial
- **WAL mode** — Supports concurrent reads from the API server
- **Upgrade path** — The `Database` class abstracts all SQL; swapping to PostgreSQL requires changing only the connection layer

### Why UPSERT Semantics?

Running the pipeline multiple times for the same ZIP code should update existing records, not create duplicates. `ON CONFLICT DO UPDATE` with `COALESCE` ensures:
- New data overwrites old data where available
- Existing data is preserved where the new scrape returned null
- Certifications are deduplicated
- Insights are replaced with the latest generation

### Why Background Pipeline Execution in the API?

A full pipeline run takes ~90 seconds. The API server uses `BackgroundTasks` to execute pipeline runs in a thread pool, returning the `run_id` immediately. Clients poll `GET /pipeline/runs/{id}` to check progress. This keeps the API responsive for concurrent read requests.
