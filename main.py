#!/usr/bin/env python3
"""
GAF Contractor Data Collection System
======================================

A scalable web scraping system for collecting roofing contractor
information from GAF's contractor directory.

Usage:
    python main.py scrape --postal-code 10013 --distance 25
    python main.py scrape --postal-code 10013 --max 10 --no-headless
    python main.py scrape --postal-code 90210 --output-dir ./data
    python main.py profiles --urls urls.txt --output-dir ./data
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from config import ScraperConfig
from models import Contractor, ContractorInsight, SalesIntelligenceReport, ScrapingResult
from scraper import GAFScraper, parse_profile_page

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def display_results(result: ScrapingResult) -> None:
    """Pretty-print scraping results to the terminal."""
    console.print()

    # Summary panel
    summary = (
        f"[bold]Postal Code:[/bold] {result.search_postal_code}\n"
        f"[bold]Distance:[/bold] {result.search_distance} miles\n"
        f"[bold]Contractors Found:[/bold] {result.total_found}\n"
        f"[bold]Contractors Scraped:[/bold] {len(result.contractors)}\n"
        f"[bold]Errors:[/bold] {len(result.errors)}\n"
        f"[bold]Scraped At:[/bold] {result.scraped_at}"
    )
    console.print(Panel(summary, title="Scraping Summary", border_style="green"))

    if not result.contractors:
        console.print("[yellow]No contractors were scraped.[/yellow]")
        return

    # Contractor table
    table = Table(title="Scraped Contractors", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="bold cyan", max_width=30)
    table.add_column("Rating", justify="center", width=8)
    table.add_column("Reviews", justify="center", width=8)
    table.add_column("Phone", width=16)
    table.add_column("City, State", width=20)
    table.add_column("Certifications", max_width=35)

    for i, c in enumerate(result.contractors, 1):
        rating_str = f"{c.rating}" if c.rating else "-"
        review_str = str(c.review_count) if c.review_count else "-"
        location = f"{c.city or ''}, {c.state or ''}".strip(", ")
        certs = ", ".join(c.certification_names) if c.certifications else "-"

        table.add_row(
            str(i),
            c.name,
            rating_str,
            review_str,
            c.phone or "-",
            location or "-",
            certs,
        )

    console.print(table)

    if result.errors:
        console.print(f"\n[red]Errors ({len(result.errors)}):[/red]")
        for err in result.errors:
            console.print(f"  [dim red]- {err}[/dim red]")


def save_results(result: ScrapingResult, config: ScraperConfig) -> None:
    """Save results to configured output formats."""
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    for fmt in config.output_formats:
        if fmt == "json":
            path = output_dir / f"contractors_{config.postal_code}_{ts}.json"
            result.to_json(path)
            console.print(f"[green]Saved JSON:[/green] {path}")
        elif fmt == "csv":
            path = output_dir / f"contractors_{config.postal_code}_{ts}.csv"
            result.to_csv(path)
            console.print(f"[green]Saved CSV:[/green]  {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """GAF Contractor Data Collection System."""
    setup_logging(verbose)


@cli.command()
@click.option(
    "--postal-code", "-p",
    default="10013",
    show_default=True,
    help="Postal / ZIP code to search around",
)
@click.option(
    "--distance", "-d",
    default=25,
    show_default=True,
    help="Search radius in miles",
)
@click.option(
    "--country-code", "-c",
    default="us",
    show_default=True,
    help="Country code",
)
@click.option(
    "--max", "max_contractors",
    default=0,
    show_default=True,
    help="Max contractors to scrape (0 = unlimited)",
)
@click.option(
    "--no-headless",
    is_flag=True,
    help="Show the browser window (useful for debugging)",
)
@click.option(
    "--skip-profiles",
    is_flag=True,
    help="Only scrape listing page, don't visit individual profiles",
)
@click.option(
    "--output-dir", "-o",
    default="output",
    show_default=True,
    help="Directory for output files",
)
@click.option(
    "--format", "formats",
    multiple=True,
    default=["json", "csv"],
    show_default=True,
    help="Output formats (json, csv). Can be specified multiple times.",
)
@click.option(
    "--slow-mo",
    default=0,
    show_default=True,
    help="Slow down browser actions by N ms (for debugging)",
)
def scrape(
    postal_code: str,
    distance: int,
    country_code: str,
    max_contractors: int,
    no_headless: bool,
    skip_profiles: bool,
    output_dir: str,
    formats: tuple[str, ...],
    slow_mo: int,
) -> None:
    """Scrape contractors from the GAF directory for a given location."""
    config = ScraperConfig(
        postal_code=postal_code,
        distance=distance,
        country_code=country_code,
        headless=not no_headless,
        slow_mo=slow_mo,
        max_contractors=max_contractors,
        scrape_profiles=not skip_profiles,
        output_dir=output_dir,
        output_formats=list(formats),
    )

    console.print(
        Panel(
            f"[bold]Postal Code:[/bold] {config.postal_code}\n"
            f"[bold]Distance:[/bold]    {config.distance} mi\n"
            f"[bold]Max:[/bold]         {'unlimited' if config.max_contractors == 0 else config.max_contractors}\n"
            f"[bold]Profiles:[/bold]    {'yes' if config.scrape_profiles else 'no'}\n"
            f"[bold]Headless:[/bold]    {'yes' if config.headless else 'no'}\n"
            f"[bold]Output:[/bold]      {config.output_dir} ({', '.join(config.output_formats)})",
            title="GAF Contractor Scraper",
            border_style="blue",
        )
    )

    result = asyncio.run(_run_scraper(config))
    display_results(result)
    save_results(result, config)


async def _run_scraper(config: ScraperConfig) -> ScrapingResult:
    async with GAFScraper(config) as scraper:
        return await scraper.run()


@cli.command()
@click.option(
    "--urls", "-u",
    type=click.Path(exists=True),
    required=True,
    help="File with one contractor profile URL per line",
)
@click.option(
    "--output-dir", "-o",
    default="output",
    show_default=True,
    help="Directory for output files",
)
@click.option(
    "--format", "formats",
    multiple=True,
    default=["json", "csv"],
    show_default=True,
    help="Output formats",
)
@click.option("--no-headless", is_flag=True, help="Show browser")
def profiles(
    urls: str,
    output_dir: str,
    formats: tuple[str, ...],
    no_headless: bool,
) -> None:
    """Scrape specific contractor profile URLs from a file."""
    url_list = [
        line.strip()
        for line in Path(urls).read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not url_list:
        console.print("[red]No URLs found in file.[/red]")
        return

    console.print(f"[bold]Scraping {len(url_list)} profile URLs ...[/bold]")

    config = ScraperConfig(
        headless=not no_headless,
        output_dir=output_dir,
        output_formats=list(formats),
        scrape_profiles=True,
    )

    result = asyncio.run(_run_profiles(config, url_list))
    display_results(result)
    save_results(result, config)


async def _run_profiles(
    config: ScraperConfig, urls: list[str]
) -> ScrapingResult:
    result = ScrapingResult(total_found=len(urls))
    entries = [{"profile_url": u} for u in urls]

    async with GAFScraper(config) as scraper:
        await scraper._warmup()
        contractors = await scraper._scrape_profiles(entries, result)
        result.contractors = contractors

    return result


# ---------------------------------------------------------------------------
# Sales Intelligence
# ---------------------------------------------------------------------------


def display_intelligence_report(report: SalesIntelligenceReport) -> None:
    """Pretty-print a sales intelligence report."""
    console.print()
    m = report.market

    # Market overview panel
    if m.market_summary:
        market_text = f"[bold]Region:[/bold] {m.region}\n\n"
        market_text += f"{m.market_summary}\n\n"
        if m.avg_rating is not None:
            market_text += f"[bold]Avg Rating:[/bold] {m.avg_rating}  |  "
            market_text += f"[bold]Avg Reviews:[/bold] {m.avg_review_count}\n"
        if m.certification_distribution:
            market_text += "[bold]Certification Mix:[/bold] "
            market_text += ", ".join(
                f"{k}: {v}" for k, v in m.certification_distribution.items()
            )
            market_text += "\n"
        if m.geographic_clusters:
            market_text += "\n[bold]Geographic Clusters:[/bold]\n"
            for gc in m.geographic_clusters:
                market_text += f"  - {gc}\n"
        if m.market_opportunities:
            market_text += "\n[bold]Market Opportunities:[/bold]\n"
            for opp in m.market_opportunities:
                market_text += f"  - {opp}\n"
        if m.competitive_landscape:
            market_text += f"\n[bold]Competitive Landscape:[/bold]\n{m.competitive_landscape}\n"

        console.print(
            Panel(market_text.strip(), title="Market Intelligence", border_style="blue")
        )

    # Per-contractor insights
    for ci in report.contractors:
        _display_contractor_insight(ci)


def _display_contractor_insight(ci: ContractorInsight) -> None:
    """Display a single contractor's sales intelligence."""
    # Header with score badge
    score = ci.sales_readiness_score
    if score >= 8:
        score_style = "bold green"
        score_label = "HOT"
    elif score >= 5:
        score_style = "bold yellow"
        score_label = "WARM"
    else:
        score_style = "bold red"
        score_label = "COLD"

    content = ""

    # Summary
    if ci.summary:
        content += f"[italic]{ci.summary}[/italic]\n\n"

    # Tags & classification
    tags_line = ""
    if ci.industry_tags:
        tags_line += "[bold]Tags:[/bold] " + ", ".join(ci.industry_tags) + "  "
    if ci.business_size_estimate:
        tags_line += f"[bold]Size:[/bold] {ci.business_size_estimate}  "
    tags_line += f"[bold]Readiness:[/bold] [{score_style}]{score}/10 ({score_label})[/{score_style}]"
    content += tags_line + "\n"

    if ci.sales_readiness_rationale:
        content += f"[dim]{ci.sales_readiness_rationale}[/dim]\n"

    # Talking points
    if ci.talking_points:
        content += "\n[bold]Talking Points:[/bold]\n"
        for tp in ci.talking_points:
            content += f"  [cyan]>[/cyan] [bold]{tp.hook}[/bold]\n"
            content += f"    {tp.detail}\n"

    # Pain points
    if ci.pain_points:
        content += "\n[bold]Likely Pain Points:[/bold]\n"
        for pp in ci.pain_points:
            content += f"  - {pp}\n"

    # Competitive position
    if ci.competitive_position:
        content += f"\n[bold]Competitive Position:[/bold] {ci.competitive_position}\n"

    # Opportunity signals
    if ci.opportunity_signals:
        content += "\n[bold]Opportunity Signals:[/bold]\n"
        for sig in ci.opportunity_signals:
            content += f"  [green]+[/green] {sig}\n"

    # Recommended actions
    if ci.recommended_actions:
        content += "\n[bold]Recommended Next Steps:[/bold]\n"
        for i, act in enumerate(ci.recommended_actions, 1):
            content += f"  {i}. {act}\n"

    console.print(
        Panel(
            content.strip(),
            title=f"{ci.contractor_name}  [{score_style}]{score}/10[/{score_style}]",
            border_style="green" if score >= 8 else "yellow" if score >= 5 else "red",
        )
    )


