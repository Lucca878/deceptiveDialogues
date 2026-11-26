#!/bin/sh
set -eu

ssh -T root@157.90.127.76 'sh -s' <<'REMOTE'
set -eu
cd /var/www/study2
git pull --ff-only
# Create private persistent storage and a shared export directory outside the web checkout.
install -d -o www-data -g www-data -m 0750 /var/lib/study2 /var/lib/study2/exports
# Install the repository's systemd configuration with the shared database path.
install -m 0644 deploy/study2.service /etc/systemd/system/study2.service
systemctl daemon-reload
npm ci
npm run build
.venv/bin/pip install -r requirements.txt
# Stop the single worker before removing the explicitly discarded legacy TEST database.
systemctl stop study2.service
# Preserve old export bundles under the shared export root before removing the redundant TEST directory.
if [ -d /var/lib/study2/test/exports ]; then
	install -d -o www-data -g www-data -m 0750 /var/lib/study2/exports/legacy_test_exports
	find /var/lib/study2/test/exports -mindepth 1 -maxdepth 1 -exec mv -t /var/lib/study2/exports/legacy_test_exports -- {} +
	rmdir /var/lib/study2/test/exports
fi
# Start this fresh collection with no old TEST archive or SQLite journal sidecars.
rm -f /var/lib/study2/test/study2-test.sqlite /var/lib/study2/test/study2-test.sqlite-wal /var/lib/study2/test/study2-test.sqlite-shm
# Remove only the now-empty legacy directory; unexpected files cause a safe deployment failure instead of deletion.
if [ -d /var/lib/study2/test ]; then
	rmdir /var/lib/study2/test
fi
# Create the legacy compatibility schema as the service account in the shared database file.
sudo -u www-data env STUDY_DATASET=test STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server db upgrade
# Initialize the run-specific raw and metadata tables; the command refuses incompatible existing metadata.
sudo -u www-data env TURNSTILE_ENABLED=true STUDY_DATASET=test STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server init-study-run --run-id test --config config/study_test.json --protocol-version dialogue_v1 --application-revision "$(git rev-parse --short HEAD)"
# Keep the newly created SQLite database private and writable by the service account.
chown www-data:www-data /var/lib/study2/study2.sqlite
chmod 0640 /var/lib/study2/study2.sqlite
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
#   install -d -o www-data -g www-data -m 0750 /var/lib/study2 /var/lib/study2/exports — create shared private runtime and export directories.
#   install -m 0644 deploy/study2.service /etc/systemd/system/study2.service — install the unit with the shared database URL.
#   systemctl daemon-reload — load the updated systemd unit configuration.
#   npm ci — install the JavaScript dependencies exactly as recorded in the lockfile.
#   npm run build — compile TypeScript into browser-ready JavaScript files.
#   .venv/bin/pip install -r requirements.txt — install the listed Python dependencies in the server's virtual environment.
#   systemctl stop study2.service — stop the live single worker before the explicitly authorized fresh-start reset.
#   move /var/lib/study2/test/exports/* into /var/lib/study2/exports/legacy_test_exports — preserve existing export bundles under the common root.
#   rm -f /var/lib/study2/test/study2-test.sqlite and its -wal/-shm files — discard the old TEST database and SQLite sidecars for a fresh collection.
#   rmdir /var/lib/study2/test — remove only the empty legacy directory; unexpected files are not recursively deleted.
#   sudo -u www-data env STUDY_DATASET=test STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server db upgrade — create the compatibility schema in the shared database.
#   sudo -u www-data env STUDY_DATASET=test STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server init-study-run --run-id test --config config/study_test.json --protocol-version dialogue_v1 — create raw_test and metadata_test.
#   chown www-data:www-data /var/lib/study2/study2.sqlite; chmod 0640 /var/lib/study2/study2.sqlite — restrict the database file to the service account and group.
#   systemctl restart study2.service — restart the application service to load the update; active in-memory rooms and chats are cleared.
#   systemctl status study2.service --no-pager — show whether the service is running without opening a pager.
# REMOTE — mark the end of the script text sent to the remote shell.
