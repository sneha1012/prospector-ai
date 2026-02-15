"""
Core scraping engine for GAF contractor data.

Strategy:
  1. Navigate to the search results page with Playwright (handles JS rendering).
  2. Intercept XHR/Fetch requests to capture the contractor data API response.
  3. Parse the search results listing for contractor cards.
  4. Optionally follow each contractor profile link for full details.
  5. Collect and return structured Contractor models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)
from playwright_stealth import Stealth
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from config import ScraperConfig
from models import Certification, Contractor, Review, ScrapingResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_delay(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _extract_rating(text: str) -> Optional[float]:
    """Pull a numeric rating like '4.9' from a string."""
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _extract_review_count(text: str) -> Optional[int]:
    """Pull review count from strings like '4.9(940)' or '(940)'."""
    m = re.search(r"\((\d[\d,]*)\)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _parse_contractor_id_from_url(url: str) -> Optional[str]:
    """Extract contractor ID from profile URL like '...fletcher-construction-1107334'."""
    m = re.search(r"-(\d{5,})$", url.rstrip("/"))
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# API Response Interceptor
# ---------------------------------------------------------------------------

class APIInterceptor:
    """Captures Coveo search API responses containing contractor data."""

    def __init__(self) -> None:
        self.captured_responses: list[dict[str, Any]] = []
        self.coveo_responses: list[dict[str, Any]] = []
        self.api_url: Optional[str] = None

    async def on_response(self, response: Response) -> None:
        url = response.url
        content_type = response.headers.get("content-type", "")

        if "json" not in content_type:
            return

        # Specifically target Coveo search API (the primary data source)
        if "coveo.com/rest/search" in url:
            try:
                body = await response.json()
                self.coveo_responses.append(
                    {"url": url, "status": response.status, "body": body}
                )
                self.api_url = url
                logger.info(
                    "Captured Coveo API: %d results (total: %d)",
                    len(body.get("results", [])),
                    body.get("totalCount", 0),
                )
            except Exception:
                pass

        # Also capture other JSON responses that might contain contractor data
        elif any(
            kw in url.lower()
            for kw in ["contractor", "search", "locator", "find"]
        ):
            try:
                body = await response.json()
                self.captured_responses.append(
                    {"url": url, "status": response.status, "body": body}
                )
            except Exception:
                pass


def parse_coveo_results(coveo_body: dict[str, Any]) -> list[Contractor]:
    """
    Parse the Coveo search API response into Contractor models.
    This is the primary data extraction path — the Coveo API returns
    structured contractor data directly.
    """
    results = coveo_body.get("results", [])
    contractors: list[Contractor] = []

    for item in results:
        raw = item.get("raw", {})
        title = item.get("title", "")
        profile_url = item.get("clickUri", "") or item.get("uri", "")

        # Parse certifications
        cert_names = raw.get(
            "gaf_f_contractor_certifications_and_awards_residential", []
        )
        if isinstance(cert_names, str):
            cert_names = [cert_names]
        certs = [
            Certification(
                name=cn,
                type="award" if "award" in cn.lower() else "certification",
            )
            for cn in cert_names
        ]

        # Profile photo
        profile_photos = []
        img_src = raw.get("gaf_featured_image_src", "")
        if img_src:
            profile_photos.append(img_src)

        contractor = Contractor(
            contractor_id=str(raw.get("gaf_contractor_id", "")),
            name=raw.get("gaf_contractor_dba") or raw.get("gaf_navigation_title") or title,
            slug=profile_url.rstrip("/").split("/")[-1] if profile_url else None,
            city=raw.get("gaf_f_city"),
            state=raw.get("gaf_f_state_code"),
            postal_code=raw.get("gaf_postal_code"),
            country=raw.get("gaf_f_country_code", "US"),
            phone=raw.get("gaf_phone"),
            rating=raw.get("gaf_rating"),
            review_count=raw.get("gaf_number_of_reviews"),
            certifications=certs,
            profile_photo_urls=profile_photos,
            profile_url=profile_url,
        )
        contractors.append(contractor)

    return contractors


# ---------------------------------------------------------------------------
# Page Parsers
# ---------------------------------------------------------------------------

def parse_search_results_page(html: str, base_url: str) -> list[dict[str, Any]]:
    """
    Parse the search results HTML to extract contractor card data.
    Returns a list of dicts with partial contractor info + profile URLs.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []

    # GAF uses various card patterns — look for links to contractor profiles
    profile_pattern = re.compile(
        r"/en-us/roofing-contractors/residential/usa/\w+/[\w-]+/[\w-]+-\d+"
    )

    # Find all anchor tags that link to contractor profiles
    seen_urls: set[str] = set()
    for a_tag in soup.find_all("a", href=profile_pattern):
        href = a_tag.get("href", "")
        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        card_data: dict[str, Any] = {"profile_url": full_url}

        # Try to extract name from the link or nearby heading
        card = _find_parent_card(a_tag)
        if card:
            card_data.update(_parse_card(card))
        else:
            link_text = _clean_text(a_tag.get_text())
            if link_text and not link_text.lower().startswith(("see", "view", "read")):
                card_data["name"] = link_text

        # Extract contractor ID from URL
        cid = _parse_contractor_id_from_url(full_url)
        if cid:
            card_data["contractor_id"] = cid

        if card_data.get("name") or card_data.get("contractor_id"):
            results.append(card_data)

    return results


