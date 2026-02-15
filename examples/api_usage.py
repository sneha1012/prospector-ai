#!/usr/bin/env python3
"""
Example: Using the ProspectorAI REST API programmatically.

Start the server first:
    python main.py serve --port 8000

Then run this script:
    python examples/api_usage.py
"""

import json
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError

BASE_URL = "http://localhost:8000"


def fetch(endpoint: str) -> dict:
    """Fetch JSON from an API endpoint."""
    url = f"{BASE_URL}{endpoint}"
    try:
        with urlopen(url) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        print(f"Error connecting to {url}: {e}")
        print("Make sure the server is running: python main.py serve")
        sys.exit(1)


def main():
    # 1. Health check
    print("=" * 60)
    print("ProspectorAI API Usage Examples")
    print("=" * 60)

    root = fetch("/")
    print(f"\nService: {root['service']} v{root['version']}")

    # 2. Dashboard stats
    print("\n--- Dashboard Stats ---")
    stats = fetch("/stats")
    print(f"  Total contractors:    {stats['total_contractors']}")
    print(f"  With insights:        {stats['contractors_with_insights']}")
    print(f"  Avg readiness score:  {stats['avg_sales_readiness_score']}")
    print(f"  Pipeline runs:        {stats['total_pipeline_runs']}")

    # 3. Top HOT leads
    print("\n--- Top HOT Leads (score >= 8) ---")
    top = fetch("/insights/top?min_score=8&limit=5")
    for i, c in enumerate(top["contractors"], 1):
        score = c.get("sales_readiness_score", 0)
        print(f"  {i}. {c['name']} ({c['city']}, {c['state']}) "
              f"— Score: {score}, Rating: {c['rating']}")

    # 4. Search by state
    print("\n--- NJ Contractors (top 5) ---")
    nj = fetch("/contractors?state=NJ&limit=5")
    for c in nj["contractors"]:
        print(f"  - {c['name']} ({c['city']}) "
              f"— {c['rating']} stars, {c.get('review_count', 0)} reviews")

    # 5. Market intelligence
    print("\n--- Market Intelligence ---")
    market = fetch("/insights/market")
    print(f"  Region: {market.get('region', 'N/A')}")
    summary = market.get("market_summary", "")
    if summary:
        print(f"  Summary: {summary[:200]}...")

    # 6. Single contractor detail
    print("\n--- Single Contractor Detail ---")
    search = fetch("/contractors?limit=1")
    if search["contractors"]:
        cid = search["contractors"][0]["contractor_id"]
        detail = fetch(f"/contractors/{cid}")
        print(f"  Name: {detail['name']}")
        print(f"  Rating: {detail['rating']} ({detail.get('review_count', 0)} reviews)")
        print(f"  Phone: {detail.get('phone', 'N/A')}")
        if detail.get("certifications"):
            certs = [c["name"] for c in detail["certifications"]]
            print(f"  Certs: {', '.join(certs)}")
        if detail.get("insight"):
            ins = detail["insight"]
            print(f"  Score: {ins.get('sales_readiness_score', 'N/A')}/10")
            print(f"  Size: {ins.get('business_size_estimate', 'N/A')}")
            if ins.get("summary"):
                print(f"  Summary: {ins['summary'][:150]}...")

    print("\n" + "=" * 60)
    print("Full API docs: http://localhost:8000/docs")
    print("Dashboard:     http://localhost:8000/dashboard")
    print("=" * 60)


if __name__ == "__main__":
    main()
