"""
Unit tests for the SQLite database layer.

Tests cover schema initialization, UPSERT semantics, COALESCE merge
behavior, search/filter queries, stats computation, and pipeline run
audit trail.
"""

import tempfile
from pathlib import Path

import pytest

from database import Database
from models import (
    Certification,
    Contractor,
    ContractorInsight,
    MarketInsight,
    SalesIntelligenceReport,
    TalkingPoint,
)


@pytest.fixture
def db():
    """Create a temporary database for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        database = Database(db_path)
        database.initialize()
        yield database
        database.close()


def _make_contractor(cid: str = "100", name: str = "Test Roofing", **kwargs):
    """Helper to create a Contractor with sensible defaults."""
    defaults = dict(
        contractor_id=cid,
        name=name,
        city="Wayne",
        state="NJ",
        postal_code="07470",
        phone="(973) 555-0100",
        rating=4.8,
        review_count=150,
        certifications=[Certification(name="GAF Master Elite")],
    )
    defaults.update(kwargs)
    return Contractor(**defaults)


# ---------------------------------------------------------------------------
# Schema & initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_initialize_creates_tables(self, db):
        conn = db._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {row["name"] for row in tables}
        assert "contractors" in table_names
        assert "certifications" in table_names
        assert "contractor_insights" in table_names
        assert "market_insights" in table_names
        assert "pipeline_runs" in table_names

    def test_wal_mode_enabled(self, db):
        conn = db._get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db):
        conn = db._get_conn()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# ---------------------------------------------------------------------------
# Pipeline runs
# ---------------------------------------------------------------------------


class TestPipelineRuns:
    def test_create_run(self, db):
        run_id = db.create_pipeline_run("10013", 25)
        assert run_id == 1

    def test_sequential_run_ids(self, db):
        id1 = db.create_pipeline_run("10013", 25)
        id2 = db.create_pipeline_run("90210", 50)
        assert id2 == id1 + 1

    def test_get_run(self, db):
        run_id = db.create_pipeline_run("10013", 25, config_json='{"demo": true}')
        run = db.get_pipeline_run(run_id)
        assert run is not None
        assert run["postal_code"] == "10013"
        assert run["distance"] == 25
        assert run["status"] == "running"

    def test_update_run_status(self, db):
        run_id = db.create_pipeline_run("10013", 25)
        db.update_pipeline_run(run_id, status="completed", total_scraped=71)
        run = db.get_pipeline_run(run_id)
        assert run["status"] == "completed"
        assert run["total_scraped"] == 71
        assert run["completed_at"] is not None

    def test_list_runs(self, db):
        db.create_pipeline_run("10013", 25)
        db.create_pipeline_run("90210", 50)
        runs = db.list_pipeline_runs()
        assert len(runs) == 2
        # Most recent first
        assert runs[0]["postal_code"] == "90210"

    def test_get_nonexistent_run(self, db):
        assert db.get_pipeline_run(999) is None


# ---------------------------------------------------------------------------
# Contractor UPSERT
# ---------------------------------------------------------------------------


class TestContractorUpsert:
    def test_insert_contractor(self, db):
        c = _make_contractor()
        count = db.upsert_contractors([c])
        assert count == 1

    def test_upsert_updates_existing(self, db):
        c1 = _make_contractor(rating=4.5)
        db.upsert_contractors([c1])

        c2 = _make_contractor(rating=4.9)
        db.upsert_contractors([c2])

        result = db.get_contractor("100")
        assert result["rating"] == 4.9

    def test_coalesce_preserves_existing_data(self, db):
        c1 = _make_contractor(website="https://test.com", description="Great company")
        db.upsert_contractors([c1])

        # Second upsert with None website — should preserve existing
        c2 = _make_contractor(website=None, description=None)
        db.upsert_contractors([c2])

        result = db.get_contractor("100")
        assert result["website"] == "https://test.com"
        assert result["description"] == "Great company"

    def test_certifications_stored(self, db):
        c = _make_contractor(
            certifications=[
                Certification(name="GAF Master Elite"),
                Certification(name="President's Club Award", type="award"),
            ]
        )
        db.upsert_contractors([c])
        result = db.get_contractor("100")
        assert len(result["certifications"]) == 2

    def test_skip_contractors_without_id(self, db):
        c = Contractor(name="No ID Corp", contractor_id=None)
        count = db.upsert_contractors([c])
        assert count == 0

    def test_multiple_contractors(self, db):
        contractors = [
            _make_contractor("1", "Alpha Roofing"),
            _make_contractor("2", "Beta Roofing"),
            _make_contractor("3", "Gamma Roofing"),
        ]
        count = db.upsert_contractors(contractors)
        assert count == 3


# ---------------------------------------------------------------------------
# Search & queries
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_all(self, db):
        db.upsert_contractors([
            _make_contractor("1", "Alpha", state="NJ", rating=4.5),
            _make_contractor("2", "Beta", state="NY", rating=4.9),
        ])
        results = db.search_contractors()
        assert len(results) == 2

    def test_search_by_state(self, db):
        db.upsert_contractors([
            _make_contractor("1", "Alpha", state="NJ"),
            _make_contractor("2", "Beta", state="NY"),
            _make_contractor("3", "Gamma", state="NJ"),
        ])
        results = db.search_contractors(state="NJ")
        assert len(results) == 2

    def test_search_by_min_rating(self, db):
        db.upsert_contractors([
            _make_contractor("1", "Low", rating=3.5),
            _make_contractor("2", "High", rating=4.9),
        ])
        results = db.search_contractors(min_rating=4.0)
        assert len(results) == 1
        assert results[0]["name"] == "High"

    def test_search_limit(self, db):
        for i in range(10):
            db.upsert_contractors([_make_contractor(str(i), f"Corp {i}")])
        results = db.search_contractors(limit=3)
        assert len(results) == 3

    def test_get_contractor_with_insight(self, db):
        c = _make_contractor()
        db.upsert_contractors([c])

        report = SalesIntelligenceReport(
            market=MarketInsight(),
            contractors=[
                ContractorInsight(
                    contractor_id="100",
                    contractor_name="Test Roofing",
                    sales_readiness_score=8,
                    summary="A strong lead",
                ),
            ],
        )
        db.upsert_insights(report)

        result = db.get_contractor("100")
        assert result["insight"] is not None
        assert result["insight"]["sales_readiness_score"] == 8


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty_stats(self, db):
        stats = db.get_stats()
        assert stats["total_contractors"] == 0
        assert stats["contractors_with_insights"] == 0
        assert stats["total_pipeline_runs"] == 0

    def test_stats_after_insert(self, db):
        db.create_pipeline_run("10013", 25)
        db.upsert_contractors([
            _make_contractor("1", "A"),
            _make_contractor("2", "B"),
        ])
        stats = db.get_stats()
        assert stats["total_contractors"] == 2
        assert stats["total_pipeline_runs"] == 1
