# Deceptive Dialogues

Web interface for a game-based study collecting deceptive and truthful dialogues.
Two anonymous participants chat by text: an interviewer asks questions trying to
determine credibility, the interviewee tries to convince them (truthfully or not).

**Status:** Flask serves the study flow, pairs participants, and enforces the dialogue protocol. Active rooms and chats remain in process memory, while completed study records are archived to the configured SQLite database when feedback is submitted. The browser does not receive private condition, correctness, or interviewer question-category data unless the protocol permits it.

## Requirements

- [Node.js](https://nodejs.org/) (LTS version)
- [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) (for the Python/Flask environment)

## Getting started

Run all commands from the project root (the folder containing `package.json`).

```bash
conda env create --file environment.yml  # first time only: create the Python environment
conda activate deceptive-dialogues       # activate it in each new terminal
npm install          # install TypeScript and esbuild (first time only)
npm run dev          # build TypeScript and start the Flask development server
```

The Conda environment pins Python to 3.12 and installs Flask. Open <http://localhost:8000/> after the server starts. Stop it with `Ctrl+C`.

### Cloudflare Turnstile entry verification

Human verification is required for every participant study run. Local development and
automated tests may explicitly disable the challenge, but the deployed study service
forces `TURNSTILE_ENABLED=true`; missing credentials or host allowlists fail closed with
HTTP 503. Create a Cloudflare Turnstile widget in **Managed** mode, configure its
permitted hostnames in Cloudflare, and put the following values in the server-only
`/etc/study2.env` file:

| Variable | Required value when enabled |
| --- | --- |
| `TURNSTILE_ENABLED` | `true` (also accepts `1`, `yes`, `on`) |
| `TURNSTILE_SITE_KEY` | The widget's public site key; intentionally rendered in HTML |
| `TURNSTILE_SECRET_KEY` | The private server-side validation key; never put it in browser code or Git |
| `TURNSTILE_HOSTNAMES` | Explicit comma-separated, exact hostnames, without schemes, ports or paths; for example `lpstudies.net` in production, or `localhost` for a separately configured local widget |
| `FLASK_SECRET_KEY` | A strong randomly generated signing secret, stored securely and kept stable across process restarts; not the Turnstile secret |

The hostnames must match both the Cloudflare widget configuration and the server's
explicit allowlist. The request's Host header never adds an allowed hostname.
When enabled, missing keys, an empty allowlist, or a missing stable signing key return
HTTP 503 rather than bypassing verification. No generated development signing key is
used when enabled at startup. When disabled, the existing temporary development key
fallback remains available; production should still set a stable `FLASK_SECRET_KEY`.

Verification runs **before landing and consent**, and protects all registered study
pages and all read/write APIs globally. Only verification and public static files are
exempt; unknown routes remain 404. The calm entry page discloses Cloudflare's processing
of technical/browser information and IP addresses, links its privacy information, and
explains that verification is not study consent. Turnstile's script loads on this page
before consent; review the provider relationship, lawful basis, ethics approval,
information letter and accessibility/support arrangements before participant use.
People without JavaScript or access to Cloudflare cannot enter while enabled.

The server validates each submitted token via an HTTPS POST to Cloudflare using only
Python's standard library. The request uses a five-second network timeout and reads
at most 16 KiB plus one overflow-detection byte. This is a socket/network timeout,
not an absolute end-to-end deadline. Only strict boolean success, action `study-entry`,
and an allowlisted hostname grant access. Missing, duplicate, empty or over-2048-character
tokens are rejected locally. Invalid responses and network/JSON failures fail closed
with a retry page. No token, private key, additional mouse tracking, or duplicate IP
record is stored by this integration; the optional `remoteip` field is not sent.
Cloudflare still receives the browser's IP through the widget, and existing web-server
access logs are unaffected. Do not configure request-body or secret logging upstream.

Success adds a signed-session verification flag and a configuration fingerprint,
preserving consent, screening, screen-out and room progress, including on GET `/start`.
It always redirects to landing, never to a `next` URL, and never replays an interrupted
POST. API denials are JSON HTTP 403 with `error` and `verification_url`, not HTML
redirects (configuration failures are HTTP 503). Verification responses and all enabled
non-static responses use `Cache-Control: no-store`; proxy/CDN configuration must also
avoid caching study pages/APIs and must not override this header.

There is no repeated challenge during a conversation in the same valid session.
Changing the site key, validation secret or hostname allowlist invalidates its
verification fingerprint; changing the signing key invalidates the entire session
and its study progress. Disabling and re-enabling with identical configuration does
not revoke old signed cookies; use a new site key/configuration if revocation is needed.
Turnstile is a bot-resistance measure, **not identity authentication, one-person-one-entry,
CSRF protection, rate limiting, or proof that later messages were written by a person**.
Cookies can be copied, and clearing them requires a new check. Existing process-memory
storage and protocol limitations remain unchanged.

The study flow is landing → consent → travel introduction → one questionnaire at `/travel-cities` → shared explanation at `/instructions` → **Find a partner** → automatic matching at `/room` → private instructions at `/role-instructions` → each selects **I'm ready** → chat at `/dialogue` only when both are ready. England eligibility is handled by Prolific only; the app asks no location question. The questionnaire loads its ordered city list from [config/city_candidates.json](config/city_candidates.json) through [city_candidates.py](city_candidates.py); run metadata snapshots each city's ID, display name, country, and order. The current list has 20 English cities with stable slug IDs. For every city, participants must answer Yes/No to visiting in the past 3 years and rate familiarity from 1 to 7. The server validates all 40 fields before checking eligibility: at least five Yes and at least five No answers are required (5–15 Yes inclusive). Familiarity does not affect eligibility. Invalid or missing answers return HTTP 400 with valid selections preserved, not a screen-out. The form needs no JavaScript.

Eligible questionnaire submissions are retained privately in `screening_submissions`, an in-process dictionary protected by `rooms_lock`. Each opaque, server-generated random submission ID selects a record containing `version`, a server UTC ISO `timestamp`, and `answers`: all 20 canonical city IDs mapped to `visited` (`yes`/`no`) and `familiarity` (integer 1–7). Invalid or ineligible submissions create no eligible record. The signed, readable browser session contains only the opaque `screening_submission_id` reference alongside existing progress/ownership fields, never city answers or the submission timestamp. Each room privately maps its participant IDs to their own submission IDs in `screening_submission_ids`; room APIs, chat responses and partner pages do not expose these references or answers. No claims, truth assignments, export or database persistence are added by this retention step.

A current versioned completion marker **and a retained record of the current version** are required to open instructions, room, dialogue, or any room API. If process memory is lost on restart, or the version changes, participants must repeat screening even if their signed cookies still contain completed flags. Refreshes, GET `/start`, return visits and renewed consent preserve an existing eligible record and reference. Re-screening replaces only that owner's reference, never another participant's data; previous records remain in memory until restart, with no expiry or cleanup mechanism yet. Consent and the introduction remain prerequisites, and screen-out remains in force on return visits in the same session. Clearing cookies loses the reference without deleting retained records and can reset this prototype state; Prolific identity is not yet authenticated by the app. Storage is single-process only: separate workers would not share submissions or rooms.

The shared explanation says: “In the next part of this study, you will be matched with another human participant for a text conversation. After you have been paired, you will receive instructions for your assigned role.” Viewing it does not grant access: **Find a partner** submits a POST that sets the existing `instructions_complete=True` pre-match flag and redirects to the room. Before continuation, room, role-instruction and dialogue requests redirect to the shared explanation and every room API returns JSON HTTP 403. Both instruction routes require consent and current eligible screening; screened-out participants cannot continue. Completed screening return routes lead to `/instructions`, which redirects to the room if continuation is already complete. Renewed consent preserves this progress; a newly completed screening or new screen-out clears an obsolete instructions flag. This flag records pre-match continuation only, not role readiness or whether instructions were read or understood.

On entering `/room`, participants are automatically paired in arrival order using 20 fixed internal codes (`ROOM01`–`ROOM20`), with capacity for 40 participants. No code entry or sharing is needed. The first arrival waits; the next joins the same room and the server randomly assigns interviewer/interviewee roles. Successful matching binds the participant ID and room to the signed Flask session using a private ownership token also held in room memory. Returning requests from that session reuse the assignment; another session cannot claim it by posting the same ID, and a bound session cannot switch to a different participant ID. At full capacity, new arrivals see an all-rooms-occupied message. Rooms are not recycled, and abandoned waiting participants are not removed automatically yet.

Once paired, each browser opens `/role-instructions`. The server chooses both role and condition from server-side room state; the browser cannot select either. The editable condition map in [config/study_test.json](config/study_test.json) defines the assignment method and a separate instruction source for each role in each condition. The four sources are [feedback interviewer instructions](app/templates/participant_materials/role_instructions/interviewer_feedback.html), [control interviewer instructions](app/templates/participant_materials/role_instructions/interviewer_control.html), [feedback interviewee instructions](app/templates/participant_materials/role_instructions/interviewee_feedback.html), and [control interviewee instructions](app/templates/participant_materials/role_instructions/interviewee_control.html). They currently preserve the same wording by role; the four files can be edited independently when condition-specific text is approved. The server renders only the assigned participant's selected source and never includes the condition label in page data, session state, or room-status responses. The configured method currently supports `equal_random_choice`; add other allocation methods in code and tests before using them in configuration.

**I'm ready** posts to `/role-instructions`; the server records that participant in the room's readiness set under the room lock. Repeated POSTs, refreshes and matching retries do not reset readiness. Until both participants are ready, the ready participant sees only “Waiting for your partner. Please keep this page open.” The page polls the existing `GET /api/rooms/<code>` endpoint, whose `chat_ready` boolean reports whether chat is available; it exposes no partner ID, instruction content or individual readiness flag. Existing `waiting`/`paired` matching semantics remain unchanged. Direct dialogue requests go to matching before pairing, or role instructions before both are ready. Message send/retrieve and typing APIs require both readiness and session-bound membership, returning JSON HTTP 403 otherwise; room-status polling remains available before readiness. Message timestamps remain private, server-generated UTC values.

For local pair testing, use **two separate browser profiles or private contexts** (not two tabs sharing cookies), complete consent and screening, then select **Find a partner** in each. Check the different role placeholders, select **I'm ready** in one and confirm neutral waiting, then select it in the other and confirm both enter chat. Readiness, ownership tokens, rooms, assignments and messages are in memory and disappear on restart. Signed-session binding prevents simple ID swapping but is not durable participant authentication; clearing cookies loses access to an existing assignment, and Prolific identity is still not authenticated. This remains a technical prototype, not a collection-ready study.

## Project structure

```
deceptiveDialogues/
├── app/
│   ├── database.py         # Flask-SQLAlchemy database binding
│   ├── raw_exports.py      # participant and room exports from raw events
│   ├── run_tables.py       # run metadata and raw-event table initialization
│   ├── static/
│   │   ├── css/style.css
│   │   ├── fonts/          # bundled font files and license
│   │   └── js/             # generated JavaScript bundles and source maps
│   └── templates/          # Flask-rendered pages and shared fragments
│       ├── participant_materials/
│       │   ├── consent.html
│       │   ├── debriefing.html
│       │   ├── general_instructions.html
│       │   └── role_instructions/
│       │       ├── interviewer_feedback.html
│       │       ├── interviewer_control.html
│       │       ├── interviewee_feedback.html
│       │       └── interviewee_control.html
│       ├── consent.html
│       ├── debriefing.html
│       ├── dialogue.html
│       ├── feedback.html
│       ├── instructions.html
│       ├── landing.html
│       ├── no_consent.html
│       ├── role_instructions.html
│       ├── room.html
│       ├── screened_out.html
│       ├── study_ended.html
│       ├── travel_cities.html
│       ├── travel_intro.html
│       └── verify_human.html
├── config/
│   ├── city_candidates.json # ordered editable city source, copied into run metadata
│   └── study_test.json      # test-run protocol, targets, and participant materials
├── deploy/
│   ├── study2.nginx.conf
│   └── study2.service
├── frontend/src/            # TypeScript sources compiled into app/static/js/
│   ├── dialogue.ts
│   ├── landing.ts
│   ├── role_instructions.ts
│   └── room.ts
├── tests/                   # screening, matching, verification, export, and run-table tests
├── .github/agents/          # repository-specific coding agent instructions
├── city_candidates.py       # validates and loads the shared city list
├── deploy.sh                # remote deployment script
├── environment.yml          # Conda environment
├── form-prefill.md          # participant-form prefill notes
├── package.json             # npm scripts and frontend build dependencies
├── package-lock.json        # locked npm dependency versions
├── requirements.txt         # Python dependencies
├── server-explanation.md    # backend behavior notes
├── server.py                # Flask routes and study protocol
├── tsconfig.json            # TypeScript type-checking settings
└── README.md
```

TypeScript is written in `frontend/src/` and compiled by esbuild into `app/static/js/`.
Never edit files in `app/static/js/` directly; they are generated build output and are
overwritten on every build. Flask renders files in `app/templates/`; those templates are
not exposed as raw files. Files in `app/static/` are served publicly, so do not put secrets
or private data there. Edit `config/city_candidates.json` to exchange the questionnaire
candidates; keep each ID unique and stable when changing only a display name.

## Scripts

| Command             | What it does                                                    |
| ------------------- | --------------------------------------------------------------- |
| `npm run dev`       | Build TypeScript and start Flask locally (Ctrl+C stops)          |
| `npm run build`     | Compile TypeScript into `app/static/js/` (used for deployment)  |
| `npm run typecheck` | Check types with `tsc` (esbuild does not check types)           |

`npm run dev` builds TypeScript into `app/static/js/` before Flask starts. Re-run `npm run build`
after changing TypeScript while the server is running.

Run `npm run typecheck` before every commit.

Run the backend tests from the project root with the Conda environment active:
`python -m unittest discover -s tests -v`. The suite covers questionnaire validation,
eligibility boundaries, consent/screening/instructions access gates, POST-only
pre-match continuation and return progress, full room capacity, concurrent matching,
private role rendering, session-bound authorization, readiness retries/refreshes/concurrency,
both-ready API gating, and private server-generated message timestamps.

Entry-verification tests use mocked Cloudflare replies only; they need no real keys,
network access or new dependencies. Existing study-test fixtures explicitly disable
Turnstile; there is no `TESTING`-based production bypass. Run the focused suite with
`python -m unittest discover -s tests -p test_human_verification.py -v`, then the full
backend command above, `npm run typecheck`, `npm run build`, and `git diff --check`.
For a later manual smoke test, configure a Managed widget and matching environment,
restart locally, and confirm entry/direct-page/API blocking, successful landing-only
continuation and preserved study progress. Separately test a fresh browser session
and an unavailable-provider/configuration scenario. Mocked tests cannot certify the
live Cloudflare configuration, third-party widget accessibility, or production caching.

## Tech stack

- **Frontend:** TypeScript (compiled with esbuild), HTML, plain CSS; no frontend framework
- **Backend:** Python with Flask
- **Hosting:** Hetzner CPX22, Ubuntu (deployed prototype)

## Backend

Flask serves the study pages and temporarily holds active screening submissions, private room associations, room codes, participant roles, session-ownership tokens, readiness sets, and live room state in memory. The browser polls Flask once per second for matching, chat availability after readiness, or new messages. Restarting the server clears active sessions. Completed study data is archived transactionally at feedback submission, and Prolific handoff times are recorded afterward. The app uses one Gunicorn worker because active rooms are in memory.

## Data collection

The active collection stores run-specific raw event and metadata records in the configured database (`raw_<run_id>` and `metadata_<run_id>`). Active room state is still process-memory only, so a restart clears ongoing sessions; submitted records remain in the configured database. The export command reads raw events and creates separate participant/room CSV/JSON files; downloading the SQLite database file alone does **not** create those exports.

### Target raw-event design

The run-specific tables, initializer, and raw-derived participant/room exports are implemented. The server buffers `human_verification_completed`, `study_started`, `consent_decided`, `screening_completed`, `instructions_completed`, `page_visit`, `room_paired`, `participant_ready`, `dialogue_started`, `message_sent`, `question_type_assigned`, `dialogue_completed`, `judgement_submitted`, `dialogue_continued`, `feedback_submitted`, and `study_completed` events in memory. It batch-saves the accumulated events at feedback; the later completion handoff closes any open page interval and saves `study_completed`. Repeated feedback and completion requests use opaque event IDs to avoid duplicate rows. Feedback and handoff events are written only to the run-specific raw event table.

- Each collection run has a raw event table and a metadata table named `raw_<run_id>` and `metadata_<run_id>`. For the first run, these are `raw_test` and `metadata_test`.
- The raw table includes `event_id`, `study_run_id`, `participant_id`, `room_id`, `room_code`, `event_type`, `dialogue_number`, `turn_number`, `occurred_at`, `started_at`, `ended_at`, `duration_seconds`, and `event_data`. Fields that do not apply to an event are nullable. `study_run_id` is assigned by the table's default and is not sent by the server.
- `event_id` is an opaque unique key. `dialogue_number` and `turn_number` locate an event in the study flow. Names use lowercase `snake_case`; judgement fields retain the server's `judgement` spelling.
- `participant_id` means the stable server-issued study participant ID. `room_participant_id` means the temporary ID used to bind a browser session to room membership. `room_id` is the opaque stable room ID; `room_code` is generated by the server for pairing and is not participant-entered. `prolific_id` is an external nullable ID, not a replacement for `participant_id`.
- The server and browser room API now use `participant_id` only for the stable internal ID and `room_participant_id` for room membership. Existing signed cookies with `study_session_id` migrate to `participant_id` without changing the value. All persisted study data uses run-specific raw event tables.
- `event_data` is one structured JSON object per event, containing event-specific values. `message_sent` stores the message text and role; `question_type_assigned` refers to its question's `event_id`; `judgement_submitted` stores judgement, confidence, and correctness.
- The first study event is `human_verification_completed`. Human verification is required for every run. The Prolific ID is captured from the incoming link in server memory and included in that event; it is not copied into the signed cookie.
- Events receive server timestamps when actions occur, stay in server memory during the study, and are batch-written transactionally when feedback is submitted. The completion handoff records final timing afterward. A process restart before feedback still loses the active in-memory study, matching current behavior.
- `room_paired` records both stable participant IDs, their assigned roles, the assigned `condition`, `room_id`, and generated `room_code`. `dialogue_started` records `started_at`, `dialogue_number`, and the claim, city ID, truth status, and interviewee participant ID. `message_sent` records the stable actor ID, role, message text, dialogue number, and turn number. `question_type_assigned` records the category and its target message `event_id`. `dialogue_completed` and `judgement_submitted` carry intervals needed for their duration columns; `page_visit` intervals produce participant page-duration totals.
- The metadata table stores the run ID, creation time, schema and protocol versions, application revision, and an immutable configuration snapshot. The snapshot includes the ordered city candidate IDs, display names, countries, and source path from `config/city_candidates.json`; the allocation method and condition names; and the source path and full text of every condition/role instruction variant. It also records dialogue/question counts, screening version and allowed question types, but never secrets or participant-level assignments.
- The canonical `export-study` command derives participant and room CSV/JSON files from `raw_<run_id>`, not the legacy tables. The room-wide export remains one row per dialogue, with columns such as `interviewer_message_5`, `question_type_interviewer_message_5`, and `interviewee_message_5`; `dialogue_number` identifies which dialogue that row represents. Output headers use lowercase `snake_case`, including `prolific_id`, `interviewee_participant_id`, and `judgement`. `export-study` is the only export command.
- Initialize a run with `flask --app server init-study-run --run-id test --config config/study_test.json --protocol-version dialogue_v1`. The command validates the run ID, creates `raw_test` and `metadata_test`, and stores the immutable configuration snapshot, including the actual city list. Repeating it with matching configuration is safe; it refuses changed metadata or partial schemas. The explicit `--replace-existing` option deletes and recreates both tables, including all raw events; the development deployment uses this reset and must not be used once research data needs to be retained.
- Exports are written directly under `/var/lib/study2/exports/`, with a run ID and timestamp in each filename. The live SQLite database is `/var/lib/study2/study2.sqlite`; table names are inside that database file.

#### Export CSV and JSON files from the VPS

Run these commands in a terminal on your Mac from any directory. They connect to the VPS, write timestamped TEST-run exports directly into the shared private exports directory, then download its contents to `~/study2-exports/`.

```sh
ssh root@157.90.127.76 "cd /var/www/study2 && sudo -u www-data env STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server export-study --output /var/lib/study2/exports --run-id test"
mkdir -p -m 700 "$HOME/study2-exports"
scp -r "root@157.90.127.76:/var/lib/study2/exports/." "$HOME/study2-exports/"
```

Each export creates `participants_test_<timestamp>.csv`, `rooms_test_<timestamp>.csv`, corresponding JSON files, and one flat JSON file per room. These exports contain research data: keep them private, do not commit them to Git, and do not put them in a public web directory. The SSH command may prompt for your server login; enter credentials only in your terminal.

#### Export raw and metadata tables directly (optional)

If you want the raw event table and metadata table directly (instead of derived participant/room exports), run:

```sh
ssh root@157.90.127.76 "sqlite3 -header -csv /var/lib/study2/study2.sqlite 'SELECT * FROM raw_test;'" > "$HOME/raw_test.csv"
ssh root@157.90.127.76 "sqlite3 -header -csv /var/lib/study2/study2.sqlite 'SELECT * FROM metadata_test;'" > "$HOME/metadata_test.csv"
```

To target a different run, replace `raw_test` and `metadata_test` with `raw_<run_id>` and `metadata_<run_id>`.

To download the raw SQLite database instead, use this separate command (it does not generate CSV/JSON files):

```sh
scp root@157.90.127.76:/var/lib/study2/study2.sqlite "$HOME/study2.sqlite"
```

If the database may be receiving writes while you copy it, prefer the export steps above so the app creates a consistent set of CSV/JSON files using SQLite's normal database connection.

To open the export folder in finder run:

```sh
open "$HOME/study2-exports"
```

#### Operator checklist

Use this runbook for routine collection/export operations.

1. Initialize run tables (once per run ID).

```sh
ssh root@157.90.127.76 "cd /var/www/study2 && sudo -u www-data env TURNSTILE_ENABLED=true STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server init-study-run --run-id test --config config/study_test.json --protocol-version dialogue_v1"
```

2. Verify run tables exist.

```sh
ssh root@157.90.127.76 "sqlite3 /var/lib/study2/study2.sqlite \"SELECT name FROM sqlite_master WHERE type='table' AND name IN ('raw_test','metadata_test');\""
```

3. Export participant/room CSV+JSON from raw events.

```sh
ssh root@157.90.127.76 "cd /var/www/study2 && sudo -u www-data env STUDY_RUN_ID=test STUDY_DATABASE_URL=sqlite:////var/lib/study2/study2.sqlite .venv/bin/flask --app server export-study --output /var/lib/study2/exports --run-id test"
```

4. Export raw and metadata tables directly (optional).

```sh
ssh root@157.90.127.76 "sqlite3 -header -csv /var/lib/study2/study2.sqlite 'SELECT * FROM raw_test;'" > "$HOME/raw_test.csv"
ssh root@157.90.127.76 "sqlite3 -header -csv /var/lib/study2/study2.sqlite 'SELECT * FROM metadata_test;'" > "$HOME/metadata_test.csv"
```

5. Download export bundle to your Mac.

```sh
mkdir -p -m 700 "$HOME/study2-exports"
scp -r "root@157.90.127.76:/var/lib/study2/exports/." "$HOME/study2-exports/"
```

6. Run a quick event count sanity check.

```sh
ssh root@157.90.127.76 "sqlite3 /var/lib/study2/study2.sqlite \"SELECT event_type, COUNT(*) FROM raw_test GROUP BY event_type ORDER BY event_type;\""
```

## Deployment

Study 2 is deployed from the public Git repository `https://github.com/Lucca878/deceptiveDialogues` to `/var/www/study2` on the Hetzner VPS. It runs as a single Gunicorn worker on `127.0.0.1:8001`. Its systemd unit and Nginx config are in `deploy/study2.service` and `deploy/study2.nginx.conf`.

### Deploy an update

#### One-time environment configuration for verification (manual, not performed here)

[deploy/study2.service](deploy/study2.service) reads the optional systemd environment
file `/etc/study2.env` for the private Turnstile credentials, allowlist, and stable
Flask signing key. Create that file on the server as root-owned, mode **0600**, outside
the checkout, using systemd `NAME=value` syntax (no `export`). Keep private values out
of Git, screenshots, and command history. The service forces `TURNSTILE_ENABLED=true`;
if required verification configuration is absent or invalid, participants receive a
closed 503 response instead of bypassing the challenge. Schedule deployment outside
participant sessions because restarting clears active rooms and chats.

The deployment script installs the unit and reloads systemd. For a manual unit edit,
copy `deploy/study2.service` to `/etc/systemd/system/study2.service`, then run
`systemctl daemon-reload` and restart the service. Check
`systemctl status study2.service --no-pager` without dumping environment secrets.
After later edits to the environment file, restart the service. The unit labels the run
`test` and points SQLite to `/var/lib/study2/study2.sqlite`, outside the checkout. The
unit sets `STUDY_CONFIG_PATH` to `/var/www/study2/config/study_test.json`; this is the
same file passed to `init-study-run` so runtime behavior matches the metadata snapshot.
The deployment script creates the shared database and exports directories and initializes
`raw_test` and `metadata_test` for the `test` run. This is a development reset: every
deployment deletes and recreates both tables, including all raw events, then writes a
fresh metadata snapshot from the current configuration. Do not use this deployment
behavior once research data must be retained; production collection should initialize
a fresh run ID instead. This deployment also removes the old
`/var/lib/study2/test/study2-test.sqlite` database and its SQLite sidecars. Running
locally without `STUDY_DATABASE_URL` uses ephemeral
in-memory SQLite; it creates no persistent local database file. The database file is
runtime data and is not committed. The database contains only run-specific raw event and metadata tables. Feedback and handoff
recording is implemented. No production keys or configuration are supplied by this repo.

First, test the change locally, then commit and push it to the `main` branch.
From the repository root on your Mac, run:

```bash
sh deploy.sh
```

The script connects to the VPS over SSH, pulls the latest changes in `/var/www/study2`,
installs the updated systemd unit and dependencies, builds the frontend, initializes
the TEST run tables with a full development reset, restarts
`study2.service`, and displays its status.
It stops if a command fails. SSH access to `root@157.90.127.76` is required;
enter any SSH password or key passphrase directly in your terminal if prompted.
Restarting clears all active rooms and chats. After deployment, check the site at
<https://lpstudies.net>.

### Switch which study `lpstudies.net` serves

Study 1 is at `/var/www/study1`; study 2 is at `/var/www/study2`. Both deployments remain on the server. To serve study 2 at the domain root:

```bash
ln -sfn /etc/nginx/sites-available/lpstudies-study2 /etc/nginx/sites-enabled/lpstudies
nginx -t && systemctl reload nginx
```

To switch back to study 1:

```bash
ln -sfn /etc/nginx/sites-available/lpstudies /etc/nginx/sites-enabled/lpstudies
nginx -t && systemctl reload nginx
```

The room and message state is in process memory, so the one-worker service is intentional for this prototype. Restarting it clears active rooms and chats. This is suitable for an online technical test, not a real participant collection run until persistent storage and production participant authentication are added.