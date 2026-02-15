"""
Data models for GAF contractor information.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class Certification(BaseModel):
    """A GAF certification or award held by a contractor."""

    name: str = Field(description="Certification name, e.g. 'GAF Master Elite'")
    type: str = Field(
        default="certification",
        description="'certification' or 'award'",
    )


class Review(BaseModel):
    """A single customer review."""

    author: Optional[str] = None
    rating: Optional[float] = None
    date: Optional[str] = None
    text: Optional[str] = None


class Contractor(BaseModel):
    """Full representation of a GAF-certified roofing contractor."""

    # --- Identity ---
    contractor_id: Optional[str] = Field(
        default=None, description="GAF Contractor ID number"
    )
    name: str = Field(description="Business name")
    slug: Optional[str] = Field(
        default=None, description="URL slug from the profile page"
    )

    # --- Contact ---
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = "US"
    phone: Optional[str] = None
    website: Optional[str] = None

    # --- Profile ---
    description: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    years_in_business: Optional[str] = None
    number_of_employees: Optional[str] = None
    state_license_number: Optional[str] = None

    # --- Certifications ---
    certifications: list[Certification] = Field(default_factory=list)

    # --- Media ---
    profile_photo_urls: list[str] = Field(default_factory=list)
    job_photo_urls: list[str] = Field(default_factory=list)

    # --- Reviews ---
    reviews: list[Review] = Field(default_factory=list)

    # --- Metadata ---
    profile_url: Optional[str] = None
    scraped_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    search_postal_code: Optional[str] = None
    search_distance: Optional[int] = None

    @property
    def certification_names(self) -> list[str]:
        return [c.name for c in self.certifications]


# ---------------------------------------------------------------------------
# Sales Intelligence Models
# ---------------------------------------------------------------------------


class TalkingPoint(BaseModel):
    """A single sales engagement talking point."""

    hook: str = Field(description="Conversation opener / headline")
    detail: str = Field(description="Supporting detail or data point")
    source: str = Field(
        default="profile",
        description="Where this insight was derived from",
    )


class ContractorInsight(BaseModel):
    """AI-generated sales intelligence for a single contractor."""

    contractor_id: str
    contractor_name: str

    # --- Executive summary ---
    summary: str = Field(
        default="",
        description="2-3 sentence executive brief for a sales rep",
    )

    # --- Classification ---
    industry_tags: list[str] = Field(
        default_factory=list,
        description="e.g. ['Residential Roofing', 'Siding', 'Multi-Service Exterior']",
    )
    business_size_estimate: str = Field(
        default="",
        description="Micro / Small / Mid-size / Large estimate",
    )

    # --- Engagement intelligence ---
    talking_points: list[TalkingPoint] = Field(default_factory=list)
    pain_points: list[str] = Field(
        default_factory=list,
        description="Likely pain points or challenges this contractor faces",
    )

    # --- Sales readiness ---
    sales_readiness_score: int = Field(
        default=0,
        description="1-10 score of how ready this lead is for outreach",
    )
    sales_readiness_rationale: str = Field(
        default="",
        description="Why this score was assigned",
    )

    # --- Competitive positioning ---
    competitive_position: str = Field(
        default="",
        description="How this contractor is positioned vs. local competitors",
    )

    # --- Opportunity signals ---
    opportunity_signals: list[str] = Field(
        default_factory=list,
        description="Growth signals, expansion indicators, technology gaps",
    )

    # --- Recommended actions ---
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Concrete next-step actions for the sales rep",
    )

    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MarketInsight(BaseModel):
    """AI-generated market-level analysis for the searched region."""

    region: str = Field(default="", description="e.g. '10013 — New York metro, 25 mi'")

    market_summary: str = Field(
        default="",
        description="Overview of the contractor landscape in this region",
    )
    certification_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="Count of contractors per certification tier",
    )
    geographic_clusters: list[str] = Field(
        default_factory=list,
        description="Notable geographic concentrations",
    )
    avg_rating: Optional[float] = None
    avg_review_count: Optional[float] = None
    top_performers: list[str] = Field(
        default_factory=list,
        description="Names of the highest-rated / most-reviewed contractors",
    )
    market_opportunities: list[str] = Field(
        default_factory=list,
        description="Gaps or opportunities in this market",
    )
    competitive_landscape: str = Field(
        default="",
        description="AI analysis of competitive dynamics",
    )

    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SalesIntelligenceReport(BaseModel):
    """Full sales intelligence output combining market + contractor insights."""

    market: MarketInsight = Field(default_factory=MarketInsight)
    contractors: list[ContractorInsight] = Field(default_factory=list)
    source_file: str = ""
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2, ensure_ascii=False)
        return path


class ScrapingResult(BaseModel):
    """Container for a batch of scraped contractors."""

    contractors: list[Contractor] = Field(default_factory=list)
    search_postal_code: str = ""
    search_distance: int = 0
    total_found: int = 0
    scraped_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    errors: list[str] = Field(default_factory=list)

    def to_json(self, path: str | Path) -> Path:
        """Export results to JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2, ensure_ascii=False)
        return path

    def to_csv(self, path: str | Path) -> Path:
        """Export contractor data to a flat CSV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not self.contractors:
            path.write_text("")
            return path

        fieldnames = [
            "contractor_id",
            "name",
            "address",
            "city",
            "state",
            "postal_code",
            "country",
            "phone",
            "website",
            "rating",
            "review_count",
            "certifications",
            "years_in_business",
            "number_of_employees",
            "state_license_number",
            "description",
            "profile_url",
            "scraped_at",
            "search_postal_code",
            "search_distance",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for c in self.contractors:
                row = c.model_dump()
                # Flatten certifications to a pipe-separated string
                row["certifications"] = " | ".join(c.certification_names)
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        return path
