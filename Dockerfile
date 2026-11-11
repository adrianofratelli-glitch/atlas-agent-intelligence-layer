# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
RUN apt-get update \
  && apt-get install -y --no-install-recommends nginx curl \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --create-home --uid 10001 app

WORKDIR /app

# The agent spawns the MongoDB MCP Server with `npx` (backend/agent.py); the
# runtime needs Node >= 20.19 (glibc build, same Debian family as python-slim).
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
# pov-shared (guardrail scoring per intent, injection heuristic, change-stream
# cache invalidation) comes from a named context. Core only: the [tracing]
# extra (OTel sink, opt-in via TRACE_SINK) is left out to keep the image small.
#   docker build --build-context shared=../_shared -t intelligence-layer-poc .
COPY --from=shared . /opt/pov-shared
RUN pip install --no-cache-dir /opt/pov-shared

COPY backend/ ./backend/
COPY --from=frontend-build /build/dist ./frontend/dist
COPY nginx.conf /etc/nginx/nginx.conf
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh && chown -R app:app /app
USER app
# Warm the npx cache so the first MCP session does not download the server.
RUN npx -y mongodb-mcp-server@2.1.0 --version

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["/start.sh"]
