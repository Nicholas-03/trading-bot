# venv + Docker Containerisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local Python virtual environment and containerise the trading bot for deployment on a VM via Docker Compose.

**Architecture:** Multi-stage Dockerfile — a builder stage installs all Python dependencies into an isolated `/venv`, and a lean runtime stage copies only the venv and source code. Docker Compose wires the container to a `.env` file at runtime so secrets are never baked into the image.

**Tech Stack:** Python 3.11, Docker (multi-stage build), Docker Compose v2

---

## Files

| Action | Path | Purpose |
|--------|------|---------|
| Create | `.env.example` | Placeholder config template, safe to commit |
| Create | `.dockerignore` | Exclude secrets and cache from build context |
| Create | `Dockerfile` | Multi-stage image build |
| Create | `docker-compose.yml` | VM deployment config |
| Modify | `README.md` | Add local venv and Docker deployment sections |

---

### Task 1: Create `.env.example`

**Files:**
- Create: `.env.example`

- [ ] **Step 1: Create the file**

```
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# LLM provider: "claude" or "gemini"
LLM_PROVIDER=gemini

# Claude (required if LLM_PROVIDER=claude)
ANTHROPIC_API_KEY=your_anthropic_api_key
ANTHROPIC_MODEL=claude-opus-4-6

# Gemini (required if LLM_PROVIDER=gemini)
GOOGLE_API_KEY=your_google_api_key
GEMINI_MODEL=gemini-2.5-flash

TRADE_AMOUNT_USD=10.0
ALLOW_SHORT=false
STOP_LOSS_PCT=0.10
TAKE_PROFIT_PCT=0.20
```

- [ ] **Step 2: Verify `.env` is gitignored but `.env.example` is not**

Run:
```bash
git check-ignore -v .env .env.example
```
Expected: only `.env` is printed (ignored). `.env.example` prints nothing (not ignored).

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "chore: add .env.example template"
```

---

### Task 2: Create `.dockerignore`

**Files:**
- Create: `.dockerignore`

- [ ] **Step 1: Create the file**

```
.env
.env.local
.git
.gitignore
.venv
venv
__pycache__
*.pyc
*.pyo
.pytest_cache
tests/
docs/
README.md
```

Explanation of each entry:
- `.env` / `.env.local` — secrets must never enter the build context
- `.git` — not needed at runtime, adds ~MB to context
- `.venv` / `venv` — local dev environment, replaced by the container's own venv
- `__pycache__`, `*.pyc`, `*.pyo` — stale bytecode from the host
- `.pytest_cache`, `tests/` — tests are not run inside the container
- `docs/`, `README.md` — documentation, not needed at runtime

- [ ] **Step 2: Commit**

```bash
git add .dockerignore
git commit -m "chore: add .dockerignore"
```

---

### Task 3: Create `Dockerfile`

**Files:**
- Create: `Dockerfile`

- [ ] **Step 1: Create the file**

```dockerfile
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
COPY main.py config.py ./
COPY trading/ trading/
COPY llm/ llm/
COPY news/ news/

CMD ["python", "main.py"]
```

- [ ] **Step 2: Build the image to verify it succeeds**

Run:
```bash
docker build -t trading-bot:test .
```
Expected: build completes with `Successfully tagged trading-bot:test` (or equivalent). Both stages should complete without errors.

- [ ] **Step 3: Verify the image size is reasonable**

Run:
```bash
docker images trading-bot:test
```
Expected: image size is roughly 200–350 MB (slim base + Python deps, no build tools).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "feat: add multi-stage Dockerfile"
```

---

### Task 4: Create `docker-compose.yml`

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Create the file**

```yaml
services:
  trading-bot:
    build: .
    env_file: .env
    restart: unless-stopped
```

Explanation:
- `build: .` — builds the image from the local `Dockerfile`
- `env_file: .env` — injects all variables from `.env` as environment variables at runtime; `.env` is never copied into the image
- `restart: unless-stopped` — the container restarts automatically on crash or VM reboot; only stops when you explicitly run `docker compose down`

