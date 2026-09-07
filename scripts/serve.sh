#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h:h}"
cd "$project_dir"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
set -a
[[ -f .env ]] && source .env
set +a
exec .venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 \
  --port "${MEDIA_FETCHER_PORT:-8097}"
