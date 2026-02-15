"""
Sales Intelligence Engine
=========================

Transforms raw scraped contractor data into actionable sales intelligence
using the OpenAI ChatGPT API.

Pipeline:
  1. Compute local analytics (rating distributions, geographic clusters, etc.)
  2. Build structured prompts with full market context
  3. Call GPT to generate per-contractor insights + market-level analysis
  4. Parse structured JSON responses into Pydantic models
  5. Store the enriched intelligence report

Design notes:
  - Contractors are batched to give GPT comparative market context
  - Structured JSON output (`response_format`) is enforced for reliable parsing
  - Web-enrichment summaries are injected into prompts when available
  - Token-efficient: one call per batch for contractor insights, one for market
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections import Counter
from typing import Any, Optional

from openai import OpenAI

from models import (
    Contractor,
    ContractorInsight,
    MarketInsight,
    SalesIntelligenceReport,
    ScrapingResult,
    TalkingPoint,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "gpt-4o-mini"
BATCH_SIZE = 15  # contractors per GPT call (fits context window comfortably)

# Certification tier ordering (highest first)
CERT_TIERS = [
    "President's Club Award",
    "GAF Master Elite",
    "GAF Certified Plus",
    "GAF Certified",
]


# ---------------------------------------------------------------------------
# Local pre-analytics (no API call needed)
# ---------------------------------------------------------------------------


def compute_local_analytics(contractors: list[Contractor]) -> dict[str, Any]:
    """
    Derive statistics from the scraped data before sending to GPT.
    This gives the LLM concrete numbers to reason about.
    """
    ratings = [c.rating for c in contractors if c.rating is not None]
    review_counts = [c.review_count for c in contractors if c.review_count is not None]

    # Certification distribution
    cert_counter: Counter[str] = Counter()
    for c in contractors:
        for cert in c.certifications:
            # Normalize name (strip ® ™)
            name = cert.name.replace("®", "").replace("™", "").strip()
            cert_counter[name] += 1

    # Geographic distribution
    state_counter: Counter[str] = Counter()
    city_counter: Counter[str] = Counter()
    for c in contractors:
        if c.state:
            state_counter[c.state] += 1
        if c.city and c.state:
            city_counter[f"{c.city}, {c.state}"] += 1

    # Top performers by review volume
    by_reviews = sorted(
        [c for c in contractors if c.review_count],
        key=lambda c: c.review_count or 0,
        reverse=True,
    )
    # Top performers by rating (with minimum review threshold)
    by_rating = sorted(
        [c for c in contractors if (c.rating or 0) > 0 and (c.review_count or 0) >= 20],
        key=lambda c: (c.rating or 0, c.review_count or 0),
        reverse=True,
    )

    return {
        "total_contractors": len(contractors),
        "avg_rating": round(statistics.mean(ratings), 2) if ratings else None,
        "median_rating": round(statistics.median(ratings), 2) if ratings else None,
        "min_rating": min(ratings) if ratings else None,
        "max_rating": max(ratings) if ratings else None,
        "avg_review_count": round(statistics.mean(review_counts), 1)
        if review_counts
        else None,
        "median_review_count": round(statistics.median(review_counts), 1)
        if review_counts
        else None,
        "total_reviews": sum(review_counts) if review_counts else 0,
        "certification_distribution": dict(cert_counter.most_common()),
        "state_distribution": dict(state_counter.most_common()),
        "city_distribution": dict(city_counter.most_common(10)),
        "top_by_reviews": [
            f"{c.name} ({c.review_count} reviews, {c.rating} stars)"
            for c in by_reviews[:5]
        ],
        "top_by_rating": [
            f"{c.name} ({c.rating} stars, {c.review_count} reviews)"
            for c in by_rating[:5]
        ],
    }


def _contractor_to_prompt_block(c: Contractor, web_context: str = "") -> str:
    """Serialize a contractor into a compact text block for the prompt."""
    certs = ", ".join(c.certification_names) if c.certifications else "None listed"
    lines = [
        f"CONTRACTOR: {c.name}",
        f"  ID: {c.contractor_id or 'N/A'}",
        f"  Location: {c.city or '?'}, {c.state or '?'} {c.postal_code or ''}",
        f"  Phone: {c.phone or 'N/A'}",
        f"  Website: {c.website or 'N/A'}",
        f"  Rating: {c.rating or 'N/A'} ({c.review_count or 0} reviews)",
        f"  Certifications: {certs}",
    ]
    if c.years_in_business:
        lines.append(f"  Years in business: {c.years_in_business}")
    if c.number_of_employees:
        lines.append(f"  Employees: {c.number_of_employees}")
    if c.description:
        desc = c.description[:500] + ("..." if len(c.description) > 500 else "")
        lines.append(f"  Description: {desc}")
    if web_context:
        lines.append(f"  Additional web context: {web_context}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def _build_contractor_insights_prompt(
    contractors: list[Contractor],
    analytics: dict[str, Any],
    web_contexts: dict[str, str],
) -> list[dict[str, str]]:
    """Build the prompt messages for per-contractor insight generation."""
    contractor_blocks = "\n\n".join(
        _contractor_to_prompt_block(c, web_contexts.get(c.contractor_id or "", ""))
        for c in contractors
    )

    system_msg = """\
