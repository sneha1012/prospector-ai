"""
Configuration for the GAF Contractor Scraping System.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScraperConfig:
    """Top-level configuration for the scraper."""

    # --- Search Parameters ---
    postal_code: str = "10013"
    distance: int = 25  # miles
    country_code: str = "us"

    # --- Browser Settings ---
    headless: bool = True
    slow_mo: int = 0  # milliseconds between actions (0 = fast)
    viewport_width: int = 1920
    viewport_height: int = 1080
    user_agent: Optional[str] = None  # None = use Playwright default

    # --- Rate Limiting ---
    min_delay: float = 1.5  # minimum seconds between page loads
    max_delay: float = 4.0  # maximum seconds between page loads
    profile_min_delay: float = 1.0  # minimum seconds between profile scrapes
    profile_max_delay: float = 3.0  # maximum seconds between profile scrapes

    # --- Retry Settings ---
    max_retries: int = 3
    retry_wait_min: float = 2.0
    retry_wait_max: float = 15.0

    # --- Timeouts ---
    page_timeout: int = 60_000  # milliseconds
    navigation_timeout: int = 30_000  # milliseconds
    selector_timeout: int = 15_000  # milliseconds

    # --- Output ---
    output_dir: str = "output"
    output_formats: list[str] = field(default_factory=lambda: ["json", "csv"])

    # --- Scraping Scope ---
    max_contractors: int = 0  # 0 = no limit
    scrape_profiles: bool = True  # follow links to scrape full profiles
    scrape_reviews: bool = False  # scrape individual reviews (slow)

    # --- URLs ---
    base_url: str = "https://www.gaf.com"
    search_path: str = "/en-us/roofing-contractors/residential"

    @property
    def search_url(self) -> str:
        params = (
            f"?distance={self.distance}"
            f"&postalCode={self.postal_code}"
            f"&countryCode={self.country_code}"
        )
        return f"{self.base_url}{self.search_path}{params}"
