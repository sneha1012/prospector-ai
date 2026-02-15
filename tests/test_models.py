"""
Unit tests for Pydantic data models.

Tests cover model creation, validation, serialization, default values,
computed properties, and export functionality.
"""

import json
import tempfile
from pathlib import Path

import pytest

from models import (
    Certification,
    Contractor,
    ContractorInsight,
    MarketInsight,
    Review,
    SalesIntelligenceReport,
    ScrapingResult,
    TalkingPoint,
)


# ---------------------------------------------------------------------------
# Certification model
# ---------------------------------------------------------------------------


class TestCertification:
    def test_create_certification(self):
        cert = Certification(name="GAF Master Elite", type="certification")
        assert cert.name == "GAF Master Elite"
        assert cert.type == "certification"

    def test_default_type(self):
        cert = Certification(name="GAF Certified")
        assert cert.type == "certification"

    def test_award_type(self):
        cert = Certification(name="President's Club Award", type="award")
        assert cert.type == "award"


# ---------------------------------------------------------------------------
# Contractor model
# ---------------------------------------------------------------------------


class TestContractor:
    def test_create_minimal_contractor(self):
        c = Contractor(name="Test Roofing LLC")
        assert c.name == "Test Roofing LLC"
        assert c.contractor_id is None
        assert c.certifications == []
        assert c.country == "US"

    def test_create_full_contractor(self):
        c = Contractor(
            contractor_id="12345",
            name="Elite Roofing Inc",
            city="Wayne",
            state="NJ",
            postal_code="07470",
            phone="(973) 555-1234",
            rating=4.9,
            review_count=250,
            certifications=[
                Certification(name="GAF Master Elite"),
                Certification(name="President's Club Award", type="award"),
            ],
        )
        assert c.contractor_id == "12345"
        assert c.rating == 4.9
        assert c.review_count == 250
        assert len(c.certifications) == 2

    def test_certification_names_property(self):
        c = Contractor(
            name="Test",
            certifications=[
                Certification(name="GAF Master Elite"),
                Certification(name="President's Club Award", type="award"),
            ],
        )
        assert c.certification_names == ["GAF Master Elite", "President's Club Award"]

    def test_empty_certification_names(self):
        c = Contractor(name="Test")
        assert c.certification_names == []

    def test_default_timestamps(self):
        c = Contractor(name="Test")
        assert c.scraped_at is not None
        assert len(c.scraped_at) > 0

    def test_default_lists(self):
        c = Contractor(name="Test")
        assert c.profile_photo_urls == []
        assert c.job_photo_urls == []
        assert c.reviews == []


# ---------------------------------------------------------------------------
# TalkingPoint model
# ---------------------------------------------------------------------------


class TestTalkingPoint:
    def test_create_talking_point(self):
        tp = TalkingPoint(
            hook="Your 5.0 rating is outstanding",
            detail="That puts you in the top tier in your market",
            source="GAF profile data",
        )
        assert tp.hook == "Your 5.0 rating is outstanding"
        assert tp.source == "GAF profile data"

    def test_default_source(self):
        tp = TalkingPoint(hook="Hook", detail="Detail")
        assert tp.source == "profile"


# ---------------------------------------------------------------------------
# ContractorInsight model
# ---------------------------------------------------------------------------


class TestContractorInsight:
    def test_create_insight(self):
        ci = ContractorInsight(
            contractor_id="12345",
            contractor_name="Test Roofing",
            summary="A top-tier contractor",
            sales_readiness_score=8,
            industry_tags=["Residential Roofing"],
            business_size_estimate="Small",
        )
        assert ci.sales_readiness_score == 8
        assert ci.industry_tags == ["Residential Roofing"]

    def test_defaults(self):
        ci = ContractorInsight(
            contractor_id="12345",
            contractor_name="Test",
        )
        assert ci.sales_readiness_score == 0
        assert ci.talking_points == []
        assert ci.pain_points == []
        assert ci.opportunity_signals == []
        assert ci.recommended_actions == []
        assert ci.generated_at is not None


# ---------------------------------------------------------------------------
# MarketInsight model
# ---------------------------------------------------------------------------


class TestMarketInsight:
    def test_create_market_insight(self):
        mi = MarketInsight(
            region="10013 — 25 mi radius",
            market_summary="A competitive market with 71 contractors",
            avg_rating=4.53,
            avg_review_count=135.0,
        )
        assert mi.region == "10013 — 25 mi radius"
        assert mi.avg_rating == 4.53

    def test_defaults(self):
        mi = MarketInsight()
        assert mi.region == ""
        assert mi.certification_distribution == {}
        assert mi.geographic_clusters == []


# ---------------------------------------------------------------------------
# ScrapingResult model
# ---------------------------------------------------------------------------


class TestScrapingResult:
    def test_create_empty_result(self):
        r = ScrapingResult()
        assert r.contractors == []
        assert r.total_found == 0
        assert r.errors == []

    def test_json_export(self):
        r = ScrapingResult(
            search_postal_code="10013",
            search_distance=25,
            total_found=2,
            contractors=[
                Contractor(name="A", contractor_id="1", rating=4.5),
                Contractor(name="B", contractor_id="2", rating=4.8),
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.json"
            r.to_json(path)
            assert path.exists()
            data = json.loads(path.read_text())
            assert len(data["contractors"]) == 2
            assert data["search_postal_code"] == "10013"

    def test_csv_export(self):
        r = ScrapingResult(
            contractors=[
                Contractor(name="Test Corp", contractor_id="99", rating=5.0),
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.csv"
            r.to_csv(path)
            assert path.exists()
            content = path.read_text()
            assert "Test Corp" in content
            assert "contractor_id" in content  # header row

    def test_csv_export_empty(self):
        r = ScrapingResult()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "empty.csv"
            r.to_csv(path)
            assert path.exists()
            assert path.read_text() == ""


# ---------------------------------------------------------------------------
# SalesIntelligenceReport model
# ---------------------------------------------------------------------------


class TestSalesIntelligenceReport:
    def test_create_report(self):
        report = SalesIntelligenceReport(
            market=MarketInsight(region="10013"),
            contractors=[
                ContractorInsight(contractor_id="1", contractor_name="A"),
                ContractorInsight(contractor_id="2", contractor_name="B"),
            ],
            source_file="test.json",
        )
        assert len(report.contractors) == 2
        assert report.market.region == "10013"

    def test_json_export(self):
        report = SalesIntelligenceReport(
            market=MarketInsight(region="10013", market_summary="Test"),
            contractors=[
                ContractorInsight(
                    contractor_id="1",
                    contractor_name="Test Corp",
                    sales_readiness_score=9,
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.json"
            report.to_json(path)
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["market"]["region"] == "10013"
            assert data["contractors"][0]["sales_readiness_score"] == 9

    def test_defaults(self):
        report = SalesIntelligenceReport()
        assert report.contractors == []
        assert report.market.region == ""
        assert report.generated_at is not None
