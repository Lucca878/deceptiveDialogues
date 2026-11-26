from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from http.client import HTTPException
import json
from os import environ
from secrets import choice
from secrets import token_hex
from threading import Lock
from time import monotonic
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from random import sample, shuffle

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from app.database import db, migrate
from app import database as _database_models  # Register tables so Flask-Migrate can inspect the complete schema.
from app.study_data import archive_completed_room, record_prolific_handoff, register_study_data
from app.run_tables import register_run_table_commands
from city_candidates import CANDIDATE_CITIES

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.config.update(
    TURNSTILE_ENABLED=environ.get("TURNSTILE_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on"),
    TURNSTILE_SITE_KEY=environ.get("TURNSTILE_SITE_KEY", "").strip(),
    TURNSTILE_SECRET_KEY=environ.get("TURNSTILE_SECRET_KEY", "").strip(),
    TURNSTILE_HOSTNAMES=tuple(sorted({
        host.strip() for host in environ.get("TURNSTILE_HOSTNAMES", "").split(",") if host.strip()
    })),
    FLASK_SECRET_KEY=environ.get("FLASK_SECRET_KEY", "").strip(),
    # Keep the destination configurable so the approved Prolific completion URL can replace this temporary general link.
    PROLIFIC_COMPLETION_URL=environ.get("PROLIFIC_COMPLETION_URL", "https://www.prolific.com/").strip(),
    # Label this initial archive as test data and permit an explicit VPS database URL override.
    STUDY_DATASET=environ.get("STUDY_DATASET", "test").strip().lower() or "test",
    # Name the run-specific raw and metadata tables; keep this distinct from legacy archive filtering during migration.
    STUDY_RUN_ID=environ.get("STUDY_RUN_ID", "test").strip().lower() or "test",
    # Local use is ephemeral; the VPS service explicitly supplies its persistent TEST database URL.
    SQLALCHEMY_DATABASE_URI=environ.get("STUDY_DATABASE_URL", "sqlite:///:memory:"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)
# Bind Flask-SQLAlchemy and Flask-Migrate after configuring the app; no tables or study rows are created yet.
db.init_app(app)
migrate.init_app(app, db)
register_study_data(app)
register_run_table_commands(app)
# A generated development key must never count as a configured verification key.
app.config["SECRET_KEY"] = app.config["FLASK_SECRET_KEY"] or (
    None if app.config["TURNSTILE_ENABLED"] else token_hex(32)
)
rooms = {}
rooms_lock = Lock()
# Private eligible answers and room associations share rooms_lock. Never serialize
# these records into a browser session, template, or API response.
screening_submissions = {}
# Keep page timing outside the signed browser cookie; this state will be archived at feedback submission.
study_sessions = {}
STUDY_PAGE_ENDPOINTS = {
    "verify_human": "verification",
    # Turnstile is optional; when enabled, its rendered challenge is part of verification duration.
    "landing": "landing",
    "consent": "consent",
    "consent_decision": "consent-decision",
    "travel_intro": "travel-intro",
    "travel_cities": "travel-cities",
    "instructions": "instructions",
    "room": "room",
    "role_instructions": "role-instructions",
    "dialogue": "dialogue",
    "feedback": "feedback",
    "debriefing": "debriefing",
    "study_ended": "study-ended",
    "screened_out_cities": "screened-out",
    "submit_feedback": "feedback",
}
ROOM_CODES = tuple(f"ROOM{number:02d}" for number in range(1, 10001))
SCREENING_VERSION = "english-cities-v1"
TURNSTILE_ACTION = "study-entry"
TURNSTILE_TIMEOUT_SECONDS = 5
TURNSTILE_MAX_RESPONSE_BYTES = 16384


def _utc_timestamp():
    """Return a server-generated UTC ISO timestamp for study events."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ensure_participant_id(started=False):
    """Create or migrate the stable participant ID and remember study start time."""
    participant_id = session.get("participant_id")
    if not isinstance(participant_id, str) or not participant_id:
        # Preserve existing participants' IDs when their signed cookie still has the old key.
        participant_id = session.pop("study_session_id", None)
        if not isinstance(participant_id, str) or not participant_id:
            participant_id = token_hex(16)
        session["participant_id"] = participant_id
    else:
        # Remove the legacy alias after migration so one identity has one canonical cookie key.
        session.pop("study_session_id", None)

    if started and not session.get("study_started_at"):
        session["study_started_at"] = _utc_timestamp()

    with rooms_lock:
        state = study_sessions.setdefault(participant_id, {
            "study_started_at": session.get("study_started_at"),
            "consent_accepted": session.get("consent_accepted") is True,
            "consent_accepted_at": session.get("consent_accepted_at"),
            "entry_verified": _human_verified(),
            "travel_intro_seen": session.get("travel_intro_seen") is True,
            "travel_screened_out": session.get("travel_screened_out") is True,
            "screening_complete_version": session.get("travel_screening_complete"),
            "instructions_complete": session.get("instructions_complete") is True,
            "page_visits": [],
            "open_page_visit": None,
        })
        # A session cookie may outlive process memory; restore only its opaque ID and start timestamp.
        if state.get("study_started_at") is None:
            state["study_started_at"] = session.get("study_started_at")
    return participant_id


def _update_study_session(participant_id, **values):
    """Update server-side study metadata without copying private credentials into the archive."""
    if not participant_id:
        return
    with rooms_lock:
        state = study_sessions.get(participant_id)
        if state is not None:
            state.update(values)


def _close_open_page_visit(participant_id, exited_at):
    """Close the current page interval using a server timestamp, if one is open."""
    with rooms_lock:
        state = study_sessions.get(participant_id)
        visit = state.get("open_page_visit") if state else None
        if not visit or visit.get("exited_at") is not None:
            return
        visit["exited_at"] = exited_at
        entered_at = datetime.fromisoformat(visit["entered_at"])
        exit_time = datetime.fromisoformat(exited_at)
        visit["duration_seconds"] = max(0.0, (exit_time - entered_at).total_seconds())
        state["open_page_visit"] = None


def _record_page_visit(participant_id, page_name, entered_at):
    """Start a page visit and close the previous one at this server-observed transition."""
    with rooms_lock:
        state = study_sessions.setdefault(participant_id, {
            "study_started_at": session.get("study_started_at"),
            "page_visits": [],
            "open_page_visit": None,
        })
        previous = state.get("open_page_visit")
        if previous and previous.get("exited_at") is None:
            previous["exited_at"] = entered_at
            entered_time = datetime.fromisoformat(previous["entered_at"])
            exit_time = datetime.fromisoformat(entered_at)
            previous["duration_seconds"] = max(0.0, (exit_time - entered_time).total_seconds())

        visit = {
            "sequence": len(state["page_visits"]) + 1,
            "page_name": page_name,
            "room_id": rooms.get(state.get("room_code"), {}).get("archive_id"),
            "entered_at": entered_at,
            "exited_at": None,
            "duration_seconds": None,
        }
        state["page_visits"].append(visit)
        state["open_page_visit"] = visit


def _human_verification_configured(): # Returns True if human verification is properly configured.
    return all((
        app.config["TURNSTILE_SITE_KEY"],
        app.config["TURNSTILE_SECRET_KEY"],
        app.config["TURNSTILE_HOSTNAMES"],
        app.config["FLASK_SECRET_KEY"],
        app.config["SECRET_KEY"] == app.config["FLASK_SECRET_KEY"],
    )) # Returns True if all required human verification configuration is present and valid.


def _human_verification_fingerprint():
    # No key or token is stored in the signed (readable) browser session.
    configuration = json.dumps([
        app.config["TURNSTILE_ENABLED"], app.config["TURNSTILE_SITE_KEY"],
        app.config["TURNSTILE_SECRET_KEY"], sorted(app.config["TURNSTILE_HOSTNAMES"]),
        TURNSTILE_ACTION,
    ])
    return sha256(configuration.encode("utf-8")).hexdigest()


def _human_verified():
    return (
        session.get("human_verified") is True
        and session.get("human_verification_config") == _human_verification_fingerprint()
    )


def _verification_unavailable(): # Returns a response indicating that human verification is temporarily unavailable.
    message = "Entry verification is temporarily unavailable. Please try again later."
    if request.path.startswith("/api/"):
        return jsonify(error=message, verification_url=url_for("verify_human")), 503
    return render_template("verify_human.html", error=message, site_key=None), 503


@app.before_request # Ensures that human verification is completed before processing any request.
def require_human_verification(): # Redirects to human verification if not already verified.
    if (
        not app.config["TURNSTILE_ENABLED"]
        or request.endpoint in (None, "static", "verify_human")
    ): # Skip human verification for static files, the verification page itself, or if Turnstile is disabled.
        return None
    if not _human_verification_configured(): # Returns an error page if human verification is not properly configured.
        return _verification_unavailable()
    if _human_verified(): # Skip human verification if already verified.
        return None
    if request.path.startswith("/api/"): # Return a JSON error for API requests if human verification is not completed.
        return jsonify(
            error="Please complete entry verification before continuing.",
            verification_url=url_for("verify_human"),
        ), 403
    # Never replay a blocked POST or accept a browser-supplied redirect target.
    # Redirect browser requests to the human verification page if not verified.
    return redirect(url_for("verify_human"))


@app.after_request # Ensures that the human verification page is not cached by the browser.
def prevent_verification_caching(response): # Modify response headers to prevent caching of the verification page.
    if request.endpoint == "verify_human" or (
        app.config["TURNSTILE_ENABLED"] and request.endpoint != "static"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def record_study_page_visit(response):
    # Track rendered study pages only; frequent JSON room polling is not a page transition.
    page_name = STUDY_PAGE_ENDPOINTS.get(request.endpoint)
    if (
        page_name is None
        or response.mimetype != "text/html"
        or 300 <= response.status_code < 400
        or response.status_code >= 500
    ):
        return response
    participant_id = _ensure_participant_id()
    _record_page_visit(participant_id, page_name, _utc_timestamp())
    return response


@app.route("/verify-human", methods=["GET", "POST"]) # Route for handling human verification page and form submission.
def verify_human(): # Handles the human verification logic for GET and POST requests.
    if not app.config["TURNSTILE_ENABLED"]: # Redirect to landing page if Turnstile is not enabled.
        return redirect(url_for("landing"))
    if not _human_verification_configured(): # Return error page if human verification is not properly configured.
        return _verification_unavailable()
    if _human_verified(): # Redirect to landing page if already verified.
        return redirect(url_for("landing"))

    error = None # Initialize error message for display if verification fails.
    status_code = 200
    if request.method == "POST": # Handle form submission for human verification.
        tokens = request.form.getlist("cf-turnstile-response") # Retrieve Turnstile response token from form.
        token = tokens[0] if len(tokens) == 1 else ""
        if not token.strip() or len(token) > 2048: # Validate token presence and length.
            error = "Please complete the verification below and try again."
            status_code = 400
        else: # Proceed with server-side verification of the Turnstile token.
            verification_request = Request(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=urlencode({
                    "secret": app.config["TURNSTILE_SECRET_KEY"], "response": token,
                }).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            ) # Construct the HTTP request for Turnstile verification.
            try:
                with urlopen(verification_request, timeout=TURNSTILE_TIMEOUT_SECONDS) as upstream: # Send the verification request to Cloudflare Turnstile.
                    raw = upstream.read(TURNSTILE_MAX_RESPONSE_BYTES + 1) # Read the response with a size limit.
                if len(raw) > TURNSTILE_MAX_RESPONSE_BYTES: # Check if response exceeds the maximum allowed size.
                    raise ValueError("Verification response exceeds limit")
                result = json.loads(raw) # Parse the JSON response.
                if not isinstance(result, dict) or type(result.get("success")) is not bool: # Validate the structure of the response.
                    raise ValueError("Invalid verification response")
                if result["success"] is True and ( # Further validate the response fields if verification succeeded.
                    not isinstance(result.get("action"), str)
                    or not isinstance(result.get("hostname"), str)
                ):
                    raise ValueError("Invalid verification response") # Raise error if required fields are missing or of incorrect type.
                if (
                    result["success"] is True
                    and result.get("action") == TURNSTILE_ACTION
                    and result.get("hostname") in app.config["TURNSTILE_HOSTNAMES"]
                ): # Check if verification succeeded and the action and hostname are valid.
                    session["human_verified"] = True # Mark the session as human-verified.
                    session["human_verification_config"] = _human_verification_fingerprint() # Store the human verification fingerprint in the session.
                    participant_id = _ensure_participant_id()
                    _update_study_session(participant_id, entry_verified=True)
                    return redirect(url_for("landing")) # Redirect to the landing page after successful verification.
                error = "Verification was not successful. Please complete a new check and try again." # Set error message for failed verification.
                status_code = 400 # Set HTTP status code for failed verification.
            except (URLError, OSError, HTTPException, ValueError, RecursionError):
                # Do not log exceptions: upstream messages can contain submitted secrets.
                error = "Verification could not be completed. Please try the check again."
                status_code = 503
    return render_template(
        "verify_human.html", error=error, site_key=app.config["TURNSTILE_SITE_KEY"],
    ), status_code # Return the verification page with the appropriate status code.


def consent_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("consent_accepted") is not True:
            if request.path.startswith("/api/"):
                return jsonify(error="Please provide consent before continuing."), 403
            return redirect(url_for("consent"))
        return view(*args, **kwargs)

    return wrapped


def _screening_complete():
    with rooms_lock: # Ensure thread-safe access to screening submissions; ensures screening answers were actually retrieved safely.
        submission = screening_submissions.get(session.get("screening_submission_id"))
        return (
            session.get("travel_intro_seen") is True
            and session.get("travel_screening_complete") == SCREENING_VERSION
            and submission is not None
            and submission["version"] == SCREENING_VERSION
        ) # Return True if the screening is complete, False otherwise.


def screening_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("travel_screened_out") is True:
            if request.path.startswith("/api/"):
                return jsonify(error="This study is not available after the travel screening."), 403
            return redirect(url_for("screened_out_cities"))
        if not _screening_complete():
            if request.path.startswith("/api/"):
                return jsonify(error="Please complete the travel questions before continuing."), 403
            return redirect(url_for("travel_intro"))
        return view(*args, **kwargs)

    return wrapped


def instructions_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("instructions_complete") is not True:
            if request.path.startswith("/api/"):
                return jsonify(error="Please continue from the instructions before matching."), 403
            return redirect(url_for("instructions"))
        return view(*args, **kwargs)

    return wrapped


def _get_room_participant_id(data):
    if not isinstance(data, dict):
        return None
    room_participant_id = data.get("room_participant_id")
    if not isinstance(room_participant_id, str) or not room_participant_id or len(room_participant_id) > 64:
        return None
    return room_participant_id


def _message_for_client(message):
    return {
        key: message[key]
        for key in ("id", "room_participant_id", "role", "text")
    } | {"is_claim": message.get("is_claim", False)}


def _bound_room(code, claimed_room_participant_id=None):
    """Called under rooms_lock. Browser IDs never grant membership."""
    room_participant_id = session.get("room_participant_id")
    room = rooms.get(code.upper())
    if (
        room is None
        or session.get("room_code") != code.upper()
        or room_participant_id not in room["participants"]
        or not session.get("room_token")
        or room["owners"].get(room_participant_id) != session["room_token"]
        or (claimed_room_participant_id is not None and claimed_room_participant_id != room_participant_id)
    ):
        return None, None
    return room, room_participant_id


def _chat_ready(room):
    return (
        len(room["participants"]) == 2
        and all(role is not None for role in room["participants"].values())
        and all(pid in room["ready"] for pid in room["participants"])
    )

def _dialogue_complete(room):
    """Called under rooms_lock; report completion without exposing the claim order."""
    if room["current_claim"] is not None:
        return False
    interviewee_room_participant_id = next(
        (room_participant_id for room_participant_id, role in room["participants"].items() if role == "interviewee"),
        None,
    )
    submission = screening_submissions.get(room["screening_submission_ids"].get(interviewee_room_participant_id))
    order = submission.get("order") if submission else None
    return (
        isinstance(order, list)
        and len(order) == 10
        and room["current_claim_index"] >= len(order)
    )


def _advance_transition(room, now):
    """Called under rooms_lock; advance the room once when its countdown expires."""
    transition_end = room.get("transition_end")
    if not isinstance(transition_end, (int, float)):
        return None, False

    seconds_remaining = max(0.0, transition_end - now)
    if seconds_remaining == 0 and room.get("transition_advanced") is not True:
        room["current_claim_index"] += 1
        room["current_claim"] = None
        room["next_speaker"] = None
        room["typing"].clear()
        room["transition_advanced"] = True
    return seconds_remaining, seconds_remaining == 0


def _acknowledge_transition(room, room_participant_id):
    """Called under rooms_lock; retain transition state until both participants arrive."""
    if room.get("transition_advanced") is not True:
        return
    pending = room.get("transition_pending")
    if not isinstance(pending, set):
        return
    pending.discard(room_participant_id)
    if not pending:
        room.pop("transition_end", None)
        room.pop("transition_advanced", None)
        room.pop("transition_pending", None)


@app.get("/")
def landing():
    # Create an opaque per-browser study reference so the landing visit can be timed without storing the cookie.
    _ensure_participant_id()
    return render_template("landing.html")


@app.get("/dialogue")
@consent_required
@screening_required
@instructions_required
def dialogue():
    with rooms_lock:
        room, room_participant_id = _bound_room(session.get("room_code", ""))
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        _advance_transition(room, monotonic())
        if _dialogue_complete(room):
            return redirect(url_for("feedback"))
        # If there is no current claim, generate one based on the interviewee's next city and screening answers.
        if room["current_claim"] is None:
            # Identify the interviewee and retrieve their next city and corresponding screening answer.
            interviewee_room_participant_id = next(
                (room_participant_id for room_participant_id, role in room["participants"].items() if role == "interviewee"),
                None,
            )
            # Retrieve the screening submission for the interviewee.
            submission_id = room["screening_submission_ids"].get(interviewee_room_participant_id)
            # Look up the submission in the global screening_submissions dictionary.
            submission = screening_submissions.get(submission_id)
            # Extract the order of cities from the submission, if available.
            order = submission.get("order") if submission else None
            # Reject missing or malformed saved claim orders before checking their length or indexing them.
            if not isinstance(order, list) or len(order) != 10:
                return "The dialogue claims could not be loaded. Please contact the researcher.", 409
            # Select the claim using the room's zero-based index into the saved shuffled order.
            next_city = (
                order[room["current_claim_index"]]
                if order and room["current_claim_index"] < len(order)
                else None
            )
            # Retrieve the interviewee's answer for the next city.
            answer = submission["answers"].get(next_city) if submission and next_city else None
            # Look up the human-readable city name for the next city.
            city_name = next(
                (name for city_id, name, _ in CANDIDATE_CITIES if city_id == next_city),
                None,
            )
            # If any of the required information is missing or invalid, return an error.
            if (
                interviewee_room_participant_id is None
                or answer is None
                or answer.get("visited") not in ("yes", "no")
                or city_name is None
            ):
                return "The dialogue claim could not be loaded. Please contact the researcher.", 409
            # Use one server timestamp for the visible claim and the dialogue's start boundary.
            claim_started_at = _utc_timestamp()
            # Construct the claim message based on the interviewee's answer and the city name.
            claim_message = {
                "id": len(room["messages"]) + 1,
                "room_participant_id": interviewee_room_participant_id,
                "role": "interviewee",
                "text": f"I have visited {city_name} in the past three years.",
                "is_claim": True,
                "timestamp": claim_started_at,
            }
            # Append the claim message to the room's message list.
            room["messages"].append(claim_message)
            # Keep correctness metadata server-side; client message projection omits it.
            # Add a server-side dialogue ID/index for this claim, based on the interviewee's saved shuffled order.
            room["current_claim"] = {
                "city_id": next_city,
                "claim_text": claim_message["text"],
                "truth_status": answer["visited"] == "yes",
                "dialogue_id": room["current_claim_index"]+1,
                "opening_message_id": claim_message["id"],
                "started_at": claim_started_at,
                "conversation_ended_at": None,
                "judgement_started_at": None,
                "judgement_submitted_at": None,
                "judgement_duration_seconds": None,
            }
            # Initialize the interviewer-question counter for this claim to zero. Store the count in this room's current_claim state, so it resets for each new claim.
            room["current_claim"]["interviewer_question_count"] = 0
            # After creating the opening claim, set the room's next speaker to "interviewer".
            room["next_speaker"] = "interviewer"
            # Discard any typing pings sent before the opening claim was created.
            room["typing"].clear()
            # The current claim now reflects the interviewee's latest response.
        _acknowledge_transition(room, room_participant_id)
        # Pass this participant's server-derived role into the template so interviewer-only judgement controls are never sent to the interviewee.
        return render_template("dialogue.html", role=room["participants"][room_participant_id])


@app.get("/debriefing")
@consent_required
@screening_required
@instructions_required
def debriefing():
    with rooms_lock:
        room, room_participant_id = _bound_room(session.get("room_code", ""))
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        if not _dialogue_complete(room):
            return redirect(url_for("dialogue"))
        if room_participant_id not in room.get("feedback_submissions", {}):
            return redirect(url_for("feedback"))
        _acknowledge_transition(room, room_participant_id)
        response = app.make_response(render_template("debriefing.html"))
        response.headers["Cache-Control"] = "no-store"
        return response


@app.get("/feedback")
@consent_required
@screening_required
@instructions_required
def feedback():
    with rooms_lock:
        room, room_participant_id = _bound_room(session.get("room_code", ""))
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        if not _dialogue_complete(room):
            return redirect(url_for("dialogue"))
        _acknowledge_transition(room, room_participant_id)
        # Keep the completion page final for this participant after feedback is saved.
        submitted = room_participant_id in room.get("feedback_submissions", {})
        response = app.make_response(render_template("feedback.html", submitted=submitted))
        response.headers["Cache-Control"] = "no-store"
        return response

@app.get("/study-ended")
@consent_required
@screening_required
@instructions_required
def study_ended():
    # Recheck the participant's server-owned room and completion state before showing the final destination.
    with rooms_lock:
        room, room_participant_id = _bound_room(session.get("room_code", ""))
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        if not _dialogue_complete(room):
            return redirect(url_for("dialogue"))
        if room_participant_id not in room.get("feedback_submissions", {}):
            return redirect(url_for("feedback"))
        # Disable caching so a stale final page does not linger after navigation.
        response = app.make_response(render_template(
            "study_ended.html",
            completion_url=url_for("complete_study"),
        ))
        response.headers["Cache-Control"] = "no-store" # Prevent caching of the final page
        return response


@app.get("/complete-study")
@consent_required
@screening_required
@instructions_required
def complete_study():
    # Revalidate completion on the server before recording the final redirect timestamp.
    with rooms_lock:
        room, room_participant_id = _bound_room(session.get("room_code", ""))
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        if not _dialogue_complete(room):
            return redirect(url_for("dialogue"))
        if room_participant_id not in room.get("feedback_submissions", {}):
            return redirect(url_for("feedback"))

        # Record when this app initiates the external handoff; Prolific's successful receipt is not observable here.
        redirect_at = session.get("prolific_redirect_at") or _utc_timestamp()
        participant_id = session.get("participant_id", "")
        state = study_sessions.get(participant_id)
        if state is not None:
            started_at = state.get("study_started_at")
            duration_seconds = None
            if started_at:
                duration_seconds = max(
                    0.0,
                    (datetime.fromisoformat(redirect_at) - datetime.fromisoformat(started_at)).total_seconds(),
                )
            participants_completed_at = room.setdefault("participants_completed_at", {})
            previous_redirect_at = participants_completed_at.get(room_participant_id)
            if previous_redirect_at is None:
                participants_completed_at[room_participant_id] = redirect_at
            room_completed_at = room.get("completed_at")
            if room_completed_at is None and set(participants_completed_at) >= set(room["participants"]):
                room_completed_at = redirect_at
            page_visits = [dict(visit) for visit in state.get("page_visits", [])]
            if page_visits and page_visits[-1].get("exited_at") is None:
                final_visit = page_visits[-1]
                final_visit["exited_at"] = redirect_at
                entered_at = datetime.fromisoformat(final_visit["entered_at"])
                exited_at = datetime.fromisoformat(redirect_at)
                final_visit["duration_seconds"] = max(
                    0.0, (exited_at - entered_at).total_seconds(),
                )

            if not record_prolific_handoff(
                participant_id=participant_id,
                room_id=room["archive_id"],
                redirect_at=previous_redirect_at or redirect_at,
                duration_seconds=duration_seconds,
                page_visits=page_visits,
                room_completed_at=room_completed_at,
            ):
                # Do not send the participant away when the final timestamp could not be saved; they can retry.
                return "Could not save your completion time. Please try again.", 503

            session["prolific_redirect_at"] = previous_redirect_at or redirect_at
            state["prolific_redirect_at"] = previous_redirect_at or redirect_at
            state["duration_seconds"] = duration_seconds
            if room_completed_at is not None:
                room["completed_at"] = room_completed_at
        else:
            return "Could not save your completion time. Please try again.", 503

    # The final study page has remained open until this same-origin redirect request.
    final_redirect_at = session["prolific_redirect_at"]
    _close_open_page_visit(participant_id, final_redirect_at)
    # Redirect using only the server-configured destination, never a browser-provided URL.
    return redirect(app.config["PROLIFIC_COMPLETION_URL"])


@app.post("/submit_feedback")
@consent_required
@screening_required
@instructions_required
def submit_feedback():
    # Bind this submission to the room participant in the signed session, not a posted participant ID.
    room_code = session.get("room_code", "")
    room_participant_id = session.get("room_participant_id", "")
    errors = {}
    values = {
        "task_skill": request.form.get("task_skill", ""),
        "task_difficulty": request.form.get("task_difficulty", ""),
        "task_motivation": request.form.get("task_motivation", ""),
        "strategy": request.form.get("strategy", ""),
        "additional_feedback": request.form.get("additional_feedback", ""),
    }

    with rooms_lock:
        room, room_participant_id = _bound_room(room_code, room_participant_id)
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        if not _dialogue_complete(room):
            return redirect(url_for("dialogue"))
        # Preserve the first saved form response; refreshes or repeated POSTs cannot overwrite it.
        if room_participant_id in room.get("feedback_submissions", {}):
            return redirect(url_for("debriefing"))

        # Each required rating must be exactly one value from the seven-point scale.
        for field in ("task_skill", "task_difficulty", "task_motivation"):
            submitted_values = request.form.getlist(field)
            if len(submitted_values) != 1 or submitted_values[0] not in {str(value) for value in range(1, 8)}:
                errors[field] = "Choose one rating from 1 to 7."

        # Count whitespace-separated words so repeated spaces cannot bypass the five-word limit.
        strategy_words = values["strategy"].split()
        if not strategy_words:
            errors["strategy"] = "Enter your main strategy."
        elif len(strategy_words) > 5:
            errors["strategy"] = "Use no more than five words."

        if errors:
            response = app.make_response(render_template(
                "feedback.html", submitted=False, errors=errors, values=values,
            ))
            response.status_code = 400
            response.headers["Cache-Control"] = "no-store"
            return response

        # Prepare the participant's validated feedback using a server-generated timestamp.
        feedback_record = {
            "task_skill": int(values["task_skill"]),
            "task_difficulty": int(values["task_difficulty"]),
            "task_motivation": int(values["task_motivation"]),
            "strategy": values["strategy"].strip(),
            "additional_feedback": values["additional_feedback"],
            "timestamp": _utc_timestamp(),
        }
        # Commit the whole server-owned participant/room snapshot before acknowledging this submission.
        room.setdefault("feedback_submissions", {})[room_participant_id] = feedback_record
        if not archive_completed_room(
            room_code=room_code,
            room_state=room,
            study_sessions=study_sessions,
            screening_submissions=screening_submissions,
            feedback_room_participant_id=room_participant_id,
            feedback_record=feedback_record,
        ):
            # Keep the response retryable if persistence failed; do not claim that feedback was saved.
            room["feedback_submissions"].pop(room_participant_id, None)
            response = app.make_response(render_template(
                "feedback.html",
                submitted=False,
                errors={"database": "We could not securely save your feedback. Please try again."},
                values=values,
            ))
            response.status_code = 503
            response.headers["Cache-Control"] = "no-store"
            return response
    # Use Post/Redirect/Get so refreshing the confirmation page does not resubmit the form.
    return redirect(url_for("debriefing"))


@app.get("/room")
@consent_required
@screening_required
@instructions_required
def room():
    return render_template("room.html")


@app.get("/consent")
def consent():
    # Ensure direct consent links still receive a pseudonymous study ID for later page timing.
    _ensure_participant_id()
    if session.get("consent_accepted") is True:
        if _screening_complete() and session.get("travel_screened_out") is not True:
            return redirect(url_for("instructions"))
        return redirect(url_for("travel_intro"))
    return render_template("consent.html")


@app.get("/start")
def start_study():
    # Preserve the first server timestamp for the participant's Start-the-study click.
    participant_id = _ensure_participant_id(started=True)
    _close_open_page_visit(participant_id, session["study_started_at"])
    return redirect(url_for("consent")) # redirect to consent page


@app.post("/consent") # Handle consent decision (accept or deny)
def consent_decision():
    decision = request.form.get("decision")
    if decision not in ("accept", "deny"):
        return "Invalid consent choice.", 400
    # Drop legacy city selections; current progress and screen-out survive return visits.
    session.pop("other_city_ids", None)
    session.pop("other_cities_complete", None)
    if decision == "accept":
        # Ensure a direct consent POST still has a server-timed study start.
        participant_id = _ensure_participant_id(started=True)
        session["consent_accepted"] = True
        session.setdefault("consent_accepted_at", _utc_timestamp())
        _update_study_session(
            participant_id,
            consent_accepted=True,
            consent_accepted_at=session["consent_accepted_at"],
        )
        if _screening_complete() and session.get("travel_screened_out") is not True:
            return redirect(url_for("instructions"))
        return redirect(url_for("travel_intro"))
    session.pop("consent_accepted", None)
    session.pop("consent_accepted_at", None)
    _update_study_session(
        session.get("participant_id"),
        consent_accepted=False,
        consent_accepted_at=None,
    )
    return render_template("no_consent.html")


@app.get("/travel-intro")
@consent_required
def travel_intro():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if _screening_complete():
        return redirect(url_for("instructions"))
    return render_template("travel_intro.html")


@app.post("/travel-intro")
@consent_required
def begin_travel_screening():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if _screening_complete():
        return redirect(url_for("instructions"))
    session["travel_intro_seen"] = True
    _update_study_session(session.get("participant_id"), travel_intro_seen=True)
    return redirect(url_for("travel_cities"))


@app.route("/travel-cities", methods=["GET", "POST"])
@consent_required
def travel_cities():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if session.get("travel_intro_seen") is not True:
        return redirect(url_for("travel_intro"))
    if _screening_complete():
        return redirect(url_for("instructions"))

    values = {}
    errors = {}
    if request.method == "POST":
        for city_id, _, _ in CANDIDATE_CITIES:
            for field, allowed in (("visited", {"yes", "no"}), ("familiarity", {str(n) for n in range(1, 8)})):
                key = f"{field}_{city_id}"
                values[key] = request.form.get(key, "")
                if len(request.form.getlist(key)) != 1 or values[key] not in allowed:
                    errors[key] = "Please choose Yes or No." if field == "visited" else "Please choose a rating from 1 to 7."
        # Validate all 40 fields before making any eligibility decision.
        if not errors:
            yes_count = sum(values[f"visited_{city_id}"] == "yes" for city_id, _, _ in CANDIDATE_CITIES)
            session.pop("other_city_ids", None)
            session.pop("other_cities_complete", None)
            session.pop("instructions_complete", None)
            if yes_count >= 5 and len(CANDIDATE_CITIES) - yes_count >= 5:
                with rooms_lock:
                    # Only server-generated IDs select records. Ignore extra form
                    # keys, including IDs, timestamps, versions and truth fields.
                    submission_id = token_hex(32)
                    answers = {
                        city_id: {
                            "visited": values[f"visited_{city_id}"],
                            "familiarity": int(values[f"familiarity_{city_id}"]),
                        }
                        for city_id, _, _ in CANDIDATE_CITIES
                    }
                    visited_city_ids = [
                        city_id for city_id, answer in answers.items()
                        if answer["visited"] == "yes"
                    ]
                    not_visited_city_ids = [
                        city_id for city_id, answer in answers.items()
                        if answer["visited"] == "no"
                    ]
                    sampled_visited = sample(visited_city_ids, 5)
                    sampled_not_visited = sample(not_visited_city_ids, 5)
                    combined_sampled = sampled_visited + sampled_not_visited
                    shuffle(combined_sampled)
                    screening_submissions[submission_id] = {
                        "version": SCREENING_VERSION,
                        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "answers": answers,
                        "order": combined_sampled,
                    }
                    # Re-screening replaces only this owner's room reference;
                    # no participant's retained record is deleted or overwritten.
                    bound_room, room_participant_id = _bound_room(session.get("room_code", ""))
                    if bound_room is not None:
                        bound_room["screening_submission_ids"][room_participant_id] = submission_id
                    session["screening_submission_id"] = submission_id
                session["travel_screening_complete"] = SCREENING_VERSION
                _update_study_session(
                    session.get("participant_id"),
                    screening_complete_version=SCREENING_VERSION,
                    travel_screened_out=False,
                )
                return redirect(url_for("instructions"))
            session.pop("travel_screening_complete", None)
            session["travel_screened_out"] = True
            _update_study_session(
                session.get("participant_id"),
                screening_complete_version=None,
                travel_screened_out=True,
            )
            return redirect(url_for("screened_out_cities"))

    return render_template("travel_cities.html", cities=CANDIDATE_CITIES, values=values, errors=errors), 400 if errors else 200


@app.get("/instructions")
@consent_required
@screening_required
def instructions():
    if session.get("instructions_complete") is True:
        return redirect(url_for("room"))
    return render_template("instructions.html")


@app.post("/instructions")
@consent_required
@screening_required
def continue_to_matching():
    session["instructions_complete"] = True
    _update_study_session(session.get("participant_id"), instructions_complete=True)
    return redirect(url_for("room"))


@app.route("/role-instructions", methods=["GET", "POST"])
@consent_required
@screening_required
@instructions_required
def role_instructions():
    with rooms_lock:
        claimed_room_participant_id = request.values.get("room_participant_id")
        data = request.get_json(silent=True)
        if isinstance(data, dict) and "room_participant_id" in data:
            claimed_room_participant_id = data["room_participant_id"]
        room, room_participant_id = _bound_room(session.get("room_code", ""), claimed_room_participant_id)
        if claimed_room_participant_id is not None and claimed_room_participant_id != session.get("room_participant_id"):
            return "This participant is not in the room.", 403
        if room is None or room["participants"][room_participant_id] is None:
            return redirect(url_for("room"))
        if request.method == "POST":
            room["ready"].add(room_participant_id)
            return redirect(url_for("role_instructions"))
        if _chat_ready(room):
            # Start the room timeline when both participants are ready to enter the shared chat.
            room.setdefault("chat_started_at", _utc_timestamp())
            return redirect(url_for("dialogue"))
        response = app.make_response(render_template(
            "role_instructions.html",
            role=room["participants"][room_participant_id],
            ready=room_participant_id in room["ready"],
            code=session["room_code"],
        ))
        response.headers["Cache-Control"] = "no-store"
        return response


@app.get("/screened-out-cities")
@consent_required
def screened_out_cities():
    if session.get("travel_screened_out") is not True:
        return redirect(url_for("travel_intro"))
    return render_template("screened_out.html")


@app.post("/api/rooms")
@consent_required
@screening_required
@instructions_required
def match_room(): # Handles the API request to match a participant to a room
    data = request.get_json(silent=True) or {}
    room_participant_id = _get_room_participant_id(data)
    if room_participant_id is None:
        return jsonify(error="A room participant ID is required."), 400

    with rooms_lock:
        # Returning clients keep their place, even when every room is occupied. This ensures that participants who have already been assigned a room do not lose their spot.
        for code, room in rooms.items():
            if room_participant_id in room["participants"]:
                bound_room, _ = _bound_room(code, room_participant_id)
                if bound_room is None:
                    return jsonify(error="This room participant ID is already in use."), 403
                role = room["participants"][room_participant_id]
                if role is None:
                    return jsonify(code=code, status="waiting")
                return jsonify(code=code, status="paired", role=role)

        if session.get("room_participant_id") not in (None, room_participant_id):
            return jsonify(error="This session is bound to another room participant."), 403

        # Fill a waiting room before opening the next unused code.
        code = next((code for code in ROOM_CODES if code in rooms and len(rooms[code]["participants"]) == 1), None)
        if code is None:
            code = next((code for code in ROOM_CODES if code not in rooms), None)
            if code is None:
                return jsonify(error="All rooms are currently occupied."), 409
            token = token_hex(32)
            # Add a room field for the next allowed speaker; initialize it to None until the claim exists.
            participant_id = session.get("participant_id", "")
            rooms[code] = {"archive_id": token_hex(16), "created_at": _utc_timestamp(), "participants": {room_participant_id: None}, "messages": [], "dialogues": [], "raw_events": [], "typing": {}, "ready": set(), "owners": {room_participant_id: token}, "participant_ids": {room_participant_id: participant_id}, "current_claim": None, "current_claim_index": 0, "next_speaker": None}
            rooms[code]["screening_submission_ids"] = {room_participant_id: session["screening_submission_id"]} # Store the screening submission ID for this room participant.
            session.update(room_code=code, room_participant_id=room_participant_id, room_token=token)
            study_state = study_sessions.get(participant_id)
            if study_state is not None:
                study_state.update(room_code=code, room_participant_id=room_participant_id)
                if study_state.get("open_page_visit"):
                    study_state["open_page_visit"]["room_id"] = rooms[code]["archive_id"]
            return jsonify(code=code, status="waiting")

        token = token_hex(32)
        participant_id = session.get("participant_id", "")
        rooms[code]["owners"][room_participant_id] = token
        rooms[code].setdefault("participant_ids", {})[room_participant_id] = participant_id
        rooms[code]["screening_submission_ids"][room_participant_id] = session["screening_submission_id"] # Store the screening submission ID for this room participant.
        session.update(room_code=code, room_participant_id=room_participant_id, room_token=token)
        participants = rooms[code]["participants"] # Get the dictionary of participants for the current room
        participants[room_participant_id] = None # Initialize the room membership role until assigned.
        roles = ["interviewer", "interviewee"] # List of possible roles for participants in the room
        if choice((True, False)):
            roles.reverse()
        participants.update(zip(participants, roles)) # Assign roles to participants in the room, pairing them with the available roles
        rooms[code]["paired_at"] = _utc_timestamp()
        for paired_room_participant_id in participants:
            study_state = study_sessions.get(
                rooms[code]["participant_ids"].get(paired_room_participant_id, ""),
            )
            if study_state is not None:
                study_state.update(room_code=code, room_participant_id=paired_room_participant_id)
                if study_state.get("open_page_visit"):
                    study_state["open_page_visit"]["room_id"] = rooms[code]["archive_id"]
        # Find the participant who was assigned the interviewer role.
        interviewer_room_participant_id = next(
            room_participant_id for room_participant_id, role in participants.items() if role == "interviewer"
        )
        # Independently choose feedback or control with equal probability.
        assigned_condition = choice(("feedback", "control"))
        # Keep the condition in server-side room state, keyed by the interviewer ID.
        rooms[code]["conditions"] = {interviewer_room_participant_id: assigned_condition}
        # Buffer the authoritative role and condition assignment; it is batch-written at feedback with the rest of the study.
        rooms[code]["raw_events"].append({
            "event_id": token_hex(16),
            "room_id": rooms[code]["archive_id"],
            "room_code": code,
            "event_type": "room_paired",
            "occurred_at": rooms[code]["paired_at"],
            "event_data": {
                "participants": [
                    {
                        "participant_id": rooms[code]["participant_ids"][room_participant_id],
                        "role": role,
                    }
                    for room_participant_id, role in participants.items()
                ],
                "condition": assigned_condition,
            },
        })
        return jsonify(code=code, status="paired", role=participants[room_participant_id])


@app.get("/api/rooms/<code>") # Retrieves the status of a specific room for a participant
@consent_required
@screening_required
@instructions_required
def room_status(code):
    claimed_room_participant_id = request.args.get("room_participant_id")
    with rooms_lock: # Ensures thread-safe access to the rooms dictionary
        room, room_participant_id = _bound_room(code, claimed_room_participant_id)
        if room is None:
            return jsonify(error="This session is not a participant in the room."), 403

        participants = room["participants"]
        if room_participant_id not in participants:
            return jsonify(error="This participant is not in the room."), 403

        role = participants[room_participant_id]
        chat_ready = _chat_ready(room)
    if role is None: # If the participant has not been assigned a role yet, they are still waiting
        return jsonify(code=code.upper(), status="waiting", chat_ready=False)
    return jsonify(code=code.upper(), status="paired", role=role, chat_ready=chat_ready)


@app.post("/api/rooms/<code>/messages")
@consent_required
@screening_required
@instructions_required
def send_room_message(code): # Handles sending a message to a specific room by a participant
    data = request.get_json(silent=True) or {} # Get the JSON data from the request body, or use an empty dictionary if none is provided
    room_participant_id = _get_room_participant_id(data) # Extract the room membership ID from the request data.
    text = data.get("text") if isinstance(data, dict) else None
    if room_participant_id is None:
        return jsonify(error="A room participant ID is required."), 400
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="A non-empty message is required."), 400 # Return an error if the message text is empty or not a string
    text = text.strip() # Trim leading and trailing whitespace from the message text
    if len(text) > 2000:
        return jsonify(error="Messages must be 2000 characters or fewer."), 400 # Return an error if the message exceeds the character limit    

    with rooms_lock:
        room, room_participant_id = _bound_room(code, room_participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        # Ensure that the opening claim has been made before allowing messages to be sent
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before messages can be sent."), 403
        role = room["participants"][room_participant_id]
        # Before accepting an interviewer message, reject it if this claim already has 10 questions
        if role == "interviewer" and room["current_claim"]["interviewer_question_count"] >= 10:
            return jsonify(error="This claim has already received 10 questions from the interviewer."), 403
        # Reject this request unless role matches the room's next allowed speaker.
        if role != room["next_speaker"]:
            return jsonify(error="It is not your turn to speak."), 403
        question_type = data.get("question_type")
        if role == "interviewer":
            if question_type is not None and question_type not in ["open-ended", "closed-ended", "follow-up", "clarification", "probing"]:
                return jsonify(error="A valid question type is required for interviewer messages."), 400
        else:
            if question_type is not None:
                return jsonify(error="Interviewee messages cannot have a question type."), 400
        message = {
            "id": len(room["messages"]) + 1,
            "room_participant_id": room_participant_id,
            "role": role,
            "text": text,
            "timestamp": _utc_timestamp(),
        }
        if role == "interviewer":
            message["question_type"] = question_type
            if question_type is None:
                # Keep the interviewee from answering until this message has a server-validated category.
                room["current_claim"]["pending_question_type_message_id"] = message["id"]
        room["messages"].append(message)
        # If the accepted message is from the interviewer, increment this claim's question count.
        if role == "interviewer":
            room["current_claim"]["interviewer_question_count"] += 1
        # When question 10 is asked, still allow the interviewee's tenth answer; after that answer, end question turns for this claim.
        if room["current_claim"]["interviewer_question_count"] >= 10 and role == "interviewee":
            room["current_claim"]["question_turns_ended"] = True
            # End conversation timing at the server's acceptance of the tenth answer.
            room["current_claim"]["conversation_ended_at"] = message["timestamp"]
            dialogue_started_at = room["current_claim"].get("started_at")
            if dialogue_started_at:
                room["current_claim"]["dialogue_duration_seconds"] = max(
                    0.0,
                    (datetime.fromisoformat(message["timestamp"]) - datetime.fromisoformat(dialogue_started_at)).total_seconds(),
                )
            # Judgement timing starts at the same boundary and runs through any feedback shown before Continue.
            room["current_claim"]["judgement_started_at"] = message["timestamp"]
        if room["current_claim"].get("question_turns_ended"):
            room["next_speaker"] = None
        elif room["current_claim"].get("pending_question_type_message_id") is not None:
            # Pause turn-taking while the interviewer classifies the question they just sent.
            room["next_speaker"] = None
        else:
            # Otherwise, switch the next allowed speaker to the other role.
            room["next_speaker"] = "interviewer" if role == "interviewee" else "interviewee"

    return jsonify(message=_message_for_client(message)), 201 # Return the newly created message without its private timestamp


@app.post("/api/rooms/<code>/question-type")
@consent_required
@screening_required
@instructions_required
def set_question_type(code):
    # Accept a category only for the interviewer’s pending message in the active dialogue.
    data = request.get_json(silent=True) or {}
    room_participant_id = _get_room_participant_id(data)
    question_type = data.get("question_type") if isinstance(data, dict) else None
    allowed_question_types = ["open-ended", "closed-ended", "follow-up", "clarification", "probing"]
    if room_participant_id is None:
        return jsonify(error="A room participant ID is required."), 400
    if question_type not in allowed_question_types:
        return jsonify(error="A valid question type is required."), 400

    with rooms_lock:
        # Bind the request to its assigned room and reject stale or unrelated participants.
        room, room_participant_id = _bound_room(code, room_participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        if room["current_claim"] is None:
            return jsonify(error="There is no active question to categorize."), 403
        if room["participants"].get(room_participant_id) != "interviewer":
            return jsonify(error="Only the interviewer can categorize a question."), 403

        # Require the one pending message rather than allowing a category to be changed later.
        pending_message_id = room["current_claim"].get("pending_question_type_message_id")
        if pending_message_id is None:
            return jsonify(error="There is no question waiting for a category."), 403
        message = next(
            (item for item in room["messages"] if item["id"] == pending_message_id),
            None,
        )
        if message is None or message["room_participant_id"] != room_participant_id:
            return jsonify(error="The pending question could not be found."), 409

        # Save private metadata, clear the gate, and grant the interviewee the next turn atomically.
        message["question_type"] = question_type
        del room["current_claim"]["pending_question_type_message_id"]
        room["next_speaker"] = "interviewee"

    return jsonify(ok=True), 200


# POST endpoint for the interviewer to submit a true/false judgement and 0–100 confidence after all ten answers; derive the role and active dialogue ID from the bound room, then store the judgement on its current_claim.
@app.post("/api/rooms/<code>/judgement") # Endpoint for the interviewer to submit a true/false judgement and confidence after all ten answers
@consent_required
@screening_required
@instructions_required
def submit_judgement(code):
    data = request.get_json(silent=True) or {}
    room_participant_id = _get_room_participant_id(data)
    if room_participant_id is None:
        return jsonify(error="A room participant ID is required."), 400

    with rooms_lock:
        room, room_participant_id = _bound_room(code, room_participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before submitting a judgement."), 403
        if room["participants"][room_participant_id] != "interviewer":
            return jsonify(error="Only the interviewer can submit a judgement."), 403
        if room["current_claim"].get("question_turns_ended") is not True:
            return jsonify(error="All ten questions must be asked before submitting a judgement."), 403

        judgement = data.get("judgement")
        confidence = data.get("confidence")
        if judgement not in ["true", "false"]:
            return jsonify(error="Judgement must be true or false."), 400
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0 <= confidence <= 100):
            return jsonify(error="Confidence must be a number between 0 and 100."), 400

        judgement_submitted_at = _utc_timestamp()
        truth_status = room["current_claim"].get("truth_status")
        correct = ((judgement == "true") == truth_status)
        room["current_claim"]["judgement"] = {
            "judgement": judgement,
            "confidence": confidence,
            "correct": correct,
            "timestamp": judgement_submitted_at,
        }
        room["current_claim"]["judgement_submitted_at"] = judgement_submitted_at
        # only when this room's assigned interviewer is in the feedback condition; never expose it to control.
        # Determine if the current interviewer is in the feedback condition
        interviewer_room_participant_id = next(
            room_participant_id for room_participant_id, role in room["participants"].items() if role == "interviewer"
        )
        condition = room.get("conditions", {}).get(interviewer_room_participant_id)
        if condition == "feedback":
            return jsonify(status="ok", correct=correct), 201
        else:
            return jsonify(status="ok"), 201
        # truth_status = room["current_claim"].get("truth_status")
        # correct = ((judgement == "true") == truth_status)
    # This line is now handled within the feedback condition check above
    

@app.post("/api/rooms/<code>/continue") # Advances the claim index after a judgement; only the bound interviewer can perform this action
@consent_required
@screening_required
@instructions_required
def continue_claim(code):
    data = request.get_json(silent=True) or {}
    room_participant_id = _get_room_participant_id(data)
    if room_participant_id is None:
        return jsonify(error="A room participant ID is required."), 400

    with rooms_lock:
        room, room_participant_id = _bound_room(code, room_participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before continuing."), 403
        if room["participants"][room_participant_id] != "interviewer":
            return jsonify(error="Only the interviewer can continue to the next claim."), 403
        if room["current_claim"].get("judgement") is None:
            return jsonify(error="A judgement must be submitted before continuing."), 403
        if room.get("transition_end") is not None:
            return jsonify(error="The next dialogue transition is already in progress."), 409

        # End judgement timing on the interviewer's Continue click, including any feedback wait.
        judgement_ended_at = _utc_timestamp()
        judgement_started_at = room["current_claim"].get("judgement_started_at")
        if judgement_started_at:
            room["current_claim"]["judgement_ended_at"] = judgement_ended_at
            room["current_claim"]["judgement_duration_seconds"] = max(
                0.0,
                (datetime.fromisoformat(judgement_ended_at) - datetime.fromisoformat(judgement_started_at)).total_seconds(),
            )

        # Preserve the completed dialogue in the room snapshot before transition advancement clears current_claim.
        current_claim = room["current_claim"]
        judgement_record = current_claim["judgement"]
        conversation_ended_at = current_claim.get("conversation_ended_at")
        conversation_started_at = current_claim.get("started_at")
        room.setdefault("dialogues", []).append({
            "dialogue_number": current_claim["dialogue_id"],
            "city_id": current_claim["city_id"],
            "claim_text": current_claim.get("claim_text", ""),
            "truth_status": current_claim["truth_status"],
            "started_at": conversation_started_at,
            "conversation_ended_at": conversation_ended_at,
            "dialogue_duration_seconds": current_claim.get("dialogue_duration_seconds", 0.0),
            "judgement": judgement_record["judgement"],
            "confidence": judgement_record["confidence"],
            "correct": judgement_record["correct"],
            "judgement_submitted_at": current_claim.get("judgement_submitted_at"),
            "judgement_ended_at": current_claim.get("judgement_ended_at"),
            "judgement_duration_seconds": current_claim.get("judgement_duration_seconds", 0.0),
        })

        # Start a server-timed 3-second transition on this valid Continue click; keep the current claim until the countdown expires. We need to store the transition deadline in the room.
        transition_start = monotonic() # Record the start time of the transition
        transition_end = transition_start + 3.0
        room["transition_end"] = transition_end # Store the transition deadline in the room
        room["transition_advanced"] = False
        room["transition_pending"] = set(room["participants"])
    return jsonify(status="ok"), 201

TYPING_WINDOW_SECONDS = 3.0


@app.post("/api/rooms/<code>/typing") # Sets the typing status for a participant in a specific room; used to indicate that the participant is currently typing
@consent_required
@screening_required
@instructions_required
def set_typing(code):
    data = request.get_json(silent=True) or {} # Parse the JSON body of the request, or use an empty dictionary if parsing fails
    room_participant_id = _get_room_participant_id(data)
    if room_participant_id is None:
        return jsonify(error="A room participant ID is required."), 400

    with rooms_lock:
        room, room_participant_id = _bound_room(code, room_participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before typing."), 403
        # Reject typing pings unless this participant's role is room["next_speaker"].
        if room["next_speaker"] != room["participants"][room_participant_id]:
            return jsonify(error="It's not your turn to type."), 403

        room["typing"][room_participant_id] = monotonic() # Update this room participant's typing time.
    return jsonify(status="ok") # Return a success status after setting the typing indicator


@app.get("/api/rooms/<code>/messages") # Retrieves messages from a specific room for a participant and indicates if their partner is currently typing
@consent_required
@screening_required
@instructions_required
def get_room_messages(code):
    room_participant_id = request.args.get("room_participant_id", "")
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        return jsonify(error="after_id must be a non-negative integer."), 400

    with rooms_lock:
        room, room_participant_id = _bound_room(code, room_participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403

        messages = [
            _message_for_client(message)
            for message in room["messages"]
            if message["id"] > after_id
            and message["id"] >= (
                room["current_claim"]["opening_message_id"]
                if room["current_claim"] is not None
                else len(room["messages"]) + 1
            )
        ]
        now = monotonic()
        seconds_remaining, transition_expired = _advance_transition(room, now)
        partner_typing = any(
            partner_id != room_participant_id and now - last < TYPING_WINDOW_SECONDS
            for partner_id, last in room["typing"].items()
        )
        current_claim = room["current_claim"]
        question_count = current_claim["interviewer_question_count"] if current_claim else 0
        question_turns_ended = bool(current_claim and current_claim.get("question_turns_ended"))
        question_type_pending = bool(
            current_claim and current_claim.get("pending_question_type_message_id") is not None
        )
        if question_turns_ended:
            question_number = 10
        elif question_type_pending:
            # Keep both participants on the question that was sent but is not yet answerable.
            question_number = max(question_count, 1)
        elif room["next_speaker"] == "interviewee":
            question_number = max(question_count, 1)
        else:
            question_number = min(question_count + 1, 10)
        # A participant keeps receiving the completion signal until its page request acknowledges the transition.
        pending = room.get("transition_pending", set())
        transition_complete = transition_expired and room_participant_id in pending
        if room_participant_id not in pending:
            seconds_remaining = None
        # Report only the completion boolean, never the private shuffled claim order.
        dialogue_complete = _dialogue_complete(room)
        dialogue_number = current_claim["dialogue_id"] if current_claim else None
        # This response is bound to the requester; it exposes no city answer or claim truth.
        can_send = room["next_speaker"] == room["participants"][room_participant_id]
        # Only the interviewer needs to know why the turn is paused; the category itself stays private.
        interviewer_question_type_pending = (
            question_type_pending and room["participants"][room_participant_id] == "interviewer"
        )

    response_data = dict(
        messages=messages,
        partner_typing=partner_typing,
        can_send=can_send,
        question_number=question_number,
        question_turns_ended=question_turns_ended,
        dialogue_complete=dialogue_complete,
        seconds_remaining=seconds_remaining,
        transition_complete=transition_complete,
        dialogue_number=dialogue_number,
    )
    if interviewer_question_type_pending:
        # Keep category-workflow state out of the interviewee's response.
        response_data["question_type_pending"] = True
    return jsonify(response_data)