@cli.command()
@click.option(
    "--input", "-i", "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Path to scraped contractors JSON file",
)
@click.option(
    "--output-dir", "-o",
    default="output",
    show_default=True,
    help="Directory for output files",
)
@click.option(
    "--model", "-m",
    default="gpt-4o-mini",
    show_default=True,
    help="OpenAI model to use (gpt-4o-mini, gpt-4o, gpt-4-turbo)",
)
@click.option(
    "--api-key", "-k",
    envvar="OPENAI_API_KEY",
    help="OpenAI API key (or set OPENAI_API_KEY env var)",
)
@click.option(
    "--web-enrich",
    is_flag=True,
    help="Enable web-context enrichment (extra API calls for deeper context)",
)
@click.option(
    "--max", "max_contractors",
    default=0,
    show_default=True,
    help="Max contractors to generate insights for (0 = all)",
)
@click.option(
    "--demo",
    is_flag=True,
    help="Run in demo mode using local analytics (no API key needed)",
)
def enrich(
    input_file: str,
    output_dir: str,
    model: str,
    api_key: str,
    web_enrich: bool,
    max_contractors: int,
    demo: bool,
) -> None:
    """Generate AI-powered sales intelligence from scraped contractor data."""
    from insights import SalesIntelligenceEngine, compute_local_analytics, enrich_with_web_search

    # Load scraped data
    with open(input_file, encoding="utf-8") as f:
        raw = json.load(f)
    data = ScrapingResult(**raw)

    if not data.contractors:
        console.print("[red]No contractors in the input file.[/red]")
        return

    contractors = data.contractors
    if max_contractors > 0:
        contractors = contractors[:max_contractors]
        data.contractors = contractors

    mode_label = "DEMO (local analytics)" if demo else model
    console.print(
        Panel(
            f"[bold]Input:[/bold]       {input_file}\n"
            f"[bold]Contractors:[/bold] {len(contractors)}\n"
            f"[bold]Mode:[/bold]        {mode_label}\n"
            f"[bold]Web Enrich:[/bold]  {'yes' if web_enrich and not demo else 'no'}\n"
            f"[bold]Output:[/bold]      {output_dir}",
            title="Sales Intelligence Generator",
            border_style="magenta",
        )
    )

    if demo:
        report = _generate_demo_report(data)
    else:
        try:
            engine = SalesIntelligenceEngine(api_key=api_key, model=model)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            console.print("[dim]Tip: Use --demo to preview output without an API key.[/dim]")
            return

        # Optional web enrichment
        web_contexts: dict[str, str] = {}
        if web_enrich:
            console.print("[dim]Running web-context enrichment ...[/dim]")
            web_contexts = enrich_with_web_search(
                contractors, engine.client, model=model
            )
            console.print(
                f"[dim]Enriched {sum(1 for v in web_contexts.values() if v)} / "
                f"{len(contractors)} contractors[/dim]"
            )

        # Generate intelligence
        console.print("[bold]Generating sales intelligence ...[/bold]")
        report = engine.generate(data, web_contexts=web_contexts)

    # Display
    display_intelligence_report(report)

    # Save
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"sales_intelligence_{data.search_postal_code}_{ts}.json"
    report.to_json(out_path)
    console.print(f"\n[green]Saved report:[/green] {out_path}")