You are a sales intelligence analyst specializing in the roofing and home improvement industry. \
You help B2B sales representatives prepare for outreach to roofing contractors.

Your outputs must be actionable, concise, and grounded in the data provided. \
When data is limited, make reasonable inferences but flag uncertainty. \
Use industry knowledge about GAF certifications, roofing market dynamics, \
seasonal patterns, and contractor business models to enrich your analysis.

GAF CERTIFICATION HIERARCHY (highest to lowest):
1. President's Club Award — elite top-tier, strongest warranties, highest standards
2. GAF Master Elite® — top 2% of contractors nationally, Golden Pledge warranty
3. GAF Certified Plus™ — mid-tier, Silver Pledge warranty
4. GAF Certified™ — entry-level GAF certification, System Plus warranty

IMPORTANT: Return ONLY valid JSON matching the exact schema specified."""

    user_msg = f"""\
Analyze these {len(contractors)} roofing contractors and generate sales intelligence for each one.

MARKET CONTEXT:
- Region: contractors within {analytics.get('total_contractors', '?')} results
- Average rating: {analytics.get('avg_rating', 'N/A')}
- Average review count: {analytics.get('avg_review_count', 'N/A')}
- Certification distribution: {json.dumps(analytics.get('certification_distribution', {}))}
- Geographic clusters: {json.dumps(analytics.get('city_distribution', {}))}
- Top performers by volume: {json.dumps(analytics.get('top_by_reviews', []))}

CONTRACTOR DATA:
{contractor_blocks}

For EACH contractor, produce a JSON object with these fields:
{{
  "contractor_id": "<their ID>",
  "contractor_name": "<their name>",
  "summary": "<2-3 sentence executive brief — who they are, what makes them notable, key sales angle>",
  "industry_tags": ["<tag1>", "<tag2>", ...],
  "business_size_estimate": "<Micro|Small|Mid-size|Large>",
  "talking_points": [
    {{
      "hook": "<conversation opener>",
      "detail": "<supporting data or talking point>",
      "source": "<where this insight comes from>"
    }}
  ],
  "pain_points": ["<likely challenge 1>", ...],
  "sales_readiness_score": <1-10>,
  "sales_readiness_rationale": "<why this score>",
  "competitive_position": "<how they compare to nearby competitors>",
  "opportunity_signals": ["<signal 1>", ...],
  "recommended_actions": ["<action 1>", ...]
}}

Return a JSON object: {{"contractors": [<array of contractor insight objects>]}}

Be specific and grounded. Reference actual data (ratings, review counts, certifications, location). \
Provide at least 3 talking points and 2 recommended actions per contractor. \
The talking points should be things a sales rep can actually say in a cold call or email."""

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def _build_market_insights_prompt(
    contractors: list[Contractor],
    analytics: dict[str, Any],
) -> list[dict[str, str]]:
    """Build prompt for market-level analysis."""
    names_by_cert: dict[str, list[str]] = {}
    for c in contractors:
        for cert in c.certifications:
            name = cert.name.replace("®", "").replace("™", "").strip()
            names_by_cert.setdefault(name, []).append(c.name)

    system_msg = """\
