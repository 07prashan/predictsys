# Single container running both halves of the app:
#   - the Python pipeline (src/predict.py) that fetches match data, fits the
#     models, and writes predictions into SQLite - and then writes the website's
#     JSON snapshots (server/public/data) from it
#   - a tiny Express server (server/) that serves the static website
#
# The website is plain static files, so this container is only ONE way to host it
# (it keeps the pipeline and the site together on one box). The free-hosting route
# in DEPLOY.md runs the pipeline in GitHub Actions and publishes the static files
# to GitHub Pages instead - same code, no always-on machine.

FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (own venv - Debian's system Python blocks bare pip installs)
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

# Node deps (just Express - the server only serves static files)
COPY server/package.json server/package-lock.json server/
RUN npm --prefix server ci --omit=dev

# App source
COPY src/ src/
COPY server/ server/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

VOLUME /app/data

CMD ["./docker-entrypoint.sh"]
