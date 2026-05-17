# DigitalOcean Deployment

This project can redeploy itself on a DigitalOcean Droplet whenever changes are pushed to the GitHub `master` branch. The deployment is handled by GitHub Actions over SSH.

## Production layout

The expected Droplet layout is:

| Path | Purpose |
|------|---------|
| `/opt/trading-bot/app` | Git checkout of this repository |
| `/opt/trading-bot/docker-compose.yml` | Production Docker Compose stack |
| `/opt/trading-bot/app/.env` | Production environment file, never committed |
| `/mnt/trading-bot-data` | Persistent analytics database volume, if used by the production stack |

The workflow defaults to the `master` branch because this repository currently uses `master` as its active branch. A local commit by itself does not deploy; the commit must be pushed to GitHub, or the workflow must be run manually from GitHub Actions.

## GitHub Actions workflow

The workflow lives at `.github/workflows/deploy-digitalocean.yml`.

On every push to `master`, or when run manually from the GitHub Actions tab, it:

1. Connects to the Droplet over SSH.
2. Pulls the latest commit into `/opt/trading-bot/app`.
3. Creates or reuses a temporary test virtual environment at `/tmp/trading-bot-deploy-venv`.
4. Installs `requirements-dev.txt`.
5. Runs `python -m pytest tests/ -v`.
6. Verifies that `main` imports cleanly.
7. Rebuilds the Docker Compose service.
8. Recreates the running `trading-bot` container.

If the pull, tests, import check, Docker build, or Compose restart fails, the workflow fails and the deploy stops.

Because the deploy step runs `git pull --ff-only origin "$BRANCH"` inside `/opt/trading-bot/app`, the Droplet checkout must be clean. If production was hotfixed manually on the Droplet, commit/push the same fix from the local repository and clean or reset the manual Droplet checkout edits before expecting the next auto-deploy to succeed.

## Required GitHub secrets

Add these in GitHub under **Settings -> Secrets and variables -> Actions -> Repository secrets**:

| Secret | Description |
|--------|-------------|
| `DROPLET_HOST` | Droplet IP address or DNS name |
| `DROPLET_USER` | SSH user, for example `root` or `deploy` |
| `DROPLET_SSH_KEY` | Private SSH key used by GitHub Actions to connect to the Droplet |

The SSH user must be able to:

- read and update the repository checkout,
- run `python3`,
- create Python virtual environments with `python3-venv`,
- run `docker compose build`,
- run `docker compose up -d --no-deps trading-bot`.

If the SSH user is `root`, the workflow installs `python3-venv` automatically when it is missing. If you use a non-root `deploy` user, install it manually, then add the user to the `docker` group or configure passwordless sudo and adjust the workflow commands accordingly.

## Optional GitHub variables

The workflow has safe defaults, but these repository variables can override them:

| Variable | Default | Description |
|----------|---------|-------------|
| `DROPLET_APP_DIR` | `/opt/trading-bot/app` | Repository checkout on the Droplet |
| `DROPLET_COMPOSE_DIR` | `/opt/trading-bot` | Directory containing the production Compose file |
| `DROPLET_SERVICE` | `trading-bot` | Compose service to rebuild and restart |
| `DROPLET_BRANCH` | `master` | Branch to pull on the Droplet |

Add them in GitHub under **Settings -> Secrets and variables -> Actions -> Variables** only if your Droplet layout differs from the defaults.

## Droplet setup

Clone the repository into the expected app directory:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv docker.io docker-compose-plugin
sudo mkdir -p /opt/trading-bot
sudo chown "$USER":"$USER" /opt/trading-bot
git clone https://github.com/Nicholas-03/trading-bot.git /opt/trading-bot/app
```

Create the production environment file on the Droplet:

```bash
cd /opt/trading-bot/app
cp .env.example .env
```

Edit `/opt/trading-bot/app/.env` with real API keys and production settings. Do not commit this file.

If the repository is private, give the Droplet read access to GitHub. The usual approach is a read-only deploy key:

```bash
ssh-keygen -t ed25519 -C "trading-bot-droplet" -f ~/.ssh/trading_bot_github
cat ~/.ssh/trading_bot_github.pub
```

Add the printed public key in GitHub under **Settings -> Deploy keys** with read access. Then configure the Droplet checkout to use the matching private key.

## Production Compose stack

The repository includes a simple local `docker-compose.yml`. The production Droplet may keep its Compose file one directory above the checkout, at `/opt/trading-bot/docker-compose.yml`, so it can mount persistent data and add infrastructure services such as Caddy.

The workflow only assumes that the Compose file contains a service named `trading-bot`. A minimal production stack looks like:

```yaml
services:
  trading-bot:
    build: ./app
    container_name: trading-bot
    env_file: ./app/.env
    restart: unless-stopped
    volumes:
      - /mnt/trading-bot-data:/app/data
```

Start it once manually:

```bash
cd /opt/trading-bot
docker compose up -d --build trading-bot
```

After that, pushes to GitHub will update it automatically.

## Operational commands

Check the running service:

```bash
cd /opt/trading-bot
docker compose ps trading-bot
```

Follow logs:

```bash
cd /opt/trading-bot
docker compose logs -f trading-bot
```

Manual redeploy:

```bash
cd /opt/trading-bot/app
git pull --ff-only origin master
cd /opt/trading-bot
docker compose up -d --build --no-deps trading-bot
```