- [ ] **Step 2: Verify Compose parses the file correctly**

Run:
```bash
docker compose config
```
Expected: Compose prints the resolved service configuration with no errors.

- [ ] **Step 3: Start the container and verify it runs**

Run:
```bash
docker compose up --build
```
Expected: container starts, bot logs appear (e.g. `Bot starting — paper=True ...`). The bot connects to Alpaca and the news WebSocket. Press `Ctrl+C` to stop.

- [ ] **Step 4: Verify restart policy and detached mode**

Run:
```bash
docker compose up -d
docker compose ps
```
Expected: `trading-bot` shows status `running`.

Stop it:
```bash
docker compose down
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add docker-compose.yml for VM deployment"
```

---

### Task 5: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace the Prerequisites and Setup sections**

The README currently lists `pip install` as the setup path and lacks Docker/venv instructions. Replace the **Prerequisites**, **Setup**, and add a **Docker deployment** section.

Find the line:
```markdown
## Prerequisites

- Python 3.11+
- An [Alpaca](https://alpaca.markets) account (paper trading is free)
- An [Anthropic](https://console.anthropic.com) account with API access
```

Replace the entire Prerequisites + Setup block (up to but not including `## Testing`) with:

```markdown
## Prerequisites

- [Alpaca](https://alpaca.markets) account (paper trading is free)
- An LLM API key — either [Anthropic](https://console.anthropic.com) or [Google AI](https://aistudio.google.com)

## Local development

### 1. Create a virtual environment

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your API keys and settings:

| Variable | Description | Default |
|----------|-------------|---------|
| `ALPACA_API_KEY` | Alpaca API key ID | required |
| `ALPACA_SECRET_KEY` | Alpaca secret key | required |
| `ALPACA_BASE_URL` | Alpaca base URL | `https://paper-api.alpaca.markets` |
| `LLM_PROVIDER` | LLM to use: `claude` or `gemini` | required |
| `ANTHROPIC_API_KEY` | Anthropic API key (if using Claude) | conditional |
| `ANTHROPIC_MODEL` | Claude model ID | `claude-opus-4-6` |
| `GOOGLE_API_KEY` | Google API key (if using Gemini) | conditional |
| `GEMINI_MODEL` | Gemini model ID | `gemini-2.5-flash` |
| `TRADE_AMOUNT_USD` | Dollar amount per buy order | `10.0` |
| `ALLOW_SHORT` | Enable short selling | `false` |
| `STOP_LOSS_PCT` | Stop-loss threshold (decimal) | `0.10` (10%) |
| `TAKE_PROFIT_PCT` | Take-profit threshold (decimal) | `0.20` (20%) |

### 4. Run the bot

```bash
python main.py
```

## Docker deployment (VM)

### 1. Clone the repo and configure

```bash
git clone <repo-url>
cd trading-bot
cp .env.example .env
# Edit .env with real API keys
```

### 2. Start the bot

```bash
docker compose up -d
```

This builds the image and starts the container in the background. The container restarts automatically on crash or VM reboot.

### 3. View logs

```bash
docker compose logs -f
```

### 4. Stop the bot

```bash
docker compose down
```
```

- [ ] **Step 2: Verify the README renders correctly**

Open `README.md` and confirm:
- No broken markdown (unclosed code fences, mismatched headers)
- Both local dev and Docker sections are present

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update README with venv and Docker deployment instructions"
```

---

## Self-Review

**Spec coverage:**
- `.env.example` — Task 1 ✓
- `.dockerignore` — Task 2 ✓
- Multi-stage `Dockerfile` — Task 3 ✓
- `docker-compose.yml` with `restart: unless-stopped` and `env_file` — Task 4 ✓
- Local venv documented — Task 5 ✓
- Secrets never in image — enforced by `.dockerignore` (Task 2) + `env_file` (Task 4) ✓

**Placeholder scan:** None found. All steps contain exact file content and commands.

**Type consistency:** N/A — no shared types across tasks.
