#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h:h}"
cd "$project_dir"
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg is required (brew install ffmpeg)"; exit 1; }
command -v deno >/dev/null || { echo "deno is required for full YouTube support (brew install deno)"; exit 1; }
[[ -f .env ]] || cp .env.example .env
uv sync
mkdir -p data/downloads
chmod 700 data data/downloads
echo "media-fetcher environment is ready."
