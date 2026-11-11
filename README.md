# Deceptive Dialogues

Web interface for a game-based study collecting deceptive and truthful dialogues.
Two anonymous participants chat by text: an interviewer asks questions trying to
determine credibility, the interviewee tries to convince them (truthfully or not).

**Status:** Flask serves landing, consent, travel screening, a shared pre-match explanation, automatic matching, private role-instruction placeholders, and chat. Both participants must explicitly select **I'm ready** before either can chat. Chat and travel answers are not persisted. Approved role instructions have not been added yet.

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

The study flow is landing → consent → travel introduction → one questionnaire at `/travel-cities` → shared explanation at `/instructions` → **Find a partner** → automatic matching at `/room` → private instructions at `/role-instructions` → each selects **I'm ready** → chat at `/dialogue` only when both are ready. England eligibility is handled by Prolific only; the app asks no location question. The questionnaire shows all 20 preset English cities in the fixed order defined in [city_candidates.py](city_candidates.py), with stable slug IDs and England as their country. For every city, participants must answer Yes/No to visiting in the past 3 years and rate familiarity from 1 to 7. The server validates all 40 fields before checking eligibility: at least five Yes and at least five No answers are required (5–15 Yes inclusive). Familiarity does not affect eligibility. Invalid or missing answers return HTTP 400 with valid selections preserved, not a screen-out. The form needs no JavaScript.

Questionnaire answers are validated then discarded; only consent, introduction progress, and eligibility/completion flags remain in the signed browser session. The versioned completion marker prevents old screening completions from opening the room. Consent and the introduction are prerequisites; room, dialogue, and every room API enforce screening and instructions continuation server-side. Screen-out remains in force on return visits in the same session, including through Start or renewed consent. Clearing cookies or changing browser sessions can reset this prototype state; Prolific identity is not yet authenticated by the app.

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

## Tech stack

- **Frontend:** TypeScript (compiled with esbuild), HTML, plain CSS; no frontend framework
- **Backend:** Python with Flask
- **Hosting:** Hetzner CPX22, Ubuntu (deployed prototype)

## Backend

Flask serves the study pages and temporarily holds room codes, participant roles, session-ownership tokens, readiness sets, and messages in memory. The browser polls Flask once per second for matching, chat availability after readiness, or new messages. Restarting the server clears this state. Production will need durable shared storage, participant authentication, and likely a real-time transport if lower latency is required. The chat is still a free-text prototype: the five-question/turn-order protocol, judgements, and ten-dialogue progression are not implemented by this readiness change.

## Data collection

_To be documented: what is stored, in which format, and how it is exported._

## Deployment

Study 2 is deployed from the public Git repository `https://github.com/Lucca878/deceptiveDialogues` to `/var/www/study2` on the Hetzner VPS. It runs as a single Gunicorn worker on `127.0.0.1:8001`. Its systemd unit and Nginx config are in `deploy/study2.service` and `deploy/study2.nginx.conf`.

### Deploy an update

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