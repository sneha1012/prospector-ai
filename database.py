"""
Database Layer — SQLite storage for structured + unstructured contractor data.

Design:
  - SQLite for zero-config portability (upgrade to PostgreSQL by swapping the engine)
  - Normalized schema: contractors, certifications, insights are separate tables
  - Unstructured blobs (descriptions, AI summaries, talking points) stored as TEXT/JSON
  - Pipeline runs tracked in a `pipeline_runs` table for auditability
  - Thread-safe: uses `check_same_thread=False` and connection-per-call pattern
  - All writes go through a single module so consistency is enforced in one place
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from models import (
    Certification,
    Contractor,
    ContractorInsight,
    MarketInsight,
    SalesIntelligenceReport,
    ScrapingResult,
    TalkingPoint,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "gaf_contractors.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    postal_code     TEXT    NOT NULL,
    distance        INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',   -- running | completed | failed
    total_found     INTEGER DEFAULT 0,
    total_scraped   INTEGER DEFAULT 0,
    total_enriched  INTEGER DEFAULT 0,
    errors          TEXT    DEFAULT '[]',                 -- JSON array of error strings
    started_at      TEXT    NOT NULL,
    completed_at    TEXT,
    config_json     TEXT                                  -- full ScraperConfig snapshot
);

CREATE TABLE IF NOT EXISTS contractors (
    contractor_id       TEXT    PRIMARY KEY,
    name                TEXT    NOT NULL,
    slug                TEXT,
    address             TEXT,
    city                TEXT,
    state               TEXT,
    postal_code         TEXT,
    country             TEXT    DEFAULT 'US',
    phone               TEXT,
    website             TEXT,
    description         TEXT,                             -- unstructured free-text
    rating              REAL,
    review_count        INTEGER,
    years_in_business   TEXT,
    number_of_employees TEXT,
    state_license_number TEXT,
    profile_url         TEXT,
    profile_photo_urls  TEXT    DEFAULT '[]',             -- JSON array
    job_photo_urls      TEXT    DEFAULT '[]',             -- JSON array
    search_postal_code  TEXT,
    search_distance     INTEGER,
    scraped_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    pipeline_run_id     INTEGER REFERENCES pipeline_runs(id)
);

CREATE TABLE IF NOT EXISTS certifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contractor_id   TEXT    NOT NULL REFERENCES contractors(contractor_id),
    name            TEXT    NOT NULL,
    type            TEXT    NOT NULL DEFAULT 'certification',
    UNIQUE(contractor_id, name)
);

CREATE TABLE IF NOT EXISTS contractor_insights (
    contractor_id           TEXT    PRIMARY KEY REFERENCES contractors(contractor_id),
    contractor_name         TEXT,
    summary                 TEXT,                         -- unstructured AI summary
    industry_tags           TEXT    DEFAULT '[]',         -- JSON array
    business_size_estimate  TEXT,
    talking_points          TEXT    DEFAULT '[]',         -- JSON array of objects
    pain_points             TEXT    DEFAULT '[]',         -- JSON array
    sales_readiness_score   INTEGER DEFAULT 0,
    sales_readiness_rationale TEXT,                       -- unstructured AI text
    competitive_position    TEXT,                         -- unstructured AI text
    opportunity_signals     TEXT    DEFAULT '[]',         -- JSON array
    recommended_actions     TEXT    DEFAULT '[]',         -- JSON array
    generated_at            TEXT    NOT NULL,
    pipeline_run_id         INTEGER REFERENCES pipeline_runs(id)
);

CREATE TABLE IF NOT EXISTS market_insights (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    region                      TEXT,
    market_summary              TEXT,                     -- unstructured AI text
    certification_distribution  TEXT    DEFAULT '{}',     -- JSON object
    geographic_clusters         TEXT    DEFAULT '[]',     -- JSON array
    avg_rating                  REAL,
    avg_review_count            REAL,
    top_performers              TEXT    DEFAULT '[]',     -- JSON array
    market_opportunities        TEXT    DEFAULT '[]',     -- JSON array
    competitive_landscape       TEXT,                     -- unstructured AI text
    generated_at                TEXT    NOT NULL,
    pipeline_run_id             INTEGER REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_contractors_city_state ON contractors(city, state);
CREATE INDEX IF NOT EXISTS idx_contractors_rating ON contractors(rating DESC);
CREATE INDEX IF NOT EXISTS idx_contractors_search ON contractors(search_postal_code, search_distance);
CREATE INDEX IF NOT EXISTS idx_certifications_contractor ON certifications(contractor_id);
CREATE INDEX IF NOT EXISTS idx_insights_score ON contractor_insights(sales_readiness_score DESC);
"""


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------


