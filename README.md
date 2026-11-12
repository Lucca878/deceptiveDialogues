# Deceptive Dialogues

Web interface for a game-based study collecting deceptive and truthful dialogues.
Two anonymous participants chat by text: an interviewer asks questions trying to
determine credibility, the interviewee tries to convince them (truthfully or not).

**Status:** Flask serves landing, consent, travel screening, a shared pre-match explanation, automatic matching, private role instructions, and chat. Both participants must explicitly select **I'm ready** before either can chat. Chat and eligible travel answers are held only in process memory, not persisted. Claim generation and dialogue progression are not implemented yet, even where described in the role instructions.

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

### Optional Cloudflare Turnstile entry verification

**Not active by default.** `TURNSTILE_ENABLED` defaults to `false`; adding this code or
supplying keys alone does not enable verification. To activate it, create a Cloudflare
Turnstile widget in **Managed** mode, configure its permitted hostnames in Cloudflare,
and supply the following environment variables before starting/restarting Flask:

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

The study flow is landing → consent → travel introduction → one questionnaire at `/travel-cities` → shared explanation at `/instructions` → **Find a partner** → automatic matching at `/room` → private instructions at `/role-instructions` → each selects **I'm ready** → chat at `/dialogue` only when both are ready. England eligibility is handled by Prolific only; the app asks no location question. The questionnaire shows all 20 preset English cities in the fixed order defined in [city_candidates.py](city_candidates.py), with stable slug IDs and England as their country. For every city, participants must answer Yes/No to visiting in the past 3 years and rate familiarity from 1 to 7. The server validates all 40 fields before checking eligibility: at least five Yes and at least five No answers are required (5–15 Yes inclusive). Familiarity does not affect eligibility. Invalid or missing answers return HTTP 400 with valid selections preserved, not a screen-out. The form needs no JavaScript.

Eligible questionnaire submissions are retained privately in `screening_submissions`, an in-process dictionary protected by `rooms_lock`. Each opaque, server-generated random submission ID selects a record containing `version`, a server UTC ISO `timestamp`, and `answers`: all 20 canonical city IDs mapped to `visited` (`yes`/`no`) and `familiarity` (integer 1–7). Invalid or ineligible submissions create no eligible record. The signed, readable browser session contains only the opaque `screening_submission_id` reference alongside existing progress/ownership fields, never city answers or the submission timestamp. Each room privately maps its participant IDs to their own submission IDs in `screening_submission_ids`; room APIs, chat responses and partner pages do not expose these references or answers. No claims, truth assignments, export or database persistence are added by this retention step.

A current versioned completion marker **and a retained record of the current version** are required to open instructions, room, dialogue, or any room API. If process memory is lost on restart, or the version changes, participants must repeat screening even if their signed cookies still contain completed flags. Refreshes, GET `/start`, return visits and renewed consent preserve an existing eligible record and reference. Re-screening replaces only that owner's reference, never another participant's data; previous records remain in memory until restart, with no expiry or cleanup mechanism yet. Consent and the introduction remain prerequisites, and screen-out remains in force on return visits in the same session. Clearing cookies loses the reference without deleting retained records and can reset this prototype state; Prolific identity is not yet authenticated by the app. Storage is single-process only: separate workers would not share submissions or rooms.

The shared explanation says: “In the next part of this study, you will be matched with another human participant for a text conversation. After you have been paired, you will receive instructions for your assigned role.” Viewing it does not grant access: **Find a partner** submits a POST that sets the existing `instructions_complete=True` pre-match flag and redirects to the room. Before continuation, room, role-instruction and dialogue requests redirect to the shared explanation and every room API returns JSON HTTP 403. Both instruction routes require consent and current eligible screening; screened-out participants cannot continue. Completed screening return routes lead to `/instructions`, which redirects to the room if continuation is already complete. Renewed consent preserves this progress; a newly completed screening or new screen-out clears an obsolete instructions flag. This flag records pre-match continuation only, not role readiness or whether instructions were read or understood.

On entering `/room`, participants are automatically paired in arrival order using 20 fixed internal codes (`ROOM01`–`ROOM20`), with capacity for 40 participants. No code entry or sharing is needed. The first arrival waits; the next joins the same room and the server randomly assigns interviewer/interviewee roles. Successful matching binds the participant ID and room to the signed Flask session using a private ownership token also held in room memory. Returning requests from that session reuse the assignment; another session cannot claim it by posting the same ID, and a bound session cannot switch to a different participant ID. At full capacity, new arrivals see an all-rooms-occupied message. Rooms are not recycled, and abandoned waiting participants are not removed automatically yet.

Once paired, each browser opens `/role-instructions`. The server chooses the role from the session-bound room membership, never from browser storage or a role query parameter. Only the assigned placeholder is rendered: “Interviewer instructions will be added here.” or “Interviewee instructions will be added here.” The other role's text is not included in the HTML or status JSON. These placeholders must be replaced with approved content before participant collection.

