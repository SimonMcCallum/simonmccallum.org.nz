FROM python:3.12-slim

WORKDIR /app

# Install cron
RUN apt-get update && apt-get install -y --no-install-recommends cron && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY server.py .
COPY static/ static/
COPY scripts/ scripts/
COPY .env* ./

# Create data and log directories
RUN mkdir -p data logs

# Set up cron job — update cache every 5 minutes
RUN echo "*/5 * * * * cd /app && /usr/local/bin/python3 scripts/update_cache.py >> logs/cron.log 2>&1" \
    > /etc/cron.d/energy-cache && \
    chmod 0644 /etc/cron.d/energy-cache && \
    crontab /etc/cron.d/energy-cache

EXPOSE 5050

# Start cron in background, then run gunicorn
CMD cron && gunicorn -w 2 -b 0.0.0.0:5050 server:app
