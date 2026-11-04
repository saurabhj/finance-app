#!/bin/bash
# Production web server: gunicorn on localhost only. Reach it from your devices
# through `tailscale serve` (HTTPS, tailnet-only); see README "Running on the Mac mini".
set -euo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate
python3 manage.py migrate --noinput
exec gunicorn finance.wsgi:application \
  --bind 127.0.0.1:${PORT:-8765} \
  --workers 2 --threads 4 \
  --timeout 600 \
  --access-logfile - --error-logfile -