You are a sales intelligence analyst specializing in the roofing industry. \
You produce market-level intelligence reports that help sales teams prioritize territories and accounts.

IMPORTANT: Return ONLY valid JSON matching the exact schema specified."""

    user_msg = f"""\
Analyze this roofing contractor market and produce a strategic market intelligence report.

MARKET DATA:
- Total contractors: {analytics['total_contractors']}
- Average rating: {analytics.get('avg_rating', 'N/A')} (median: {analytics.get('median_rating', 'N/A')})
- Average reviews: {analytics.get('avg_review_count', 'N/A')} (median: {analytics.get('median_review_count', 'N/A')})
- Total reviews across market: {analytics.get('total_reviews', 0)}
- Rating range: {analytics.get('min_rating', 'N/A')} – {analytics.get('max_rating', 'N/A')}
- Certification distribution: {json.dumps(analytics.get('certification_distribution', {}))}
- State distribution: {json.dumps(analytics.get('state_distribution', {}))}
- City clusters: {json.dumps(analytics.get('city_distribution', {}))}
- Top by review volume: {json.dumps(analytics.get('top_by_reviews', []))}
- Top by rating: {json.dumps(analytics.get('top_by_rating', []))}

CONTRACTORS BY CERTIFICATION:
{json.dumps(names_by_cert, indent=2)}

Return a JSON object:
{{
  "market_summary": "<3-5 sentence overview of the competitive landscape, key trends, market maturity>",
  "geographic_clusters": ["<notable cluster with reasoning>", ...],
  "top_performers": ["<name — why they stand out>", ...],
  "market_opportunities": ["<gap or opportunity for sales team>", ...],
  "competitive_landscape": "<paragraph analyzing competitive dynamics, certification concentration, market saturation>"
}}

