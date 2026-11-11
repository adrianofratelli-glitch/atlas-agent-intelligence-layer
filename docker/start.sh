#!/bin/sh
set -e

cd /app/backend
# A single worker owns the MCP supervisor/session; multiple workers duplicated
# that state and made health/rate metrics inconsistent.
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 &
nginx -g 'daemon off;'
