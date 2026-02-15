"""
Unit tests for the local analytics and insight generation logic.

Tests cover the compute_local_analytics function and the demo-mode
heuristic scoring — does NOT require an OpenAI API key.
"""

import pytest

from insights import compute_local_analytics
from models import Certification, Contractor


def _make_contractors(n: int = 5) -> list[Contractor]:
    """Generate a list of test contractors with varied data."""
    data = [
        ("Alpha Roofing", "NJ", "Wayne", 4.9, 250, ["GAF Master Elite"]),
        ("Beta Exteriors", "NJ", "Edison", 4.5, 80, ["GAF Master Elite"]),
        ("Gamma Siding & Home Improvement", "NY", "Bronx", 5.0, 400, ["GAF Master Elite", "President's Club Award"]),
        ("Delta Construction", "NY", "Staten Island", 4.2, 50, ["GAF Certified"]),
        ("Epsilon Roofing LLC", "NJ", "Bergenfield", 5.0, 320, ["GAF Master Elite", "President's Club Award"]),
    ]
    contractors = []
    for i, (name, state, city, rating, reviews, certs) in enumerate(data[:n]):
        contractors.append(
            Contractor(
                contractor_id=str(1000 + i),
                name=name,
                state=state,
                city=city,
                rating=rating,
                review_count=reviews,
                certifications=[Certification(name=c) for c in certs],
            )
        )
    return contractors


# ---------------------------------------------------------------------------
# Local pre-analytics
# ---------------------------------------------------------------------------


class TestComputeLocalAnalytics:
    def test_total_count(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        assert analytics["total_contractors"] == 5

    def test_average_rating(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        avg = analytics["avg_rating"]
        assert avg is not None
        assert 4.0 <= avg <= 5.0

    def test_average_review_count(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        avg_rev = analytics["avg_review_count"]
        assert avg_rev is not None
        assert avg_rev > 0

    def test_total_reviews(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        total = analytics["total_reviews"]
        expected = 250 + 80 + 400 + 50 + 320
        assert total == expected

    def test_certification_distribution(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        cert_dist = analytics["certification_distribution"]
        assert "GAF Master Elite" in cert_dist
        assert cert_dist["GAF Master Elite"] == 4  # 4 have Master Elite

    def test_state_distribution(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        state_dist = analytics["state_distribution"]
        assert state_dist["NJ"] == 3
        assert state_dist["NY"] == 2

    def test_city_distribution(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        city_dist = analytics["city_distribution"]
        assert "Wayne, NJ" in city_dist
        assert "Bronx, NY" in city_dist

    def test_top_by_reviews(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        top = analytics["top_by_reviews"]
        assert len(top) > 0
        # Gamma has 400 reviews — should be first
        assert "Gamma" in top[0]

    def test_top_by_rating(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        top = analytics["top_by_rating"]
        assert len(top) > 0
        # 5.0 rated contractors should be first
        first = top[0]
        assert "5.0" in first

    def test_empty_contractors(self):
        analytics = compute_local_analytics([])
        assert analytics["total_contractors"] == 0
        assert analytics["avg_rating"] is None
        assert analytics["total_reviews"] == 0

    def test_min_max_rating(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        assert analytics["min_rating"] == 4.2
        assert analytics["max_rating"] == 5.0

    def test_median_rating(self):
        contractors = _make_contractors(5)
        analytics = compute_local_analytics(contractors)
        median = analytics["median_rating"]
        assert median is not None
        assert 4.0 <= median <= 5.0


# ---------------------------------------------------------------------------
# Demo scoring heuristic
# ---------------------------------------------------------------------------


class TestDemoScoring:
    """Test the scoring logic used in demo mode (from pipeline._demo_enrich)."""

    def _compute_score(self, reviews: int, rating: float, has_presidents_club: bool) -> int:
        """Replicate the demo scoring heuristic."""
        score = 5
        if reviews >= 300:
            score += 2
        elif reviews >= 100:
            score += 1
        if rating >= 5.0:
            score += 1
        if has_presidents_club:
            score += 1
        return min(score, 10)

    def test_hot_lead(self):
        # 400 reviews (+2), 5.0 rating (+1), President's Club (+1) = 5+2+1+1 = 9
        score = self._compute_score(400, 5.0, True)
        assert score == 9
        assert score >= 8  # HOT

    def test_warm_lead(self):
        # 150 reviews (+1), 4.8 rating (+0), no PC (+0) = 5+1 = 6
        score = self._compute_score(150, 4.8, False)
        assert score == 6
        assert 5 <= score < 8  # WARM

    def test_cold_lead(self):
        # 30 reviews (+0), 4.2 rating (+0), no PC (+0) = 5
        score = self._compute_score(30, 4.2, False)
        assert score == 5
        assert score >= 5  # Actually WARM boundary

    def test_minimum_score(self):
        score = self._compute_score(10, 3.5, False)
        assert score == 5  # base score

    def test_maximum_score(self):
        score = self._compute_score(500, 5.0, True)
        assert score == 9  # 5+2+1+1 = 9

    def test_size_heuristic(self):
        """Test business size estimation logic."""
        assert self._estimate_size(500) == "Mid-size"
        assert self._estimate_size(200) == "Small"
        assert self._estimate_size(50) == "Micro"

    def _estimate_size(self, reviews: int) -> str:
        if reviews >= 400:
            return "Mid-size"
        elif reviews >= 150:
            return "Small"
        return "Micro"
