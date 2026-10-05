# poly-maker (github.com/warproxxx/poly-maker), the Polymarket liquidity-rewards bot, as run by deploy.sh.
FROM python:3.12-slim
RUN pip install --no-cache-dir uv && useradd -m -u 1000 bot
WORKDIR /app
COPY app/ /app/
RUN uv sync --frozen --no-dev && chown -R bot /app
COPY check.py /app/check.py
USER bot
# the exchange heartbeat cancels our orders if this process stops; restart brings them back
CMD ["uv", "run", "--no-sync", "polymaker", "run", "--config-dir", "mycfg"]