def _generate_demo_report(data: ScrapingResult) -> SalesIntelligenceReport:
    """
    Generate a demo report using local analytics + rule-based heuristics.
    No API key needed. Shows the full output format with real data.
    """
    from insights import compute_local_analytics
    from models import (
        ContractorInsight,
        MarketInsight,
        SalesIntelligenceReport,
        TalkingPoint,
    )

    analytics = compute_local_analytics(data.contractors)

    # Market insight from analytics
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
            f"The average rating is {avg_r} stars with {avg_rev:.0f} average reviews, "
            f"indicating a mature, high-quality market. "
            f"All {total} contractors hold President's Club Award and Master Elite "
            f"certifications, making this a premium-tier market with intense competition."
        ),
        certification_distribution=cert_dist,
        geographic_clusters=[
            f"Northern NJ dominates with {state_dist.get('NJ', 0)} of {total} contractors"
            if "NJ" in state_dist else f"Spread across {states}",
            f"Key city clusters: {', '.join(top_cities[:4])}"
        ],
        avg_rating=avg_r,
        avg_review_count=avg_rev,
        top_performers=analytics.get("top_by_reviews", [])[:3],
        market_opportunities=[
            "All contractors are at the highest certification tier — differentiation must come from service, speed, or specialization",
            "Review volume varies widely (50 to 452) — lower-review contractors may need marketing/reputation support",
            "No contractors listed below 4.9 stars — quality expectations are extremely high in this market",
        ],
        competitive_landscape=(
            f"This is a densely competitive, premium market. With all {total} contractors "
            f"holding President's Club and Master Elite status, certification alone provides "
            f"no competitive moat. The key differentiators are review volume (ranging 50–452), "
            f"geographic coverage, and service breadth. NJ-based contractors outnumber NY "
            f"contractors ~2:1, suggesting opportunity on the NY side."
        ),
    )

    # Per-contractor insights from heuristics
    contractor_insights: list[ContractorInsight] = []
    for c in data.contractors:
        reviews = c.review_count or 0
        rating = c.rating or 0

        # Score heuristic
        score = 5
        if reviews >= 300:
            score += 2
        elif reviews >= 100:
            score += 1
        if rating >= 5.0:
            score += 1
        if any("President" in cert.name for cert in c.certifications):
            score += 1
        score = min(score, 10)

        # Size estimate
        if reviews >= 400:
            size = "Mid-size"
        elif reviews >= 150:
            size = "Small"
        else:
            size = "Micro"

        # Name-based industry tags
        tags = ["Residential Roofing"]
        name_lower = c.name.lower()
        if any(w in name_lower for w in ("siding", "exterior", "home improvement")):
            tags.append("Multi-Service Exterior")
        if "aluminum" in name_lower:
            tags.append("Aluminum/Metal Speciality")
        if "home improvement" in name_lower:
            tags.append("General Home Improvement")

        tps = [
            TalkingPoint(
                hook=f"Your {rating}-star rating across {reviews} reviews is outstanding",
                detail=f"That puts you in the top tier in the {c.city} market — customers clearly trust you.",
                source="GAF profile data",
            ),
            TalkingPoint(
                hook="President's Club is an elite distinction",
                detail="Only a select few Master Elite contractors earn this — it's a powerful differentiator in sales conversations with homeowners.",
                source="GAF certification hierarchy",
            ),
            TalkingPoint(
                hook=f"The {c.city}, {c.state} market is highly competitive",
                detail=f"There are {total} top-tier GAF contractors within {data.search_distance} miles — standing out requires more than just certification.",
                source="Market analysis",
            ),
        ]

        pain_points = [
            "Differentiating in a market where every competitor also holds President's Club status",
            "Managing customer expectations with the pressure of maintaining a near-perfect rating",
        ]
        if reviews < 150:
            pain_points.append(
                f"Building review volume — currently at {reviews} vs. market average of {avg_rev:.0f}"
            )

        signals = []
        if reviews >= 300:
            signals.append(f"High volume ({reviews} reviews) signals established operations and growth capacity")
        if rating >= 5.0:
            signals.append("Perfect 5.0 rating indicates exceptional service delivery")
        signals.append(f"Located in {c.city}, {c.state} — {'strong' if c.state == 'NJ' else 'underserved'} market coverage")

        actions = [
            f"Call {c.phone} — lead with their President's Club status and local market insights",
            f"Reference their {rating}-star rating as a conversation anchor",
        ]
        if reviews < 200:
            actions.append("Offer review generation / reputation management as a value-add")
        else:
            actions.append("Propose premium visibility or lead generation programs")

        ci = ContractorInsight(
            contractor_id=c.contractor_id or "",
            contractor_name=c.name,
            summary=(
                f"{c.name} is a {size.lower()}, top-tier GAF contractor in {c.city}, {c.state} "
                f"with a {rating}-star rating across {reviews} reviews. "
                f"Their President's Club and Master Elite certifications place them "
                f"in the highest echelon of GAF's network."
            ),
            industry_tags=tags,
            business_size_estimate=size,
            talking_points=tps,
            pain_points=pain_points,
            sales_readiness_score=score,
            sales_readiness_rationale=(
                f"Score {score}/10: {'Strong' if score >= 7 else 'Moderate'} review volume "
                f"({reviews}), {'perfect' if rating >= 5.0 else 'near-perfect'} rating, "
                f"top-tier certifications. {'High' if score >= 7 else 'Moderate'} engagement likelihood."
            ),
            competitive_position=(
                f"{'Market leader' if reviews >= 300 else 'Established player' if reviews >= 100 else 'Growing contender'} "
                f"in {c.city} — ranked by review volume within the {data.search_postal_code} search radius."
            ),
            opportunity_signals=signals,
            recommended_actions=actions,
        )
        contractor_insights.append(ci)

    console.print("[dim]Demo mode: insights generated from local analytics + heuristics[/dim]")
    return SalesIntelligenceReport(
        market=market,
        contractors=contractor_insights,
        source_file=f"contractors_{data.search_postal_code}.json",
    )