Be specific. Reference numbers. Identify actionable patterns for a sales team."""

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


# ---------------------------------------------------------------------------
# GPT API calls
# ---------------------------------------------------------------------------


def _call_gpt(
    client: OpenAI,
    messages: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.4,
) -> dict[str, Any]:
    """
    Call the OpenAI Chat API and return parsed JSON.
    Uses response_format=json_object for reliable structured output.
    """
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content or "{}"
    logger.debug("GPT raw response (%d chars): %s...", len(content), content[:200])

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse GPT response as JSON: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SalesIntelligenceEngine:
    """
    Orchestrates the full intelligence pipeline:
      scraped data -> local analytics -> web enrichment -> GPT -> structured insights

    Usage:
        engine = SalesIntelligenceEngine(api_key="sk-...")
        report = engine.generate(scraping_result)
        report.to_json("output/insights.json")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "OpenAI API key required. Set OPENAI_API_KEY env var or pass api_key=."
            )
        self.client = OpenAI(api_key=self.api_key)
        self.model = model
        self.batch_size = batch_size

    def generate(
        self,
        data: ScrapingResult,
        web_contexts: Optional[dict[str, str]] = None,
    ) -> SalesIntelligenceReport:
        """
        Generate a full sales intelligence report from scraped data.

        Args:
            data: ScrapingResult with contractor list
            web_contexts: Optional dict mapping contractor_id -> web search summary

        Returns:
            SalesIntelligenceReport with market + per-contractor insights
        """
        contractors = data.contractors
        if not contractors:
            logger.warning("No contractors to analyze")
            return SalesIntelligenceReport()

        web_contexts = web_contexts or {}

        # Phase 1: Local analytics
        logger.info("Computing local analytics for %d contractors ...", len(contractors))
        analytics = compute_local_analytics(contractors)

        # Phase 2: Market-level insights (one call)
        logger.info("Generating market-level insights ...")
        market = self._generate_market_insights(contractors, analytics)
        market.region = (
            f"{data.search_postal_code} — {data.search_distance} mi radius"
        )
        market.certification_distribution = analytics.get(
            "certification_distribution", {}
        )
        market.avg_rating = analytics.get("avg_rating")
        market.avg_review_count = analytics.get("avg_review_count")

        # Phase 3: Per-contractor insights (batched)
        logger.info("Generating per-contractor insights ...")
        contractor_insights = self._generate_contractor_insights(
            contractors, analytics, web_contexts
        )

        return SalesIntelligenceReport(
            market=market,
            contractors=contractor_insights,
            source_file=f"contractors_{data.search_postal_code}.json",
        )

    def _generate_market_insights(
        self,
        contractors: list[Contractor],
        analytics: dict[str, Any],
    ) -> MarketInsight:
        """Generate market-level analysis via GPT."""
        messages = _build_market_insights_prompt(contractors, analytics)
        raw = _call_gpt(self.client, messages, model=self.model)

        return MarketInsight(
            market_summary=raw.get("market_summary", ""),
            geographic_clusters=raw.get("geographic_clusters", []),
            top_performers=raw.get("top_performers", []),
            market_opportunities=raw.get("market_opportunities", []),
            competitive_landscape=raw.get("competitive_landscape", ""),
        )

    def _generate_contractor_insights(
        self,
        contractors: list[Contractor],
        analytics: dict[str, Any],
        web_contexts: dict[str, str],
    ) -> list[ContractorInsight]:
        """Generate per-contractor insights, batching to fit context window."""
        all_insights: list[ContractorInsight] = []

        for i in range(0, len(contractors), self.batch_size):
            batch = contractors[i : i + self.batch_size]
            logger.info(
                "  Batch %d: contractors %d–%d of %d",
                i // self.batch_size + 1,
                i + 1,
                min(i + self.batch_size, len(contractors)),
                len(contractors),
            )

            messages = _build_contractor_insights_prompt(
                batch, analytics, web_contexts
            )
            raw = _call_gpt(self.client, messages, model=self.model)

            # Parse the response
            items = raw.get("contractors", [])
            if not items and isinstance(raw, list):
                items = raw

            for item in items:
                try:
                    talking_points = [
                        TalkingPoint(**tp)
                        for tp in item.get("talking_points", [])
                    ]
                    insight = ContractorInsight(
                        contractor_id=str(item.get("contractor_id", "")),
                        contractor_name=item.get("contractor_name", ""),
                        summary=item.get("summary", ""),
                        industry_tags=item.get("industry_tags", []),
                        business_size_estimate=item.get(
                            "business_size_estimate", ""
                        ),
                        talking_points=talking_points,
                        pain_points=item.get("pain_points", []),
                        sales_readiness_score=item.get(
                            "sales_readiness_score", 0
                        ),
                        sales_readiness_rationale=item.get(
                            "sales_readiness_rationale", ""
                        ),
                        competitive_position=item.get(
                            "competitive_position", ""
                        ),
                        opportunity_signals=item.get(
                            "opportunity_signals", []
                        ),
                        recommended_actions=item.get(
                            "recommended_actions", []
                        ),
                    )
                    all_insights.append(insight)
                except Exception as e:
                    logger.warning(
                        "Failed to parse insight for %s: %s",
                        item.get("contractor_name", "?"),
                        e,
                    )

        return all_insights


# ---------------------------------------------------------------------------
# Web enrichment (optional — uses OpenAI for summarisation)
# ---------------------------------------------------------------------------


def enrich_with_web_search(
    contractors: list[Contractor],
    client: OpenAI,
    model: str = DEFAULT_MODEL,
) -> dict[str, str]:
    """
    For each contractor, ask GPT to produce a brief business context summary
    based on what it knows about the company and its region.

    This is a lightweight alternative to actual web scraping — it uses GPT's
    training data knowledge as a proxy for web search results.

    Returns: dict mapping contractor_id -> context summary string
    """
    contexts: dict[str, str] = {}

    for c in contractors:
        cid = c.contractor_id or ""
        prompt = f"""\
Provide a brief 2-3 sentence business context summary for this roofing contractor. \
Include any knowledge about their market area, typical services, competitive environment, \
and the local housing market. If you don't have specific knowledge, provide general \
context about a roofing contractor of this profile in their region.

Company: {c.name}
Location: {c.city}, {c.state} {c.postal_code}
Rating: {c.rating} ({c.review_count} reviews)
Certifications: {', '.join(c.certification_names)}
{f'Description: {c.description[:300]}' if c.description else ''}

Return ONLY the summary text, no JSON."""

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            contexts[cid] = response.choices[0].message.content or ""
        except Exception as e:
            logger.warning("Web enrichment failed for %s: %s", c.name, e)
            contexts[cid] = ""

    return contexts
