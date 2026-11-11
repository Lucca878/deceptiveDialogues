# Deceptive Dialogues

Web interface for a game-based study collecting deceptive and truthful dialogues.
Two anonymous participants chat by text: an interviewer asks questions trying to
determine credibility, the interviewee tries to convince them (truthfully or not).

**Status:** Flask serves the landing, consent, travel-screening, room setup, and chat pages. Paired participants can exchange messages; chat and travel answers are not persisted.

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

The study flow is landing → consent → travel introduction → one questionnaire at `/travel-cities` → automatic matching at `/room` → chat at `/dialogue`. England eligibility is handled by Prolific only; the app asks no location question. The questionnaire shows all 20 preset English cities in the fixed order defined in [city_candidates.py](city_candidates.py), with stable slug IDs and England as their country. For every city, participants must answer Yes/No to visiting in the past 3 years and rate familiarity from 1 to 7. The server validates all 40 fields before checking eligibility: at least five Yes and at least five No answers are required (5–15 Yes inclusive). Familiarity does not affect eligibility. Invalid or missing answers return HTTP 400 with valid selections preserved, not a screen-out. The form needs no JavaScript.

Questionnaire answers are validated then discarded; only consent, introduction progress, and eligibility/completion flags remain in the signed browser session. The versioned completion marker prevents old screening completions from opening the room. Consent and the introduction are prerequisites; room, dialogue, and every room API enforce screening server-side. Screen-out remains in force on return visits in the same session, including through Start or renewed consent. Clearing cookies or changing browser sessions can reset this prototype state; Prolific identity is not yet authenticated by the app.

On entering `/room`, participants are automatically paired in arrival order using 20 fixed internal codes (`ROOM01`–`ROOM20`), with capacity for 40 participants. No code entry or sharing is needed. The first arrival waits; the next joins the same room and the server randomly assigns interviewer/interviewee roles. Returning requests with the same participant ID reuse the existing assignment. Once paired, both browsers enter the dialogue. At full capacity, new arrivals see an all-rooms-occupied message. Rooms are not recycled, and abandoned waiting participants are not removed automatically yet. Restarting the server clears every room, assignment, and chat message. For local pair testing, use two separate browser contexts with distinct participant IDs and complete the questionnaire in each.

## Project structure

```
deceptiveDialogues/
├── app/                    # everything served to the browser
│   ├── templates/          # HTML pages rendered by Flask
│   │   ├── landing.html
│   │   ├── consent.html
│   │   ├── travel_intro.html
│   │   ├── travel_cities.html
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
eligibility boundaries, access gates, full room capacity, concurrent matching, and
private server-generated message timestamps.

## Tech stack

- **Frontend:** TypeScript (compiled with esbuild), HTML, plain CSS; no frontend framework
- **Backend:** Python with Flask
- **Hosting:** Hetzner CPX22, Ubuntu (deployed prototype)

## Backend

Flask serves the landing, room setup, and dialogue pages and temporarily holds room codes, participant roles, and messages in memory. The browser polls Flask once per second for new messages. Restarting the server clears all rooms and messages. Production will need durable shared storage, stronger participant authentication, and likely a real-time transport if lower latency is required.

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