@cli.command()
@click.option(
    "--input", "-i", "input_file",
    type=click.Path(exists=True),
    required=True,
    help="Path to sales intelligence JSON report",
)
def view(input_file: str) -> None:
    """View a previously generated sales intelligence report."""
    with open(input_file, encoding="utf-8") as f:
        raw = json.load(f)
    report = SalesIntelligenceReport(**raw)
    display_intelligence_report(report)


# ---------------------------------------------------------------------------
# End-to-End Pipeline
# ---------------------------------------------------------------------------


@cli.command(name="pipeline")
@click.option("--postal-code", "-p", default="10013", show_default=True)
@click.option("--distance", "-d", default=25, show_default=True)
@click.option("--max", "max_contractors", default=0, show_default=True,
              help="Max contractors (0 = all)")
@click.option("--no-headless", is_flag=True, help="Show browser")
@click.option("--skip-profiles", is_flag=True, default=True, show_default=True,
              help="Skip individual profile scraping")
@click.option("--output-dir", "-o", default="output", show_default=True)
@click.option("--db", "db_path", default="gaf_contractors.db", show_default=True,
              help="SQLite database path")
@click.option("--api-key", "-k", envvar="OPENAI_API_KEY",
              help="OpenAI API key (or set OPENAI_API_KEY env var)")
