# venv + Docker Containerisation Design

**Date:** 2026-04-10
**Status:** Approved

## Overview

Add a local Python virtual environment setup and containerise the trading bot for deployment on a VM using Docker Compose. Secrets are never baked into the image — they are injected at runtime via an env file.

## Files Added

| File | Purpose |
|------|---------|
| `.env.example` | Placeholder config, safe to commit; template for real `.env` on the VM |
| `Dockerfile` | Multi-stage build: deps installed in builder stage, only venv + source copied to runtime stage |
| `docker-compose.yml` | Single-service Compose file for VM deployment |
| `.dockerignore` | Excludes `.env`, `__pycache__`, `.git`, `venv`, `.pytest_cache` from build context |

The local `venv` is created with `python -m venv .venv` and used for local dev/testing only. It is not committed.

## Dockerfile — Multi-Stage Build

**Stage 1 (builder):** `python:3.11-slim`
- Creates `/venv` and installs all `requirements.txt` deps into it
- No source code; just the dependency layer for caching efficiency

**Stage 2 (runtime):** `python:3.11-slim` (fresh, no build tools)
- Copies `/venv` from builder
- Copies source: `main.py`, `config.py`, `trading/`, `llm/`, `news/`
- Sets `PYTHONUNBUFFERED=1` so logs stream immediately
- `CMD ["python", "main.py"]`

## docker-compose.yml

Single service `trading-bot`:
- `build: .`
- `env_file: .env` — runtime secret injection, never in image
- `restart: unless-stopped` — survives crashes and VM reboots

No ports exposed. The bot makes outbound connections only.

## VM Deployment Workflow

```bash
git clone <repo>
cp .env.example .env   # fill in real API keys
docker compose up -d   # build image + start container in background
docker compose logs -f # tail logs
```

## Local Dev Workflow

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

## Security Notes

- `.env` is already in `.gitignore` and must never be committed
- `.dockerignore` ensures `.env` cannot be accidentally copied into the build context
- `.env.example` contains only placeholder values
