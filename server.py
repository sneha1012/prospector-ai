"""
FastAPI Backend
===============

REST API that serves scraped contractor data and sales intelligence
from the SQLite database.  Handles concurrent requests via async/await
and maintains data consistency through the Database layer.

Endpoints:
    GET  /                          → health / welcome
    GET  /stats                     → dashboard statistics
    POST /pipeline/run              → trigger a full scrape+enrich pipeline
    GET  /pipeline/runs             → list past pipeline runs
    GET  /pipeline/runs/{run_id}    → single pipeline run detail
    GET  /contractors               → search / list contractors
    GET  /contractors/{id}          → single contractor + insight
    GET  /insights/market           → latest market insight
    GET  /insights/market/{run_id}  → market insight for a specific run
    GET  /insights/top              → top contractors by sales readiness
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GAF Contractor Intelligence API",
    description=(
        "REST API serving scraped contractor data and AI-generated "
        "sales intelligence from the GAF roofing contractor directory."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("GAF_DB_PATH", "gaf_contractors.db")
db = Database(DB_PATH)

# Serve static files (dashboard)
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Thread pool for running the synchronous pipeline in the background
_executor = ThreadPoolExecutor(max_workers=2)

# Track in-flight pipeline runs so we don't double-start
_running_pipelines: dict[int, bool] = {}


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup() -> None:
    db.initialize()
    logger.info("Database initialized at %s", DB_PATH)


@app.on_event("shutdown")
async def shutdown() -> None:
    db.close()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class PipelineRequest(BaseModel):
    postal_code: str = Field(default="10013", description="ZIP code to search around")
    distance: int = Field(default=25, description="Search radius in miles")
    max_contractors: int = Field(default=0, description="0 = no limit")
    headless: bool = Field(default=True, description="Run browser headless")
    skip_profiles: bool = Field(default=True, description="Skip individual profile scraping")
    demo_mode: bool = Field(default=True, description="Use demo enrichment (no API key)")
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI key (or set env)")
    openai_model: str = Field(default="gpt-4o-mini")


class PipelineResponse(BaseModel):
    run_id: int
    status: str
    message: str


class StatsResponse(BaseModel):
    total_contractors: int
    contractors_with_insights: int
    avg_sales_readiness_score: float
    total_pipeline_runs: int


class ContractorSearchParams(BaseModel):
    postal_code: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    min_rating: Optional[float] = None
    min_score: Optional[int] = None
    certification: Optional[str] = None
    limit: int = 50
    offset: int = 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "GAF Contractor Intelligence API",
        "version": "1.0.0",
        "docs": "/docs",
        "dashboard": "/dashboard",
    }


@app.get("/dashboard")
async def dashboard() -> FileResponse:
    """Interactive sales intelligence dashboard."""
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


@app.get("/stats", response_model=StatsResponse)
async def get_stats() -> dict[str, Any]:
    """Dashboard statistics."""
    return db.get_stats()


# --- Pipeline management ---


@app.post("/pipeline/run", response_model=PipelineResponse)
async def start_pipeline(
    req: PipelineRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """
    Trigger a full pipeline run (scrape → store → enrich → store).

    Runs in the background so the API returns immediately with a run_id.
    Poll GET /pipeline/runs/{run_id} to check progress.
    """
    from pipeline import Pipeline

    pipeline = Pipeline(
        postal_code=req.postal_code,
        distance=req.distance,
        db_path=DB_PATH,
        openai_api_key=req.openai_api_key or os.environ.get("OPENAI_API_KEY"),
        openai_model=req.openai_model,
        max_contractors=req.max_contractors,
        headless=req.headless,
        skip_profiles=req.skip_profiles,
        demo_mode=req.demo_mode,
    )

    # Create the run record synchronously so we can return the ID
    run_id = pipeline.db.create_pipeline_run(
        req.postal_code,
        req.distance,
        config_json=json.dumps(req.model_dump()),
    )

    # Enqueue background execution
    background_tasks.add_task(_run_pipeline_in_background, pipeline, run_id)
    _running_pipelines[run_id] = True

    return {
        "run_id": run_id,
        "status": "started",
        "message": f"Pipeline run #{run_id} started. Poll /pipeline/runs/{run_id} for status.",
    }


def _run_pipeline_in_background(pipeline: "Pipeline", run_id: int) -> None:
    """Execute the synchronous pipeline in the background."""
    try:
        result = pipeline.run()
        logger.info("Background pipeline run #%d result: %s", run_id, result.get("status"))
    except Exception as e:
        logger.error("Background pipeline run #%d failed: %s", run_id, e)
        pipeline.db.update_pipeline_run(run_id, status="failed", errors=[str(e)])
    finally:
        _running_pipelines.pop(run_id, None)


@app.get("/pipeline/runs")
async def list_pipeline_runs(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List recent pipeline runs."""
    runs = db.list_pipeline_runs(limit=limit)
    # Annotate with live status
    for run in runs:
        if run["id"] in _running_pipelines:
            run["live_status"] = "running"
    return runs


@app.get("/pipeline/runs/{run_id}")
async def get_pipeline_run(run_id: int) -> dict[str, Any]:
    """Get details of a specific pipeline run."""
    run = db.get_pipeline_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Pipeline run #{run_id} not found")
    if run["id"] in _running_pipelines:
        run["live_status"] = "running"
    return run


# --- Contractors ---


@app.get("/contractors")
async def search_contractors(
    postal_code: Optional[str] = None,
    state: Optional[str] = None,
    city: Optional[str] = None,
    min_rating: Optional[float] = None,
    min_score: Optional[int] = None,
    certification: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """
    Search and filter contractors with optional insight data.
    Results are sorted by sales readiness score (desc), then rating (desc).
    """
    results = db.search_contractors(
        postal_code=postal_code,
        state=state,
        city=city,
        min_rating=min_rating,
        min_score=min_score,
        certification=certification,
        limit=limit,
        offset=offset,
    )
    return {
        "count": len(results),
        "limit": limit,
        "offset": offset,
        "contractors": results,
    }


@app.get("/contractors/{contractor_id}")
async def get_contractor(contractor_id: str) -> dict[str, Any]:
    """Get a single contractor with certifications and sales insight."""
    result = db.get_contractor(contractor_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"Contractor {contractor_id} not found",
        )
    return result


# --- Insights ---


@app.get("/insights/market")
async def get_market_insight(
    pipeline_run_id: Optional[int] = None,
) -> dict[str, Any]:
    """Get the latest (or run-specific) market insight."""
    result = db.get_market_insight(pipeline_run_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail="No market insight found. Run the pipeline first.",
        )
    return result


@app.get("/insights/top")
async def get_top_contractors(
    min_score: int = Query(default=7, ge=1, le=10),
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Get top contractors by sales readiness score."""
    results = db.search_contractors(min_score=min_score, limit=limit)
    return {
        "min_score": min_score,
        "count": len(results),
        "contractors": results,
    }
