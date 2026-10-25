FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Time zone data, so TZ (e.g. TZ=Europe/Madrid) applies to ACTIVE_HOURS and log timestamps
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create non-root user and group (UID/GID 1000)
RUN groupadd -r findly -g 1000 && \
    useradd -r -u 1000 -g findly -d /app -s /sbin/nologin findly

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=findly:findly . .

# Ensure data directory exists and assign ownership to non-root user
RUN mkdir -p /app/data && chown findly:findly /app/data

USER findly

EXPOSE 8000

# Check that the web UI responds (slim image has no curl, so use Python)
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/stats', timeout=4)" || exit 1

CMD ["python", "main.py"]
