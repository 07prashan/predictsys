# Single container running both halves of the app:
#   - the Python pipeline (src/predict.py) that fetches match data, fits the
#     models, and writes predictions into SQLite
#   - the Express server (server/) that reads that same SQLite file read-only
#     and serves the API + static website
#
# Both processes share one filesystem, so /app/data (mounted as a volume in
# production) is the single source of truth for both - no network DB needed.

FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (own venv - Debian's system Python blocks bare pip installs)
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

# Node deps (better-sqlite3 is a native module; build-essential above is the
# fallback if no prebuilt binary matches this platform/Node version)
COPY server/package.json server/package-lock.json server/
RUN npm --prefix server ci --omit=dev

# App source
COPY src/ src/
COPY server/ server/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

VOLUME /app/data

CMD ["./docker-entrypoint.sh"]
