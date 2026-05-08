# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Create an isolated venv so only it needs to be copied to the runtime stage
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim

# Activate the venv from the builder stage
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

# Stream logs immediately (no Python output buffering)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy only the source needed at runtime
COPY main.py config.py railway_start.py ./
COPY trading/ trading/
COPY llm/ llm/
COPY news/ news/
COPY notifications/ notifications/
COPY analytics/ analytics/

EXPOSE 8080

CMD ["python", "railway_start.py"]