def _find_parent_card(tag: Tag) -> Optional[Tag]:
    """Walk up the DOM to find a container that looks like a contractor card."""
    for parent in tag.parents:
        if parent.name in ("article", "li", "div"):
            classes = " ".join(parent.get("class", []))
            if any(
                kw in classes.lower()
                for kw in ["card", "contractor", "result", "item", "listing"]
            ):
                return parent
        # Don't go too far up
        if parent.name in ("body", "main", "section"):
            break
    return None


def _parse_card(card: Tag) -> dict[str, Any]:
    """Extract fields from a contractor card element."""
    data: dict[str, Any] = {}

    # Name: usually in a heading
    heading = card.find(["h2", "h3", "h4", "strong"])
    if heading:
        data["name"] = _clean_text(heading.get_text())

    # Rating & reviews
    for el in card.find_all(string=re.compile(r"\d+\.\d+.*\(\d+")):
        text = el.strip()
        data["rating"] = _extract_rating(text)
        data["review_count"] = _extract_review_count(text)
        break

    # Phone
    phone_link = card.find("a", href=re.compile(r"^tel:"))
    if phone_link:
        data["phone"] = _clean_text(phone_link.get_text())

    # Address
    for el in card.find_all(["p", "span", "div"]):
        text = _clean_text(el.get_text())
        if re.search(r"\b[A-Z]{2},?\s*\d{5}\b", text):
            data["address"] = text
            break

    return data