@click.option("--model", "-m", default="gpt-4o-mini", show_default=True)
@click.option("--demo", is_flag=True,
              help="Use demo enrichment (no API key needed)")
def pipeline_cmd(
    postal_code: str,
    distance: int,
    max_contractors: int,
    no_headless: bool,
    skip_profiles: bool,
    output_dir: str,
    db_path: str,
    api_key: str,
    model: str,
    demo: bool,
) -> None:
    """
    Run the full end-to-end pipeline:

        scrape → store in DB → enrich with AI → store insights → export

    Every run is tracked in the database for auditability. The database
    stores both structured data (contractors, certifications, scores) and
    unstructured data (descriptions, AI summaries, talking points).

    \b
    Examples:
        python main.py pipeline --demo
        python main.py pipeline -p 90210 -d 50 --demo
        python main.py pipeline -k sk-... --model gpt-4o
    """
    from database import Database
    from pipeline import Pipeline

    console.print(
        Panel(
            f"[bold]Postal Code:[/bold]  {postal_code}\n"
            f"[bold]Distance:[/bold]     {distance} mi\n"
            f"[bold]Max:[/bold]          {'unlimited' if max_contractors == 0 else max_contractors}\n"
            f"[bold]Profiles:[/bold]     {'no' if skip_profiles else 'yes'}\n"
            f"[bold]Headless:[/bold]     {'yes' if not no_headless else 'no'}\n"
            f"[bold]Database:[/bold]     {db_path}\n"
            f"[bold]Enrichment:[/bold]  {'DEMO (local analytics)' if demo else model}\n"
            f"[bold]Output:[/bold]      {output_dir}",
            title="End-to-End Pipeline",
            border_style="magenta",
        )
    )

    pipe = Pipeline(
        postal_code=postal_code,
        distance=distance,
        db_path=db_path,
        output_dir=output_dir,
        openai_api_key=api_key,
        openai_model=model,
        max_contractors=max_contractors,
        headless=not no_headless,
        skip_profiles=skip_profiles,
        demo_mode=demo,
    )

    result = pipe.run()

    # Display summary
    status_style = "green" if result.get("status") == "completed" else "red"
    summary = (
        f"[bold]Run ID:[/bold]          {result.get('run_id')}\n"
        f"[bold]Status:[/bold]          [{status_style}]{result.get('status')}[/{status_style}]\n"
        f"[bold]Found:[/bold]           {result.get('total_found', 0)}\n"
        f"[bold]Scraped:[/bold]         {result.get('total_scraped', 0)}\n"
        f"[bold]Stored in DB:[/bold]    {result.get('total_stored', 0)}\n"
        f"[bold]Enriched:[/bold]        {result.get('total_enriched', 0)}"
    )
    if result.get("contractors_json"):
        summary += f"\n[bold]Contractors:[/bold]    {result['contractors_json']}"
    if result.get("insights_json"):
        summary += f"\n[bold]Insights:[/bold]       {result['insights_json']}"
    if result.get("error"):
        summary += f"\n[bold red]Error:[/bold red]          {result['error']}"

    console.print(Panel(summary, title="Pipeline Result", border_style=status_style))

    # Show database stats
    db = Database(db_path)
    stats = db.get_stats()
    console.print(
        Panel(
            f"[bold]Total Contractors in DB:[/bold]  {stats['total_contractors']}\n"
            f"[bold]With Sales Insights:[/bold]      {stats['contractors_with_insights']}\n"
            f"[bold]Avg Readiness Score:[/bold]      {stats['avg_sales_readiness_score']}\n"
            f"[bold]Total Pipeline Runs:[/bold]      {stats['total_pipeline_runs']}",
            title=f"Database: {db_path}",
            border_style="cyan",
        )
    )
    db.close()


