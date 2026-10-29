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

The study flow is landing → consent → travel introduction → five-city questionnaire → room setup at `/room` → chat at `/dialogue`. The questionnaire checks five distinct city/country pairs and visit months within the rolling 36-month window. Answers are validated but discarded; only temporary session flags enforce the flow. Participants who cannot provide five cities are screened out. Create a room and share its six-character code, or join using a code. When the second participant joins, Flask randomly assigns one interviewer and one interviewee, then both are sent to the chat page. Messages are sent through Flask and appear for both participants. Chat data exists only in server memory and disappears when the server restarts.

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
│       ├── dialogue.ts
│       └── travel.ts
├── server.py               # Flask routes
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

First, test the change locally, then commit and push it to the `main` branch. On the VPS, pull and restart study 2:

```bash
ssh root@157.90.127.76
cd /var/www/study2
git pull --ff-only
npm ci
npm run build
.venv/bin/pip install -r requirements.txt
systemctl restart study2.service
systemctl status study2.service --no-pager
```

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