def parse_profile_page(html: str, url: str) -> Contractor:
    """
    Parse a full contractor profile page and return a Contractor model.
    """
    soup = BeautifulSoup(html, "lxml")

    # --- Name ---
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = _clean_text(h1.get_text())

    # --- Rating & Review Count ---
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # Look for pattern like "4.9(940)"
    for text_node in soup.find_all(string=re.compile(r"\d+\.\d+\s*\(\d+")):
        text = text_node.strip()
        rating = _extract_rating(text)
        review_count = _extract_review_count(text)
        break

    # If not found in text nodes, look in elements
    if rating is None:
        for el in soup.find_all(["span", "div", "p"]):
            text = _clean_text(el.get_text())
            if re.match(r"^\d+\.\d+\s*\(\d+", text):
                rating = _extract_rating(text)
                review_count = _extract_review_count(text)
                break

    # --- Address ---
    address = ""
    city = ""
    state = ""
    postal_code_val = ""

    # Look for address patterns — prefer smallest element that matches
    addr_pattern = re.compile(
        r"^([\d].+?),\s*([A-Za-z\s]+?)\s+([A-Z]{2}),?\s*(\d{5})(?:\s*USA)?$"
    )
    candidates: list[tuple[int, str, re.Match]] = []  # (text_len, text, match)
    for el in soup.find_all(["p", "span", "div", "address"]):
        text = _clean_text(el.get_text())
        m = addr_pattern.search(text)
        if m:
            candidates.append((len(text), text, m))

    if candidates:
        # Pick the shortest matching element (most specific)
        candidates.sort(key=lambda x: x[0])
        _, addr_text, m = candidates[0]
        address = addr_text.rstrip("USA").strip()
        city = m.group(2).strip()
        state = m.group(3).strip()
        postal_code_val = m.group(4).strip()
    else:
        # Fallback: looser pattern on any element
        for el in soup.find_all(["p", "span", "div", "address"]):
            text = _clean_text(el.get_text())
            m = re.search(
                r"(\d+[^,]+),\s*([A-Za-z\s]+?)\s+([A-Z]{2}),?\s*(\d{5})",
                text,
            )
            if m and len(text) < 200:  # guard against huge containers
                address = m.group(0).strip()
                city = m.group(2).strip()
                state = m.group(3).strip()
                postal_code_val = m.group(4).strip()
                break

    # --- Phone ---
    phone = ""
    phone_link = soup.find("a", href=re.compile(r"^tel:"))
    if phone_link:
        phone = _clean_text(phone_link.get_text())
        if phone.lower().startswith("phone number:"):
            phone = phone[len("phone number:") :].strip()

    # --- Website ---
    website = ""
    for a in soup.find_all("a"):
        text = _clean_text(a.get_text())
        if text.lower() in ("visit website", "website"):
            website = a.get("href", "")
            break

    # --- Description ---
    description = ""
    about_heading = soup.find(
        ["h2", "h3"], string=re.compile(r"about", re.IGNORECASE)
    )
    if about_heading:
        # Get the next sibling paragraphs
        desc_parts: list[str] = []
        for sibling in about_heading.find_next_siblings():
            if sibling.name in ("h2", "h3"):
                break
            text = _clean_text(sibling.get_text())
            if text and "expand to read more" not in text.lower():
                desc_parts.append(text)
        description = " ".join(desc_parts)

    # --- Certifications ---
    certs: list[Certification] = []
    cert_section = soup.find(
        ["h2", "h3"],
        string=re.compile(r"certifications?\s*[&]\s*awards?", re.IGNORECASE),
    )
    if cert_section:
        cert_container = cert_section.find_parent(["section", "div"])
        if cert_container:
            for item in cert_container.find_all(["h3", "h4"]):
                cert_name = _clean_text(item.get_text())
                if cert_name and cert_name.lower() not in (
                    "certifications & awards",
                    "see all certifications",
                ):
                    cert_type = "award" if "award" in cert_name.lower() else "certification"
                    certs.append(Certification(name=cert_name, type=cert_type))

    # Fallback: search for known certification names
    if not certs:
        cert_names = [
            "President's Club Award",
            "GAF Master Elite",
            "GAF Certified Plus",
            "GAF Certified",
        ]
        page_text = soup.get_text()
        for cname in cert_names:
            # Check for certification in context (not in descriptions of what they are)
            # Look for it near headings
            for heading in soup.find_all(["h3", "h4"]):
                heading_text = _clean_text(heading.get_text())
                if cname.lower().replace("®", "").replace("™", "") in heading_text.lower().replace("®", "").replace("™", ""):
                    cert_type = "award" if "award" in heading_text.lower() else "certification"
                    if not any(c.name == heading_text for c in certs):
                        certs.append(Certification(name=heading_text, type=cert_type))

    # --- Details ---
    years_in_business = ""
    number_of_employees = ""
    state_license = ""
    contractor_id = ""

    details_section = soup.find(
        ["h2", "h3"], string=re.compile(r"details", re.IGNORECASE)
    )
    if details_section:
        container = details_section.find_parent(["section", "div"])
        if container:
            text = container.get_text()

            m = re.search(r"(?:in business since|years in business)\s*[:\s]*(.+?)(?:\n|$)", text, re.IGNORECASE)
            if m:
                years_in_business = _clean_text(m.group(1))

            m = re.search(r"contractor\s*id\s*[:\s]*(\d+)", text, re.IGNORECASE)
            if m:
                contractor_id = m.group(1)

            m = re.search(
                r"state\s*license\s*number\s*[:\s]*([\w-]+)", text, re.IGNORECASE
            )
            if m:
                state_license = m.group(1)

            m = re.search(
                r"number\s*of\s*employees\s*[:\s]*(.+?)(?:\n|$)",
                text,
                re.IGNORECASE,
            )
            if m:
                number_of_employees = _clean_text(m.group(1))

    # If contractor_id not found in details, extract from URL
    if not contractor_id:
        contractor_id = _parse_contractor_id_from_url(url) or ""

    # --- Photos ---
    profile_photos: list[str] = []
    job_photos: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "profile-photos" in src:
            profile_photos.append(src)
        elif "job-photos" in src:
            job_photos.append(src)

    return Contractor(
        contractor_id=contractor_id or None,
        name=name,
        slug=url.rstrip("/").split("/")[-1] if url else None,
        address=address or None,
        city=city or None,
        state=state or None,
        postal_code=postal_code_val or None,
        country="US",
        phone=phone or None,
        website=website or None,
        description=description or None,
        rating=rating,
        review_count=review_count,
        years_in_business=years_in_business or None,
        number_of_employees=number_of_employees or None,
        state_license_number=state_license or None,
        certifications=certs,
        profile_photo_urls=profile_photos,
        job_photo_urls=job_photos,
        profile_url=url,
    )