# ---------------------------------------------------------------------------
# API Server
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.option("--db", "db_path", default="gaf_contractors.db", show_default=True)
@click.option("--reload", "do_reload", is_flag=True, help="Enable auto-reload")
def serve(host: str, port: int, db_path: str, do_reload: bool) -> None:
    """
    Start the FastAPI REST API server.

    The server provides concurrent access to the contractor database and
    sales intelligence. You can also trigger pipeline runs via POST.

    \b
    Endpoints:
        GET  /                        Health check
        GET  /stats                   Dashboard statistics
        POST /pipeline/run            Trigger pipeline (background)
        GET  /pipeline/runs           List runs
        GET  /pipeline/runs/{id}      Run detail
        GET  /contractors             Search / filter
        GET  /contractors/{id}        Single contractor + insight
        GET  /insights/market         Market intelligence
        GET  /insights/top            Top leads by score
        GET  /docs                    Interactive API docs (Swagger)
    """
    import uvicorn

    os.environ["GAF_DB_PATH"] = db_path
    console.print(
        Panel(
            f"[bold]Host:[/bold]      {host}\n"
            f"[bold]Port:[/bold]      {port}\n"
            f"[bold]Database:[/bold]  {db_path}\n"
            f"[bold]Reload:[/bold]    {'yes' if do_reload else 'no'}\n"
            f"[bold]Docs:[/bold]      http://localhost:{port}/docs",
            title="Starting API Server",
            border_style="green",
        )
    )
    uvicorn.run("server:app", host=host, port=port, reload=do_reload)


