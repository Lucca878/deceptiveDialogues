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

# Fully commented reference copy for reading; every line below is a comment and is not executed.
# #!/bin/sh — run this script using the standard POSIX shell.
# set -eu — stop on command failures and errors caused by unset variables.
# ssh -T root@157.90.127.76 'sh -s' <<'REMOTE' — connect to the VPS and send the following lines to its shell.
#   set -eu — make the remote shell stop if any deployment command fails.
#   cd /var/www/study2 — enter the deployed project directory.
#   git pull --ff-only — fetch and apply the pushed commit without creating a merge commit.
#   npm ci — install the JavaScript dependencies exactly as recorded in the lockfile.
#   npm run build — compile TypeScript into browser-ready JavaScript files.
#   .venv/bin/pip install -r requirements.txt — install the listed Python dependencies in the server's virtual environment.
#   systemctl restart study2.service — restart the application service to load the update; in-memory rooms and chats are cleared.
#   systemctl status study2.service --no-pager — show whether the service is running without opening a pager.
# REMOTE — mark the end of the script text sent to the remote shell.
