FROM python:3.11-slim

WORKDIR /app

# Install curl for healthchecks and uv for fast package management
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml .
COPY requirements.txt .

# Install dependencies using uv (much faster than pip)
RUN uv pip install --system --no-cache -r requirements.txt

# Copy application code (new package structure)
COPY src/ ./src/
COPY config/ ./config/

# Install the package
RUN uv pip install --system --no-cache -e .

# Create non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Use the new module path
CMD ["uvicorn", "hermes.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