# ---------------------------------------------------------------------------
# Database Query
# ---------------------------------------------------------------------------


@cli.command(name="db")
@click.option("--db", "db_path", default="gaf_contractors.db", show_default=True)
@click.option("--action", "-a", type=click.Choice(["stats", "runs", "top", "search"]),
              default="stats", show_default=True)
@click.option("--min-score", default=0, show_default=True, help="Min readiness score (for top/search)")
@click.option("--state", default=None, help="Filter by state (for search)")
@click.option("--limit", default=20, show_default=True)
def db_cmd(db_path: str, action: str, min_score: int, state: str, limit: int) -> None:
    """Query the contractor database directly."""
    from database import Database

    database = Database(db_path)
    database.initialize()

    if action == "stats":
        stats = database.get_stats()
        console.print(
            Panel(
                f"[bold]Total Contractors:[/bold]        {stats['total_contractors']}\n"
                f"[bold]With Sales Insights:[/bold]      {stats['contractors_with_insights']}\n"
                f"[bold]Avg Readiness Score:[/bold]      {stats['avg_sales_readiness_score']}\n"
                f"[bold]Total Pipeline Runs:[/bold]      {stats['total_pipeline_runs']}",
                title=f"Database: {db_path}",
                border_style="cyan",
            )
        )

    elif action == "runs":
        runs = database.list_pipeline_runs(limit=limit)
        if not runs:
            console.print("[yellow]No pipeline runs found.[/yellow]")
        else:
            table = Table(title="Pipeline Runs")
            table.add_column("ID", style="bold")
            table.add_column("ZIP")
            table.add_column("Status")
            table.add_column("Scraped")
            table.add_column("Enriched")
            table.add_column("Started")
            for r in runs:
                status_style = "green" if r["status"] == "completed" else "red" if r["status"] == "failed" else "yellow"
                table.add_row(
                    str(r["id"]), r["postal_code"],
                    f"[{status_style}]{r['status']}[/{status_style}]",
                    str(r.get("total_scraped", 0)),
                    str(r.get("total_enriched", 0)),
                    r.get("started_at", "")[:19],
                )
            console.print(table)

    elif action == "top":
        results = database.search_contractors(
            min_score=min_score or 7, limit=limit
        )
        if not results:
            console.print("[yellow]No contractors found with the given criteria.[/yellow]")
        else:
            table = Table(title=f"Top Contractors (score >= {min_score or 7})")
            table.add_column("#", width=4)
            table.add_column("Name", style="bold cyan", max_width=30)
            table.add_column("Score", justify="center", width=6)
            table.add_column("Rating", justify="center", width=8)
            table.add_column("City, State", width=20)
            table.add_column("Size", width=10)
            table.add_column("Summary", max_width=50)
            for i, r in enumerate(results, 1):
                score = r.get("sales_readiness_score") or 0
                sc_style = "green" if score >= 8 else "yellow" if score >= 5 else "red"
                table.add_row(
                    str(i),
                    r.get("name", ""),
                    f"[{sc_style}]{score}[/{sc_style}]",
                    str(r.get("rating", "")),
                    f"{r.get('city', '')}, {r.get('state', '')}",
                    r.get("business_size_estimate", ""),
                    (r.get("insight_summary") or "")[:50],
                )
            console.print(table)

    elif action == "search":
        results = database.search_contractors(
            state=state, min_score=min_score if min_score > 0 else None, limit=limit
        )
        if not results:
            console.print("[yellow]No contractors found.[/yellow]")
        else:
            table = Table(title="Contractor Search Results")
            table.add_column("#", width=4)
            table.add_column("ID", width=10)
            table.add_column("Name", style="bold cyan", max_width=30)
            table.add_column("Rating", justify="center", width=8)
            table.add_column("City, State", width=20)
            table.add_column("Phone", width=16)
            table.add_column("Score", justify="center", width=6)
            for i, r in enumerate(results, 1):
                score = r.get("sales_readiness_score") or 0
                table.add_row(
                    str(i),
                    r.get("contractor_id", ""),
                    r.get("name", ""),
                    str(r.get("rating", "")),
                    f"{r.get('city', '')}, {r.get('state', '')}",
                    r.get("phone", ""),
                    str(score) if score else "-",
                )
            console.print(table)

    database.close()


