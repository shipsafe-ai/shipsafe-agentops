FROM python:3.12-slim

# Node 20 — required for npx @dynatrace-oss/dynatrace-mcp-server@latest
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy shipsafe-shared first (sibling repo, needed as local dep)
COPY shipsafe-shared /app/shipsafe-shared

# Copy project files
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY main.py ./

# Install Python deps with server extras
RUN pip install --no-cache-dir -e ".[server]"

# Pre-warm npx cache for Dynatrace MCP server
RUN npx --yes @dynatrace-oss/dynatrace-mcp-server@latest --version || true

EXPOSE 8080

ENV PORT=8080 \
    PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