# ---------------------------------------------------------------------------
# Scraping Engine
# ---------------------------------------------------------------------------

class GAFScraper:
    """
    Playwright-based scraper for GAF contractor directory.

    Usage:
        async with GAFScraper(config) as scraper:
            result = await scraper.run()
            result.to_json("output/contractors.json")
    """

    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self._stealth = Stealth(
            navigator_platform_override="MacIntel",
            navigator_vendor_override="Google Inc.",
        )
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._interceptor = APIInterceptor()

    async def __aenter__(self) -> "GAFScraper":
        await self._setup()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._teardown()

    async def _setup(self) -> None:
        self._playwright = await async_playwright().start()

        # Browser launch configuration with anti-detection args
        launch_kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "slow_mo": self.config.slow_mo,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
            ],
        }

        # Try to use the real Chrome channel for a more realistic fingerprint.
        # Falls back to bundled Chromium if Chrome is not installed.
        try:
            self._browser = await self._playwright.chromium.launch(
                **launch_kwargs, channel="chrome"
            )
            logger.info("Launched with Chrome channel")
        except Exception:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            logger.info("Launched with bundled Chromium")

        # Realistic user-agent if none provided
        default_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self._context = await self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            user_agent=self.config.user_agent or default_ua,
            locale="en-US",
            timezone_id="America/New_York",
            geolocation={"latitude": 40.7128, "longitude": -74.0060},
            permissions=["geolocation"],
        )
        self._context.set_default_timeout(self.config.page_timeout)

        # Apply stealth patches to the context to evade bot detection
        await self._stealth.apply_stealth_async(self._context)

        self._page = await self._context.new_page()
        self._page.on("response", self._interceptor.on_response)

    async def _teardown(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ---- Warm-up (pass Akamai challenge) ------------------------------------

    async def _warmup(self) -> None:
        """
        Navigate to the main GAF site first to establish cookies and pass
        any Akamai bot-detection challenge before hitting specific pages.
        """
        assert self._page is not None
        logger.info("Warming up: navigating to main site to set cookies ...")
        try:
            await self._page.goto(
                self.config.base_url, wait_until="domcontentloaded"
            )
            # Wait for any challenge scripts to complete
            await self._page.wait_for_timeout(3000)

            # Verify we got past the challenge
            title = await self._page.title()
            if "access denied" in title.lower():
                logger.warning("Warm-up hit Access Denied; retrying with delay")
                await self._page.wait_for_timeout(5000)
                await self._page.reload(wait_until="domcontentloaded")
                await self._page.wait_for_timeout(3000)

            logger.info("Warm-up complete (title: %s)", await self._page.title())
        except Exception as e:
            logger.warning("Warm-up failed (non-fatal): %s", e)

    # ---- Main entry point -------------------------------------------------

    async def run(self) -> ScrapingResult:
        """
        Execute the full scraping pipeline.

        Strategy:
          1. Navigate to the search page and let JS render.
          2. The API interceptor captures the Coveo search API response,
             which contains structured contractor data (name, phone, rating,
             certifications, etc.) for all results.
          3. Parse the Coveo response as the PRIMARY data source.
          4. Handle pagination by scrolling / clicking "Load More" to
             trigger additional Coveo API calls.
          5. Optionally follow contractor profile URLs for enrichment
             (description, full address, photos, license, etc.).
        """
        result = ScrapingResult(
            search_postal_code=self.config.postal_code,
            search_distance=self.config.distance,
        )

        logger.info(
            "Starting scrape: postal=%s distance=%d",
            self.config.postal_code,
            self.config.distance,
        )

        # Warm up to pass any Akamai challenge
        await self._warmup()

        try:
            # Phase 1: Load search page — triggers Coveo API calls
            await self._load_search_page_and_paginate()

            # Phase 2: Parse contractors from captured Coveo API responses
            contractors = self._parse_coveo_contractors()
            result.total_found = len(contractors)
            logger.info(
                "Extracted %d contractors from Coveo API", len(contractors)
            )

            if self.config.max_contractors > 0:
                contractors = contractors[: self.config.max_contractors]

            # Phase 3: Optionally enrich with profile page scraping
            if self.config.scrape_profiles and contractors:
                entries = [
                    {"profile_url": c.profile_url} for c in contractors if c.profile_url
                ]
                enriched = await self._scrape_profiles(entries, result)

                # Merge enriched data back into API-sourced contractors
                enriched_by_id = {c.contractor_id: c for c in enriched if c.contractor_id}
                for c in contractors:
                    if c.contractor_id and c.contractor_id in enriched_by_id:
                        e = enriched_by_id[c.contractor_id]
                        if e.address:
                            c.address = e.address
                        if e.website:
                            c.website = e.website
                        if e.description:
                            c.description = e.description
                        if e.years_in_business:
                            c.years_in_business = e.years_in_business
                        if e.number_of_employees:
                            c.number_of_employees = e.number_of_employees
                        if e.state_license_number:
                            c.state_license_number = e.state_license_number
                        if e.job_photo_urls:
                            c.job_photo_urls = e.job_photo_urls
                        # Use profile-scraped name if API name is missing
                        if not c.name and e.name:
                            c.name = e.name

            # Set search metadata
            for c in contractors:
                c.search_postal_code = self.config.postal_code
                c.search_distance = self.config.distance

            result.contractors = contractors

        except Exception as e:
            logger.error("Scraping failed: %s", e, exc_info=True)
            result.errors.append(str(e))

            # Fallback: try to extract whatever we have from the API
            if self._interceptor.coveo_responses:
                fallback = self._parse_coveo_contractors()
                if fallback:
                    logger.info(
                        "Recovered %d contractors from Coveo API", len(fallback)
                    )
                    for c in fallback:
                        c.search_postal_code = self.config.postal_code
                        c.search_distance = self.config.distance
                    result.contractors = fallback

        return result

    def _parse_coveo_contractors(self) -> list[Contractor]:
        """
        Parse all captured Coveo API responses into deduplicated Contractor list.
        """
        all_contractors: list[Contractor] = []
        seen_ids: set[str] = set()

        for resp in self._interceptor.coveo_responses:
            body = resp.get("body", {})
            contractors = parse_coveo_results(body)
            for c in contractors:
                if c.contractor_id and c.contractor_id not in seen_ids:
                    seen_ids.add(c.contractor_id)
                    all_contractors.append(c)

        return all_contractors

    # ---- Phase 1: Load search page and paginate ----------------------------

    async def _load_search_page_and_paginate(self) -> None:
        """
        Navigate to the search page, wait for the Coveo API response,
        and handle pagination by navigating to #firstResult=10, 20, 30 ...

        GAF's pagination uses URL hash fragments:
          page 1: ...&countryCode=us           (firstResult=0, implicit)
          page 2: ...&countryCode=us#firstResult=10
          page 3: ...&countryCode=us#firstResult=20
        Each navigation triggers a fresh Coveo API call that the interceptor
        captures automatically.
        """
        assert self._page is not None
        page = self._page
        page_size = 10  # GAF returns 10 results per page

        base_url = self.config.search_url
        logger.info("Navigating to search page: %s", base_url)

        # ── Page 1 ──
        await page.goto(base_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)  # let JS hydrate & Coveo API call
        await self._scroll_page(page)

        # Extract total from the first Coveo response
        total_from_api = 0
        collected_so_far = 0
        for resp in self._interceptor.coveo_responses:
            body = resp.get("body", {})
            total_from_api = max(total_from_api, body.get("totalCount", 0))
            collected_so_far += len(body.get("results", []))

        logger.info(
            "Coveo API reports %d total results; captured %d from page 1",
            total_from_api,
            collected_so_far,
        )

        if total_from_api == 0:
            return

        # Determine how many pages we need
        max_target = total_from_api
        if self.config.max_contractors > 0:
            max_target = min(max_target, self.config.max_contractors)

        total_pages = (max_target + page_size - 1) // page_size  # ceil division

        # ── Pages 2..N via #firstResult= hash navigation ──
        for page_num in range(2, total_pages + 1):
            first_result = (page_num - 1) * page_size
            page_url = f"{base_url}#firstResult={first_result}"

            logger.info(
                "Loading page %d/%d (firstResult=%d, have %d/%d) ...",
                page_num,
                total_pages,
                first_result,
                collected_so_far,
                total_from_api,
            )

            prev_count = len(self._interceptor.coveo_responses)

            # Navigate to the paginated URL
            await page.goto(page_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)  # wait for Coveo API call
            await self._scroll_page(page)

            # Verify new Coveo response was captured
            if len(self._interceptor.coveo_responses) <= prev_count:
                # Hash change may not trigger full navigation; try JS approach
                logger.debug("No new API response — trying hash change via JS")
                await page.evaluate(
                    f"window.location.hash = 'firstResult={first_result}'"
                )
                await page.wait_for_timeout(4000)

            # Update count
            collected_so_far = sum(
                len(r.get("body", {}).get("results", []))
                for r in self._interceptor.coveo_responses
            )
            logger.info(
                "Page %d done — now have %d/%d results",
                page_num,
                collected_so_far,
                total_from_api,
            )

            # Rate limiting between pages
            delay = _random_delay(self.config.min_delay, self.config.max_delay)
            await asyncio.sleep(delay)

            # Early exit if we have enough
            if collected_so_far >= max_target:
                break

    async def _scroll_page(self, page: Page) -> None:
        """Incrementally scroll the page to trigger lazy loading."""
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)

    # ---- Phase 2: Profile scraping ----------------------------------------

    async def _scrape_profiles(
        self,
        entries: list[dict[str, Any]],
        result: ScrapingResult,
    ) -> list[Contractor]:
        """Visit each contractor profile URL and scrape full details."""
        contractors: list[Contractor] = []
        assert self._page is not None

        for i, entry in enumerate(entries):
            url = entry.get("profile_url", "")
            if not url:
                continue

            logger.info(
                "Scraping profile %d/%d: %s", i + 1, len(entries), url
            )

            try:
                contractor = await self._scrape_single_profile(url)
                contractor.search_postal_code = self.config.postal_code
                contractor.search_distance = self.config.distance

                # Merge any data we got from the search listing
                if not contractor.rating and entry.get("rating"):
                    contractor.rating = entry["rating"]
                if not contractor.review_count and entry.get("review_count"):
                    contractor.review_count = entry["review_count"]
                if not contractor.phone and entry.get("phone"):
                    contractor.phone = entry["phone"]

                contractors.append(contractor)
                logger.info("  -> Scraped: %s", contractor.name)
            except Exception as e:
                error_msg = f"Failed to scrape {url}: {e}"
                logger.error(error_msg)
                result.errors.append(error_msg)

            # Rate limiting
            if i < len(entries) - 1:
                delay = _random_delay(
                    self.config.profile_min_delay,
                    self.config.profile_max_delay,
                )
                await asyncio.sleep(delay)

        return contractors

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((TimeoutError, Exception)),
        reraise=True,
    )
    async def _scrape_single_profile(self, url: str) -> Contractor:
        """Scrape a single contractor profile page with retries."""
        assert self._page is not None
        page = self._page

        await page.goto(url, wait_until="domcontentloaded")

        # Wait for the main heading to appear (indicates content rendered)
        try:
            await page.wait_for_selector("h1", timeout=self.config.selector_timeout)
        except Exception:
            pass

        # Extra time for any remaining JS hydration
        await page.wait_for_timeout(2000)

        # Check for Access Denied and retry with a longer wait
        title = await page.title()
        if "access denied" in title.lower():
            logger.warning("Access Denied on %s — waiting and retrying", url)
            await page.wait_for_timeout(5000)
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        html = await page.content()
        contractor = parse_profile_page(html, url)

        # If the name is empty / looks wrong, give JS more time and re-parse
        if not contractor.name or contractor.name.lower() in ("access denied", ""):
            logger.debug("Name missing — waiting for JS render on %s", url)
            await page.wait_for_timeout(4000)
            html = await page.content()
            contractor = parse_profile_page(html, url)

        return contractor

