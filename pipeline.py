"""
Pipeline Orchestrator
=====================

Single entry point that runs the full end-to-end flow:

    scrape → store → enrich → store insights → export

Each stage writes to the database so the pipeline is resumable and
every run is auditable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import ScraperConfig
from database import Database
from models import ScrapingResult, SalesIntelligenceReport

logger = logging.getLogger(__name__)


class Pipeline:
    """
    Orchestrates scrape → store → enrich → store → export.

    Usage:
        pipeline = Pipeline(
            postal_code="10013",
            distance=25,
            db_path="gaf_contractors.db",
            openai_api_key="sk-...",   # optional — skips enrichment if None
        )
        result = pipeline.run()
    """

    def __init__(
        self,
        postal_code: str = "10013",
        distance: int = 25,
        db_path: str = "gaf_contractors.db",
        output_dir: str = "output",
        openai_api_key: Optional[str] = None,
        openai_model: str = "gpt-4o-mini",
        max_contractors: int = 0,
        headless: bool = True,
        skip_profiles: bool = True,
        web_enrich: bool = False,
        demo_mode: bool = False,
    ) -> None:
        self.postal_code = postal_code
        self.distance = distance
        self.output_dir = output_dir
        self.openai_api_key = openai_api_key
        self.openai_model = openai_model
        self.max_contractors = max_contractors
        self.headless = headless
        self.skip_profiles = skip_profiles
        self.web_enrich = web_enrich
        self.demo_mode = demo_mode

        self.db = Database(db_path)
        self.db.initialize()

        self.scraper_config = ScraperConfig(
            postal_code=postal_code,
            distance=distance,
            headless=headless,
            max_contractors=max_contractors,
            scrape_profiles=not skip_profiles,
            output_dir=output_dir,
        )

    def run(self) -> dict:
        """
        Execute the full pipeline synchronously.
        Returns a summary dict with run_id, counts, and paths.
        """
        run_id = self.db.create_pipeline_run(
            self.postal_code,
            self.distance,
            config_json=json.dumps(asdict(self.scraper_config)),
        )
        logger.info("Pipeline run #%d started", run_id)

        result_summary = {
            "run_id": run_id,
            "postal_code": self.postal_code,
            "distance": self.distance,
        }

        try:
            # ── Stage 1: Scrape ──
            logger.info("=" * 60)
            logger.info("STAGE 1: Scraping contractors ...")
            logger.info("=" * 60)

            scraping_result = asyncio.run(self._scrape())

            self.db.update_pipeline_run(
                run_id,
                total_found=scraping_result.total_found,
                total_scraped=len(scraping_result.contractors),
                errors=scraping_result.errors,
            )
            result_summary["total_found"] = scraping_result.total_found
            result_summary["total_scraped"] = len(scraping_result.contractors)

            # ── Stage 2: Store scraped data ──
            logger.info("=" * 60)
            logger.info("STAGE 2: Storing %d contractors in database ...",
                        len(scraping_result.contractors))
            logger.info("=" * 60)

            stored = self.db.upsert_contractors(
                scraping_result.contractors, run_id
            )
            result_summary["total_stored"] = stored

            # Export to files
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out_dir = Path(self.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            json_path = out_dir / f"contractors_{self.postal_code}_{ts}.json"
            csv_path = out_dir / f"contractors_{self.postal_code}_{ts}.csv"
            scraping_result.to_json(json_path)
            scraping_result.to_csv(csv_path)
            result_summary["contractors_json"] = str(json_path)
            result_summary["contractors_csv"] = str(csv_path)

            # ── Stage 3: Enrich with AI ──
            logger.info("=" * 60)
            logger.info("STAGE 3: Generating sales intelligence ...")
            logger.info("=" * 60)

            report = self._enrich(scraping_result)
            result_summary["total_enriched"] = len(report.contractors)

            # ── Stage 4: Store insights ──
            logger.info("=" * 60)
            logger.info("STAGE 4: Storing insights in database ...")
            logger.info("=" * 60)

            self.db.upsert_insights(report, run_id)

            insights_path = out_dir / f"sales_intelligence_{self.postal_code}_{ts}.json"
            report.to_json(insights_path)
            result_summary["insights_json"] = str(insights_path)

            # ── Done ──
            self.db.update_pipeline_run(
                run_id,
                status="completed",
                total_enriched=len(report.contractors),
            )
            result_summary["status"] = "completed"
            logger.info("=" * 60)
            logger.info("Pipeline run #%d completed successfully", run_id)
            logger.info("=" * 60)

        except Exception as e:
            logger.error("Pipeline failed: %s", e, exc_info=True)
            self.db.update_pipeline_run(
                run_id,
                status="failed",
                errors=[str(e)],
            )
            result_summary["status"] = "failed"
            result_summary["error"] = str(e)

        return result_summary

    async def _scrape(self) -> ScrapingResult:
        from scraper import GAFScraper

        async with GAFScraper(self.scraper_config) as scraper:
            return await scraper.run()

    def _enrich(self, scraping_result: ScrapingResult) -> SalesIntelligenceReport:
        """Run the enrichment stage (GPT or demo mode)."""
        if self.demo_mode or not self.openai_api_key:
            if not self.openai_api_key:
                logger.info("No OpenAI API key — using demo enrichment")
            return self._demo_enrich(scraping_result)

        from insights import SalesIntelligenceEngine, enrich_with_web_search

        engine = SalesIntelligenceEngine(
            api_key=self.openai_api_key, model=self.openai_model
        )

        web_contexts: dict[str, str] = {}
        if self.web_enrich:
            logger.info("Running web-context enrichment ...")
            web_contexts = enrich_with_web_search(
                scraping_result.contractors, engine.client, model=self.openai_model
            )

        return engine.generate(scraping_result, web_contexts=web_contexts)

    def _demo_enrich(self, data: ScrapingResult) -> SalesIntelligenceReport:
        """Demo enrichment using local analytics + heuristics."""
        from insights import compute_local_analytics
        from models import (
            ContractorInsight,
            MarketInsight,
            SalesIntelligenceReport,
            TalkingPoint,
        )

        analytics = compute_local_analytics(data.contractors)
        total = analytics["total_contractors"]
        avg_r = analytics.get("avg_rating", 0)
        avg_rev = analytics.get("avg_review_count", 0)
        cert_dist = analytics.get("certification_distribution", {})
        state_dist = analytics.get("state_distribution", {})
        top_cities = list(analytics.get("city_distribution", {}).keys())
        states = ", ".join(state_dist.keys())

        market = MarketInsight(
            region=f"{data.search_postal_code} — {data.search_distance} mi radius",
            market_summary=(
                f"This market contains {total} GAF-certified contractors across {states}. "
                f"The average rating is {avg_r} stars with {avg_rev:.0f} average reviews. "
                f"Certification distribution: {', '.join(f'{k}: {v}' for k, v in cert_dist.items())}."
            ),
            certification_distribution=cert_dist,
            geographic_clusters=[
                f"Key clusters: {', '.join(top_cities[:5])}",
                f"State split: {', '.join(f'{k}: {v}' for k, v in state_dist.items())}",
            ],
            avg_rating=avg_r,
            avg_review_count=avg_rev,
            top_performers=analytics.get("top_by_reviews", [])[:5],
            market_opportunities=[
                "Review volume varies widely — lower-review contractors may need marketing support",
                "Geographic coverage gaps may exist in underserved areas",
            ],
            competitive_landscape=(
                f"Market of {total} contractors across {states}. "
                f"Average rating {avg_r}, indicating a high-quality, competitive landscape."
            ),
        )

        contractor_insights = []
        for c in data.contractors:
            reviews = c.review_count or 0
            rating = c.rating or 0
            score = min(10, 5
                        + (2 if reviews >= 300 else 1 if reviews >= 100 else 0)
                        + (1 if rating >= 5.0 else 0)
                        + (1 if any("President" in cert.name for cert in c.certifications) else 0))

            size = "Mid-size" if reviews >= 400 else "Small" if reviews >= 150 else "Micro"
            tags = ["Residential Roofing"]
            nl = c.name.lower()
            if any(w in nl for w in ("siding", "exterior", "home improvement")):
                tags.append("Multi-Service Exterior")

            tps = [
                TalkingPoint(
                    hook=f"Your {rating}-star rating across {reviews} reviews stands out",
                    detail=f"That's {'above' if rating >= avg_r else 'in line with'} the market average of {avg_r}.",
                    source="GAF profile data",
                ),
                TalkingPoint(
                    hook=f"The {c.city}, {c.state} market has {total} GAF contractors",
                    detail="Differentiation requires more than certification — we can help with visibility.",
                    source="Market analysis",
                ),
            ]

            ci = ContractorInsight(
                contractor_id=c.contractor_id or "",
                contractor_name=c.name,
                summary=f"{c.name} is a {size.lower()} GAF contractor in {c.city}, {c.state} with {rating} stars across {reviews} reviews.",
                industry_tags=tags,
                business_size_estimate=size,
                talking_points=tps,
                pain_points=["Standing out in a competitive, high-quality market"],
                sales_readiness_score=score,
                sales_readiness_rationale=f"Score {score}/10 based on {reviews} reviews, {rating} rating, certification tier.",
                competitive_position=f"{'Leader' if reviews >= 300 else 'Contender'} in {c.city} by review volume.",
                opportunity_signals=[f"Located in {c.city}, {c.state}"],
                recommended_actions=[f"Call {c.phone} with local market data", "Offer visibility / lead-gen solutions"],
            )
            contractor_insights.append(ci)

        return SalesIntelligenceReport(
            market=market,
            contractors=contractor_insights,
            source_file=f"pipeline_run",
        )
