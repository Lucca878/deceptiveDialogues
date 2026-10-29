#!/bin/sh
set -eu

ssh -T root@157.90.127.76 'sh -s' <<'REMOTE'
set -eu
cd /var/www/study2
git pull --ff-only
npm ci
npm run build
.venv/bin/pip install -r requirements.txt
systemctl restart study2.service
systemctl status study2.service --no-pager
REMOTE
