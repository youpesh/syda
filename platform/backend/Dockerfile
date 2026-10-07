FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies (including libpq for PostgreSQL support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv from Astral official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy syda core library (for editable source dependency)
COPY syda /app/syda
COPY pyproject.toml /app/pyproject.toml

# Copy platform backend source and configuration
COPY platform/backend /app/platform/backend

WORKDIR /app/platform/backend

# Sync and install all backend and syda core dependencies
RUN uv sync

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
