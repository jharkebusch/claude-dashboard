FROM python:3.12-slim

# No dependencies to install — the app is standard library only.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLAUDE_DIR=/claude \
    DATA_DIR=/data \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=7581

WORKDIR /app
COPY serve.py ingest.py limits.py config.json pricing.json index.html ./

# Own /data as uid 1000 so a fresh named volume inherits that ownership and the
# unprivileged runtime user can write the database. The same uid is what makes
# the read-only ~/.claude mount readable, including the 0600 credentials file.
RUN mkdir -p /data && chown -R 1000:1000 /data /app

USER 1000:1000
EXPOSE 7581

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7581/api/snapshot?range=24h', timeout=4)"

CMD ["python3", "serve.py"]