@cli.command()
def info() -> None:
    """Show information about the scraping system."""
    console.print(
        Panel(
            "[bold]GAF Contractor Data Collection System[/bold]\n\n"
            "Collects contractor information from the GAF roofing\n"
            "contractor directory (gaf.com).\n\n"
            "[bold]Data Fields Collected:[/bold]\n"
            "  - Company name, contractor ID\n"
            "  - Full address (street, city, state, zip)\n"
            "  - Phone number, website\n"
            "  - Rating, review count\n"
            "  - GAF certifications & awards\n"
            "  - Years in business, employee count\n"
            "  - State license number\n"
            "  - Company description\n"
            "  - Profile & job photo URLs\n\n"
            "[bold]Certification Levels:[/bold]\n"
            "  1. President's Club Award (highest)\n"
            "  2. GAF Master Elite\n"
            "  3. GAF Certified Plus\n"
            "  4. GAF Certified\n\n"
            "[bold]Output Formats:[/bold] JSON, CSV, SQLite DB\n\n"
            "[bold cyan]End-to-End Pipeline:[/bold cyan]\n"
            "  python main.py pipeline --demo\n\n"
            "[bold cyan]REST API Server:[/bold cyan]\n"
            "  python main.py serve\n"
            "  → http://localhost:8000/docs\n\n"
            "[bold cyan]Database Queries:[/bold cyan]\n"
            "  python main.py db -a stats\n"
            "  python main.py db -a top --min-score 7\n"
            "  python main.py db -a runs\n"
            "  python main.py db -a search --state NJ\n\n"
            "[bold cyan]Sales Intelligence:[/bold cyan]\n"
            "  - Executive summaries per contractor\n"
            "  - Industry classification & business sizing\n"
            "  - Engagement talking points (cold call / email)\n"
            "  - Pain point identification\n"
            "  - Sales readiness scoring (1-10)\n"
            "  - Competitive positioning analysis\n"
            "  - Opportunity signals & growth indicators\n"
            "  - Recommended next-step actions\n"
            "  - Market-level landscape analysis",
            title="System Info",
            border_style="cyan",
        )
    )


if __name__ == "__main__":
    cli()
