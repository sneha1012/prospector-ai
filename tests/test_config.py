"""
Unit tests for the scraper configuration module.

Tests cover default values, URL construction, and parameter validation.
"""

import pytest

from config import ScraperConfig


class TestScraperConfig:
    def test_defaults(self):
        config = ScraperConfig()
        assert config.postal_code == "10013"
        assert config.distance == 25
        assert config.country_code == "us"
        assert config.headless is True
        assert config.max_contractors == 0
        assert config.scrape_profiles is True

    def test_search_url_default(self):
        config = ScraperConfig()
        url = config.search_url
        assert "postalCode=10013" in url
        assert "distance=25" in url
        assert "countryCode=us" in url
        assert url.startswith("https://www.gaf.com")

    def test_search_url_custom(self):
        config = ScraperConfig(postal_code="90210", distance=50)
        url = config.search_url
        assert "postalCode=90210" in url
        assert "distance=50" in url

    def test_rate_limiting_defaults(self):
        config = ScraperConfig()
        assert config.min_delay == 1.5
        assert config.max_delay == 4.0
        assert config.profile_min_delay == 1.0
        assert config.profile_max_delay == 3.0
        assert config.min_delay < config.max_delay

    def test_timeout_defaults(self):
        config = ScraperConfig()
        assert config.page_timeout == 60_000
        assert config.navigation_timeout == 30_000
        assert config.selector_timeout == 15_000

    def test_output_defaults(self):
        config = ScraperConfig()
        assert config.output_dir == "output"
        assert "json" in config.output_formats
        assert "csv" in config.output_formats

    def test_custom_parameters(self):
        config = ScraperConfig(
            postal_code="07470",
            distance=10,
            headless=False,
            max_contractors=20,
            scrape_profiles=False,
            min_delay=2.0,
            max_delay=5.0,
        )
        assert config.postal_code == "07470"
        assert config.distance == 10
        assert config.headless is False
        assert config.max_contractors == 20
        assert config.scrape_profiles is False

    def test_viewport_defaults(self):
        config = ScraperConfig()
        assert config.viewport_width == 1920
        assert config.viewport_height == 1080

    def test_base_url(self):
        config = ScraperConfig()
        assert config.base_url == "https://www.gaf.com"
        assert config.search_path == "/en-us/roofing-contractors/residential"
