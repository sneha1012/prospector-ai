FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser
RUN playwright install chromium --with-deps

# Copy application code
COPY config.py models.py scraper.py database.py insights.py pipeline.py main.py server.py ./
COPY static/ ./static/

# Create output directory
RUN mkdir -p output

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://localhost:8000/')" || exit 1

# Default: start the API server
CMD ["python", "main.py", "serve", "--port", "8000"]