class Database:
    """
    Thread-safe SQLite database for the GAF pipeline.

    Usage:
        db = Database("gaf_contractors.db")
        db.initialize()
        run_id = db.create_pipeline_run("10013", 25)
        db.upsert_contractors(contractors, run_id)
        db.upsert_insights(report, run_id)
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._local = threading.local()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.path), check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._transaction() as conn:
            conn.executescript(_SCHEMA)
        logger.info("Database initialized at %s", self.path)

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # --- Pipeline runs -----------------------------------------------------

    def create_pipeline_run(
        self,
        postal_code: str,
        distance: int,
        config_json: str = "",
    ) -> int:
        """Create a new pipeline run record. Returns the run ID."""
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            cur = conn.execute(
                """INSERT INTO pipeline_runs
                   (postal_code, distance, status, started_at, config_json)
                   VALUES (?, ?, 'running', ?, ?)""",
                (postal_code, distance, now, config_json),
            )
            run_id = cur.lastrowid
        logger.info("Created pipeline run #%d", run_id)
        return run_id  # type: ignore[return-value]

    def update_pipeline_run(
        self,
        run_id: int,
        *,
        status: Optional[str] = None,
        total_found: Optional[int] = None,
        total_scraped: Optional[int] = None,
        total_enriched: Optional[int] = None,
        errors: Optional[list[str]] = None,
    ) -> None:
        """Update fields on a pipeline run."""
        updates: list[str] = []
        params: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            if status in ("completed", "failed"):
                updates.append("completed_at = ?")
                params.append(datetime.now(timezone.utc).isoformat())
        if total_found is not None:
            updates.append("total_found = ?")
            params.append(total_found)
        if total_scraped is not None:
            updates.append("total_scraped = ?")
            params.append(total_scraped)
        if total_enriched is not None:
            updates.append("total_enriched = ?")
            params.append(total_enriched)
        if errors is not None:
            updates.append("errors = ?")
            params.append(json.dumps(errors))
        if not updates:
            return
        params.append(run_id)
        sql = f"UPDATE pipeline_runs SET {', '.join(updates)} WHERE id = ?"
        with self._transaction() as conn:
            conn.execute(sql, params)

    def get_pipeline_run(self, run_id: int) -> Optional[dict[str, Any]]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_pipeline_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Contractors -------------------------------------------------------

    def upsert_contractors(
        self,
        contractors: list[Contractor],
        run_id: Optional[int] = None,
    ) -> int:
        """Insert or update contractors. Returns count upserted."""
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self._transaction() as conn:
            for c in contractors:
                if not c.contractor_id:
                    continue
                conn.execute(
                    """INSERT INTO contractors (
                        contractor_id, name, slug, address, city, state,
                        postal_code, country, phone, website, description,
                        rating, review_count, years_in_business,
                        number_of_employees, state_license_number,
                        profile_url, profile_photo_urls, job_photo_urls,
                        search_postal_code, search_distance,
                        scraped_at, updated_at, pipeline_run_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(contractor_id) DO UPDATE SET
                        name=excluded.name,
                        slug=excluded.slug,
                        address=COALESCE(excluded.address, contractors.address),
                        city=COALESCE(excluded.city, contractors.city),
                        state=COALESCE(excluded.state, contractors.state),
                        postal_code=COALESCE(excluded.postal_code, contractors.postal_code),
                        phone=COALESCE(excluded.phone, contractors.phone),
                        website=COALESCE(excluded.website, contractors.website),
                        description=COALESCE(excluded.description, contractors.description),
                        rating=COALESCE(excluded.rating, contractors.rating),
                        review_count=COALESCE(excluded.review_count, contractors.review_count),
                        years_in_business=COALESCE(excluded.years_in_business, contractors.years_in_business),
                        number_of_employees=COALESCE(excluded.number_of_employees, contractors.number_of_employees),
                        state_license_number=COALESCE(excluded.state_license_number, contractors.state_license_number),
                        profile_url=COALESCE(excluded.profile_url, contractors.profile_url),
                        profile_photo_urls=excluded.profile_photo_urls,
                        job_photo_urls=excluded.job_photo_urls,
                        search_postal_code=excluded.search_postal_code,
                        search_distance=excluded.search_distance,
                        updated_at=excluded.updated_at,
                        pipeline_run_id=excluded.pipeline_run_id
                    """,
                    (
                        c.contractor_id, c.name, c.slug, c.address, c.city,
                        c.state, c.postal_code, c.country, c.phone,
                        c.website, c.description, c.rating, c.review_count,
                        c.years_in_business, c.number_of_employees,
                        c.state_license_number, c.profile_url,
                        json.dumps(c.profile_photo_urls),
                        json.dumps(c.job_photo_urls),
                        c.search_postal_code, c.search_distance,
                        c.scraped_at, now, run_id,
                    ),
                )
                # Certifications
                for cert in c.certifications:
                    conn.execute(
                        """INSERT INTO certifications (contractor_id, name, type)
                           VALUES (?, ?, ?)
                           ON CONFLICT(contractor_id, name) DO NOTHING""",
                        (c.contractor_id, cert.name, cert.type),
                    )
                count += 1
        logger.info("Upserted %d contractors into database", count)
        return count

    def upsert_insights(
        self,
        report: SalesIntelligenceReport,
        run_id: Optional[int] = None,
    ) -> int:
        """Store AI-generated insights. Returns count upserted."""
        count = 0
        with self._transaction() as conn:
            # Market insight
            m = report.market
            if m.market_summary:
                conn.execute(
                    """INSERT INTO market_insights (
                        region, market_summary, certification_distribution,
                        geographic_clusters, avg_rating, avg_review_count,
                        top_performers, market_opportunities,
                        competitive_landscape, generated_at, pipeline_run_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        m.region, m.market_summary,
                        json.dumps(m.certification_distribution),
                        json.dumps(m.geographic_clusters),
                        m.avg_rating, m.avg_review_count,
                        json.dumps(m.top_performers),
                        json.dumps(m.market_opportunities),
                        m.competitive_landscape,
                        m.generated_at, run_id,
                    ),
                )

            # Per-contractor insights
            for ci in report.contractors:
                conn.execute(
                    """INSERT INTO contractor_insights (
                        contractor_id, contractor_name, summary,
                        industry_tags, business_size_estimate,
                        talking_points, pain_points,
                        sales_readiness_score, sales_readiness_rationale,
                        competitive_position, opportunity_signals,
                        recommended_actions, generated_at, pipeline_run_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(contractor_id) DO UPDATE SET
                        contractor_name=excluded.contractor_name,
                        summary=excluded.summary,
                        industry_tags=excluded.industry_tags,
                        business_size_estimate=excluded.business_size_estimate,
                        talking_points=excluded.talking_points,
                        pain_points=excluded.pain_points,
                        sales_readiness_score=excluded.sales_readiness_score,
                        sales_readiness_rationale=excluded.sales_readiness_rationale,
                        competitive_position=excluded.competitive_position,
                        opportunity_signals=excluded.opportunity_signals,
                        recommended_actions=excluded.recommended_actions,
                        generated_at=excluded.generated_at,
                        pipeline_run_id=excluded.pipeline_run_id
                    """,
                    (
                        ci.contractor_id, ci.contractor_name, ci.summary,
                        json.dumps(ci.industry_tags),
                        ci.business_size_estimate,
                        json.dumps([tp.model_dump() for tp in ci.talking_points]),
                        json.dumps(ci.pain_points),
                        ci.sales_readiness_score,
                        ci.sales_readiness_rationale,
                        ci.competitive_position,
                        json.dumps(ci.opportunity_signals),
                        json.dumps(ci.recommended_actions),
                        ci.generated_at, run_id,
                    ),
                )
                count += 1
        logger.info("Upserted %d contractor insights into database", count)
        return count

    # --- Queries -----------------------------------------------------------

    def get_contractor(self, contractor_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single contractor with certs and insight joined."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM contractors WHERE contractor_id = ?",
            (contractor_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # Attach certifications
        certs = conn.execute(
            "SELECT name, type FROM certifications WHERE contractor_id = ?",
            (contractor_id,),
        ).fetchall()
        result["certifications"] = [dict(c) for c in certs]
        # Attach insight if available
        insight = conn.execute(
            "SELECT * FROM contractor_insights WHERE contractor_id = ?",
            (contractor_id,),
        ).fetchone()
        result["insight"] = dict(insight) if insight else None
        return result

    def search_contractors(
        self,
        *,
        postal_code: Optional[str] = None,
        state: Optional[str] = None,
        city: Optional[str] = None,
        min_rating: Optional[float] = None,
        min_score: Optional[int] = None,
        certification: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Flexible search across contractors with optional insight join."""
        conditions: list[str] = []
        params: list[Any] = []

        if postal_code:
            conditions.append("c.search_postal_code = ?")
            params.append(postal_code)
        if state:
            conditions.append("c.state = ?")
            params.append(state)
        if city:
            conditions.append("c.city = ?")
            params.append(city)
        if min_rating is not None:
            conditions.append("c.rating >= ?")
            params.append(min_rating)
        if min_score is not None:
            conditions.append("ci.sales_readiness_score >= ?")
            params.append(min_score)
        if certification:
            conditions.append(
                "EXISTS (SELECT 1 FROM certifications cert "
                "WHERE cert.contractor_id = c.contractor_id AND cert.name LIKE ?)"
            )
            params.append(f"%{certification}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        sql = f"""
            SELECT c.*, ci.sales_readiness_score, ci.summary as insight_summary,
                   ci.business_size_estimate, ci.industry_tags
            FROM contractors c
            LEFT JOIN contractor_insights ci ON c.contractor_id = ci.contractor_id
            {where}
            ORDER BY COALESCE(ci.sales_readiness_score, 0) DESC, c.rating DESC
            LIMIT ? OFFSET ?
        """
        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_market_insight(
        self, pipeline_run_id: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        """Get the latest (or run-specific) market insight."""
        conn = self._get_conn()
        if pipeline_run_id:
            row = conn.execute(
                "SELECT * FROM market_insights WHERE pipeline_run_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (pipeline_run_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM market_insights ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict[str, Any]:
        """Dashboard statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0]
        with_insights = conn.execute(
            "SELECT COUNT(*) FROM contractor_insights"
        ).fetchone()[0]
        avg_score = conn.execute(
            "SELECT AVG(sales_readiness_score) FROM contractor_insights "
            "WHERE sales_readiness_score > 0"
        ).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        return {
            "total_contractors": total,
            "contractors_with_insights": with_insights,
            "avg_sales_readiness_score": round(avg_score, 1) if avg_score else 0,
            "total_pipeline_runs": runs,
        }
