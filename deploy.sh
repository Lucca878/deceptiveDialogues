#!/bin/sh
set -eu

ssh -T root@157.90.127.76 'sh -s' <<'REMOTE'
set -eu
cd /var/www/study2
git pull --ff-only
# Create a private persistent TEST-data directory owned by the service account, outside the Git checkout.
install -d -o www-data -g www-data -m 0750 /var/lib/study2/test
# Correct ownership on an existing archive before the service account runs migrations.
if [ -f /var/lib/study2/test/study2-test.sqlite ]; then
	chown www-data:www-data /var/lib/study2/test/study2-test.sqlite
	chmod 0640 /var/lib/study2/test/study2-test.sqlite
fi
# Install the repository's systemd configuration so the app receives its explicit TEST database URL.
install -m 0644 deploy/study2.service /etc/systemd/system/study2.service
systemctl daemon-reload
npm ci
npm run build
.venv/bin/pip install -r requirements.txt
# Create or upgrade the TEST database as the same account that runs the app, avoiding a root-owned SQLite file.
sudo -u www-data env STUDY_DATASET=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/test/study2-test.sqlite .venv/bin/flask --app server db upgrade
chmod 0640 /var/lib/study2/test/study2-test.sqlite
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
#   install -d -o www-data -g www-data -m 0750 /var/lib/study2/test — create the private persistent TEST database directory.
#   install -m 0644 deploy/study2.service /etc/systemd/system/study2.service — install the unit with the TEST database URL.
#   systemctl daemon-reload — load the updated systemd unit configuration.
#   npm ci — install the JavaScript dependencies exactly as recorded in the lockfile.
#   npm run build — compile TypeScript into browser-ready JavaScript files.
#   .venv/bin/pip install -r requirements.txt — install the listed Python dependencies in the server's virtual environment.
#   if [ -f /var/lib/study2/test/study2-test.sqlite ]; then chown www-data:www-data /var/lib/study2/test/study2-test.sqlite; chmod 0640 /var/lib/study2/test/study2-test.sqlite; fi — keep an existing database writable and private for the service user.
#   sudo -u www-data env STUDY_DATASET=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/test/study2-test.sqlite .venv/bin/flask --app server db upgrade — create/upgrade the VPS TEST database schema as the service user.
#   chmod 0640 /var/lib/study2/test/study2-test.sqlite — restrict the database file to its owner and service group.
#   systemctl restart study2.service — restart the application service to load the update; active in-memory rooms and chats are cleared.
#   systemctl status study2.service --no-pager — show whether the service is running without opening a pager.
# REMOTE — mark the end of the script text sent to the remote shell.
