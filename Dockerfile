FROM python:3.12-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (cached layer) ───────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# ── Create required directories ───────────────────────────────────────────────
RUN mkdir -p /app/logs /app/data

# ── Non-root user for security ────────────────────────────────────────────────
RUN useradd -m -u 1000 nbabot && chown -R nbabot:nbabot /app
USER nbabot

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('/app/data/nba_predictions.db').close()" || exit 1

# ── Default command: run scheduler ───────────────────────────────────────────
CMD ["python", "scheduler.py"]