**I'm ready** posts to `/role-instructions`; the server records that participant in the room's readiness set under the room lock. Repeated POSTs, refreshes and matching retries do not reset readiness. Until both participants are ready, the ready participant sees only “Waiting for your partner. Please keep this page open.” The page polls the existing `GET /api/rooms/<code>` endpoint, whose `chat_ready` boolean reports whether chat is available; it exposes no partner ID, instruction content or individual readiness flag. Existing `waiting`/`paired` matching semantics remain unchanged. Direct dialogue requests go to matching before pairing, or role instructions before both are ready. Message send/retrieve and typing APIs require both readiness and session-bound membership, returning JSON HTTP 403 otherwise; room-status polling remains available before readiness. Message timestamps remain private, server-generated UTC values.

For local pair testing, use **two separate browser profiles or private contexts** (not two tabs sharing cookies), complete consent and screening, then select **Find a partner** in each. Check the different role placeholders, select **I'm ready** in one and confirm neutral waiting, then select it in the other and confirm both enter chat. Readiness, ownership tokens, rooms, assignments and messages are in memory and disappear on restart. Signed-session binding prevents simple ID swapping but is not durable participant authentication; clearing cookies loses access to an existing assignment, and Prolific identity is still not authenticated. This remains a technical prototype, not a collection-ready study.

## Project structure

```
deceptiveDialogues/
├── app/                    # everything served to the browser
│   ├── templates/          # HTML pages rendered by Flask
│   │   ├── verify_human.html
│   │   ├── landing.html
│   │   ├── consent.html
│   │   ├── travel_intro.html
│   │   ├── travel_cities.html
│   │   ├── instructions.html
│   │   ├── role_instructions.html
│   │   ├── screened_out.html
│   │   ├── room.html
│   │   └── dialogue.html
│   └── static/
│       ├── css/style.css   # all styling
│       └── js/             # compiled JavaScript (generated, not committed)
├── frontend/
│   └── src/                # TypeScript source
│       ├── landing.ts
│       ├── room.ts
│       ├── role_instructions.ts
│       └── dialogue.ts
├── server.py               # Flask routes
├── city_candidates.py      # fixed 20-city questionnaire order and stable IDs
├── environment.yml         # Conda Python and Flask dependencies
├── package.json            # npm scripts and build tools
├── tsconfig.json           # TypeScript type-checking settings
└── README.md
```

TypeScript is written in `frontend/src/` and compiled by esbuild into `app/static/js/`.
Never edit files in `app/static/js/` directly; they are overwritten on every build.
Flask renders files in `app/templates/`; those templates are not exposed as raw files. Files in
`app/static/` are served publicly, so do not put secrets or private data there.

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

Flask serves the study pages and temporarily holds eligible screening submissions, their private room associations, room codes, participant roles, session-ownership tokens, readiness sets, and messages in memory. The browser polls Flask once per second for matching, chat availability after readiness, or new messages. Restarting the server clears this state. Production will need durable shared storage, participant authentication, and likely a real-time transport if lower latency is required. The chat is still a free-text prototype: claim generation, turn-order/question limits, judgements, and ten-dialogue progression are not implemented by this retention change.

## Data collection

Eligible travel records contain the validated visitation/familiarity values, screening version and server UTC ISO submission timestamp described above. Existing messages also have private server-generated UTC timestamps. These records are process-memory only and are lost on restart; there is no database, file persistence, export, claim generation or truth assignment yet. This step is not a durable research-data collection system.

## Deployment

Study 2 is deployed from the public Git repository `https://github.com/Lucca878/deceptiveDialogues` to `/var/www/study2` on the Hetzner VPS. It runs as a single Gunicorn worker on `127.0.0.1:8001`. Its systemd unit and Nginx config are in `deploy/study2.service` and `deploy/study2.nginx.conf`.

### Deploy an update

#### One-time environment configuration for verification (manual, not performed here)

[deploy/study2.service](deploy/study2.service) includes the optional systemd directive
`EnvironmentFile=-/etc/study2.env`. Create that environment file on the server as
root-owned, mode **0600**, outside the checkout, and enter the variables listed above
there using systemd `NAME=value` syntax (no `export`). Keep all private values out of
Git, screenshots and command history. The leading minus makes the file optional:
without an explicit enable setting the feature remains **off**, not auto-enabled.
Schedule activation outside participant sessions: restarting clears rooms and chats.

Installing the updated unit requires a separate manual step on the server:
`cp /var/www/study2/deploy/study2.service /etc/systemd/system/study2.service`, then
`systemctl daemon-reload` and `systemctl restart study2.service`. Check
`systemctl status study2.service --no-pager` without dumping environment secrets.
After later edits to the environment file, restart the service; after unit-file edits,
run `daemon-reload` before restarting. [deploy.sh](deploy.sh) neither installs the unit
nor runs `daemon-reload`; its ordinary application restart does not install these
configuration changes. No production keys or configuration are supplied by this repo.

First, test the change locally, then commit and push it to the `main` branch.
From the repository root on your Mac, run:

```bash
sh deploy.sh
```

The script connects to the VPS over SSH, pulls the latest changes in `/var/www/study2`,
installs dependencies, builds the frontend, restarts `study2.service`, and displays
its status. It stops if a command fails. SSH access to `root@157.90.127.76` is required;
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