# Forward paper test of the post-news drift strategy (scripts/drift5d_paper.py), running unattended.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DRIFT_DATA_DIR=/data
RUN pip install --no-cache-dir "python-dotenv>=1,<2" tzdata \
 && useradd -u 1000 -m bot && mkdir /data && chown bot /data
WORKDIR /app
COPY scripts/build_alpaca_labels.py scripts/drift5d_paper.py scripts/jev_score.py scripts/jev_drift.py scripts/
USER bot
# healthy while the daemon touches /data/heartbeat (every minute, also while waiting for the session)
HEALTHCHECK --interval=2m --timeout=10s --start-period=3m \
  CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime('/data/heartbeat') > 300)"
CMD ["python", "scripts/drift5d_paper.py", "daemon"]
