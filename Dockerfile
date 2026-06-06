FROM python:3.12-slim

# Node 20 — required for npx @dynatrace-oss/dynatrace-mcp-server@latest
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy shipsafe-shared first (sibling repo, needed as local dep)
# Build context = shipsafe/ parent directory
COPY shipsafe-shared /app/shipsafe-shared

# Copy project files
COPY shipsafe-agentops/pyproject.toml shipsafe-agentops/README.md ./
COPY shipsafe-agentops/agent ./agent
COPY shipsafe-agentops/main.py ./

# Rewrite local file dep path to container path
RUN sed -i 's|file:///Users/prateeksrivastava/Documents/shipsafe/shipsafe-shared|file:///app/shipsafe-shared|g' pyproject.toml

# Install Python deps with server extras
RUN pip install --no-cache-dir -e ".[server]"

# Pre-warm npx cache for Dynatrace MCP server
RUN npx --yes @dynatrace-oss/dynatrace-mcp-server@latest --version || true

EXPOSE 8080

ENV PORT=8080 \
    PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
