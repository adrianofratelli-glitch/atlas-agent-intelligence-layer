#!/usr/bin/env bash
# Boots the whole app: FastAPI (default 8010) in the background + Vite (default 5183) in the foreground.
# Non-standard defaults on purpose: this workspace runs several PoVs side by side and
# 8000/5173 are the common FastAPI/Vite defaults other PoVs (e.g. pix-open-finance) use.
# Override with BACKEND_PORT / PORT env vars if you need something else.
set -e
cd "$(dirname "$0")"

BACKEND_PORT="${BACKEND_PORT:-8010}"

# Preserve any existing listener instead of assuming it belongs to this PoV.
for port in "$BACKEND_PORT" "${PORT:-5183}"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use; no process was stopped."
    exit 1
  fi
done

echo "▶ backend FastAPI :$BACKEND_PORT"
(cd backend && .venv/bin/uvicorn main:app --port "$BACKEND_PORT" &)
sleep 2

if ! curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
  echo "Backend did not become healthy on :$BACKEND_PORT."
  exit 1
fi

echo "▶ frontend Vite :${PORT:-5183}"
cd frontend
[ -x node_modules/.bin/vite ] || npm install
if [ "${POV_DEV:-0}" != "1" ] && {
  [ ! -f dist/index.html ] ||
  [ -n "$(find src -type f -newer dist/index.html -print -quit)" ] ||
  [ package-lock.json -nt dist/index.html ] ||
  [ vite.config.js -nt dist/index.html ];
}; then
  echo "▶ gerando frontend otimizado"
  npm run build
fi
export BACKEND_PORT
if [ "${POV_DEV:-0}" = "1" ]; then
  exec node_modules/.bin/vite --host 127.0.0.1 --port "${PORT:-5183}" --strictPort
fi
exec node_modules/.bin/vite preview --host 127.0.0.1 --port "${PORT:-5183}" --strictPort
