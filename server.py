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
)
# A generated development key must never count as a configured verification key.
app.config["SECRET_KEY"] = app.config["FLASK_SECRET_KEY"] or (
    None if app.config["TURNSTILE_ENABLED"] else token_hex(32)
)
rooms = {}
rooms_lock = Lock()
# Private eligible answers and room associations share rooms_lock. Never serialize
# these records into a browser session, template, or API response.
screening_submissions = {}
ROOM_CODES = tuple(f"ROOM{number:02d}" for number in range(1, 10001))
SCREENING_VERSION = "english-cities-v1"
TURNSTILE_ACTION = "study-entry"
TURNSTILE_TIMEOUT_SECONDS = 5
TURNSTILE_MAX_RESPONSE_BYTES = 16384


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


def _get_participant_id(data):
    if not isinstance(data, dict):
        return None
    participant_id = data.get("participant_id")
    if not isinstance(participant_id, str) or not participant_id or len(participant_id) > 64:
        return None
    return participant_id


def _message_for_client(message):
    return {key: message[key] for key in ("id", "participant_id", "role", "text")}


def _bound_room(code, claimed_id=None):
    """Called under rooms_lock. Browser IDs never grant membership."""
    participant_id = session.get("room_participant_id")
    room = rooms.get(code.upper())
    if (
        room is None
        or session.get("room_code") != code.upper()
        or participant_id not in room["participants"]
        or not session.get("room_token")
        or room["owners"].get(participant_id) != session["room_token"]
        or (claimed_id is not None and claimed_id != participant_id)
    ):
        return None, None
    return room, participant_id


def _chat_ready(room):
    return (
        len(room["participants"]) == 2
        and all(role is not None for role in room["participants"].values())
        and all(pid in room["ready"] for pid in room["participants"])
    )


@app.get("/")
def landing():
    return render_template("landing.html")


@app.get("/dialogue")
@consent_required
@screening_required
@instructions_required
def dialogue():
    with rooms_lock:
        room, participant_id = _bound_room(session.get("room_code", ""))
        if room is None or room["participants"][participant_id] is None:
            return redirect(url_for("room"))
        if not _chat_ready(room):
            return redirect(url_for("role_instructions"))
        # If there is no current claim, generate one based on the interviewee's next city and screening answers.
        if room["current_claim"] is None:
            # Identify the interviewee and retrieve their next city and corresponding screening answer.
            interviewee_id = next(
                (pid for pid, role in room["participants"].items() if role == "interviewee"),
                None,
            )
            # Retrieve the screening submission for the interviewee.
            submission_id = room["screening_submission_ids"].get(interviewee_id)
            # Look up the submission in the global screening_submissions dictionary.
            submission = screening_submissions.get(submission_id)
            # Extract the order of cities from the submission, if available.
            order = submission.get("order") if submission else None
            # Select the claim using room["current_claim_index"] instead of always using order[0].
            # Handle the index reaching the end of the shuffled order before trying to select another claim.
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
                interviewee_id is None
                or answer is None
                or answer.get("visited") not in ("yes", "no")
                or city_name is None
            ):
                return "The dialogue claim could not be loaded. Please contact the researcher.", 409
            # Construct the claim message based on the interviewee's answer and the city name.
            claim_message = {
                "id": len(room["messages"]) + 1,
                "participant_id": interviewee_id,
                "role": "interviewee",
                "text": f"I have visited {city_name} in the past three years.",
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            }
            # Append the claim message to the room's message list.
            room["messages"].append(claim_message)
            # Keep correctness metadata server-side; client message projection omits it.
            # Add a server-side dialogue ID/index for this claim, based on the interviewee's saved shuffled order.
            room["current_claim"] = {
                "city_id": next_city,
                "truth_status": answer["visited"] == "yes",
                "dialogue_id": room["current_claim_index"]+1,
            }
            # Initialize the interviewer-question counter for this claim to zero. Store the count in this room's current_claim state, so it resets for each new claim.
            room["current_claim"]["interviewer_question_count"] = 0
            # After creating the opening claim, set the room's next speaker to "interviewer".
            room["next_speaker"] = "interviewer"
            # Discard any typing pings sent before the opening claim was created.
            room["typing"].clear()
            # The current claim now reflects the interviewee's latest response.
        # Pass this participant's server-derived role into the template so interviewer-only judgement controls are never sent to the interviewee.
        return render_template("dialogue.html", role=room["participants"][participant_id])


@app.get("/room")
@consent_required
@screening_required
@instructions_required
def room():
    return render_template("room.html")


@app.get("/consent")
def consent():
    if session.get("consent_accepted") is True:
        if _screening_complete() and session.get("travel_screened_out") is not True:
            return redirect(url_for("instructions"))
        return redirect(url_for("travel_intro"))
    return render_template("consent.html")


@app.get("/start")
def start_study():
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
        session["consent_accepted"] = True
        if _screening_complete() and session.get("travel_screened_out") is not True:
            return redirect(url_for("instructions"))
        return redirect(url_for("travel_intro"))
    session.pop("consent_accepted", None)
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
                    bound_room, participant_id = _bound_room(session.get("room_code", ""))
                    if bound_room is not None:
                        bound_room["screening_submission_ids"][participant_id] = submission_id
                    session["screening_submission_id"] = submission_id
                session["travel_screening_complete"] = SCREENING_VERSION
                return redirect(url_for("instructions"))
            session.pop("travel_screening_complete", None)
            session["travel_screened_out"] = True
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
    return redirect(url_for("room"))


@app.route("/role-instructions", methods=["GET", "POST"])
@consent_required
@screening_required
@instructions_required
def role_instructions():
    with rooms_lock:
        claimed_id = request.values.get("participant_id")
        data = request.get_json(silent=True)
        if isinstance(data, dict) and "participant_id" in data:
            claimed_id = data["participant_id"]
        room, participant_id = _bound_room(session.get("room_code", ""), claimed_id)
        if claimed_id is not None and claimed_id != session.get("room_participant_id"):
            return "This participant is not in the room.", 403
        if room is None or room["participants"][participant_id] is None:
            return redirect(url_for("room"))
        if request.method == "POST":
            room["ready"].add(participant_id)
            return redirect(url_for("role_instructions"))
        if _chat_ready(room):
            return redirect(url_for("dialogue"))
        response = app.make_response(render_template(
            "role_instructions.html",
            role=room["participants"][participant_id],
            ready=participant_id in room["ready"],
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
    participant_id = _get_participant_id(data)
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400

    with rooms_lock:
        # Returning clients keep their place, even when every room is occupied. This ensures that participants who have already been assigned a room do not lose their spot.
        for code, room in rooms.items():
            if participant_id in room["participants"]:
                bound_room, _ = _bound_room(code, participant_id)
                if bound_room is None:
                    return jsonify(error="This participant ID is already in use."), 403
                role = room["participants"][participant_id]
                if role is None:
                    return jsonify(code=code, status="waiting")
                return jsonify(code=code, status="paired", role=role)

        if session.get("room_participant_id") not in (None, participant_id):
            return jsonify(error="This session is bound to another participant."), 403

        # Fill a waiting room before opening the next unused code.
        code = next((code for code in ROOM_CODES if code in rooms and len(rooms[code]["participants"]) == 1), None)
        if code is None:
            code = next((code for code in ROOM_CODES if code not in rooms), None)
            if code is None:
                return jsonify(error="All rooms are currently occupied."), 409
            token = token_hex(32)
            # Add a room field for the next allowed speaker; initialize it to None until the claim exists.
            rooms[code] = {"participants": {participant_id: None}, "messages": [], "typing": {}, "ready": set(), "owners": {participant_id: token}, "current_claim": None, "current_claim_index": 0, "next_speaker": None}
            rooms[code]["screening_submission_ids"] = {participant_id: session["screening_submission_id"]} # Store the screening submission ID for the participant in the room; for creating a new room.
            session.update(room_code=code, room_participant_id=participant_id, room_token=token)
            return jsonify(code=code, status="waiting")

        token = token_hex(32)
        rooms[code]["owners"][participant_id] = token
        rooms[code]["screening_submission_ids"][participant_id] = session["screening_submission_id"] # Store the screening submission ID for the participant in the room; if room exists, update it with the new participant's ID.
        session.update(room_code=code, room_participant_id=participant_id, room_token=token)
        participants = rooms[code]["participants"]
        participants[participant_id] = None
        roles = ["interviewer", "interviewee"]
        if choice((True, False)):
            roles.reverse()
        participants.update(zip(participants, roles))
        return jsonify(code=code, status="paired", role=participants[participant_id])


@app.get("/api/rooms/<code>") # Retrieves the status of a specific room for a participant
@consent_required
@screening_required
@instructions_required
def room_status(code):
    claimed_id = request.args.get("participant_id")
    with rooms_lock: # Ensures thread-safe access to the rooms dictionary
        room, participant_id = _bound_room(code, claimed_id)
        if room is None:
            return jsonify(error="This session is not a participant in the room."), 403

        participants = room["participants"]
        if participant_id not in participants:
            return jsonify(error="This participant is not in the room."), 403

        role = participants[participant_id]
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
    participant_id = _get_participant_id(data) # Extract the participant ID from the request data
    text = data.get("text") if isinstance(data, dict) else None
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400 # Return an error if no participant ID is provided
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="A non-empty message is required."), 400 # Return an error if the message text is empty or not a string
    text = text.strip() # Trim leading and trailing whitespace from the message text
    if len(text) > 2000:
        return jsonify(error="Messages must be 2000 characters or fewer."), 400 # Return an error if the message exceeds the character limit    

    with rooms_lock:
        room, participant_id = _bound_room(code, participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        # Ensure that the opening claim has been made before allowing messages to be sent
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before messages can be sent."), 403
        role = room["participants"][participant_id]
        # Before accepting an interviewer message, reject it if this claim already has 10 questions
        if role == "interviewer" and room["current_claim"]["interviewer_question_count"] >= 10:
            return jsonify(error="This claim has already received 10 questions from the interviewer."), 403
        # Reject this request unless role matches the room's next allowed speaker.
        if role != room["next_speaker"]:
            return jsonify(error="It is not your turn to speak."), 403
    
        message = {
            "id": len(room["messages"]) + 1,
            "participant_id": participant_id,
            "role": role,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        room["messages"].append(message)
        # If the accepted message is from the interviewer, increment this claim's question count.
        if role == "interviewer":
            room["current_claim"]["interviewer_question_count"] += 1
        # When question 10 is asked, still allow the interviewee's tenth answer; after that answer, end question turns for this claim.
        if room["current_claim"]["interviewer_question_count"] >= 10 and role == "interviewee":
            room["current_claim"]["question_turns_ended"] = True
        if room["current_claim"].get("question_turns_ended"):
            room["next_speaker"] = None
        else:
            # Otherwise, switch the next allowed speaker to the other role.
            room["next_speaker"] = "interviewer" if role == "interviewee" else "interviewee"

    return jsonify(message=_message_for_client(message)), 201 # Return the newly created message without its private timestamp


# POST endpoint for the interviewer to submit a true/false judgement and 0–100 confidence after all ten answers; derive the role and active dialogue ID from the bound room, then store the judgement on its current_claim.
@app.post("/api/rooms/<code>/judgement") # Endpoint for the interviewer to submit a true/false judgement and confidence after all ten answers
@consent_required
@screening_required
@instructions_required
def submit_judgement(code):
    data = request.get_json(silent=True) or {}
    participant_id = _get_participant_id(data)
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400

    with rooms_lock:
        room, participant_id = _bound_room(code, participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before submitting a judgement."), 403
        if room["participants"][participant_id] != "interviewer":
            return jsonify(error="Only the interviewer can submit a judgement."), 403
        if room["current_claim"].get("question_turns_ended") is not True:
            return jsonify(error="All ten questions must be asked before submitting a judgement."), 403

        judgement = data.get("judgement")
        confidence = data.get("confidence")
        if judgement not in ["true", "false"]:
            return jsonify(error="Judgement must be true or false."), 400
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0 <= confidence <= 100):
            return jsonify(error="Confidence must be a number between 0 and 100."), 400

        room["current_claim"]["judgement"] = {
            "judgement": judgement,
            "confidence": confidence,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        }

    return jsonify(status="ok"), 201

TYPING_WINDOW_SECONDS = 3.0


@app.post("/api/rooms/<code>/typing") # Sets the typing status for a participant in a specific room; used to indicate that the participant is currently typing
@consent_required
@screening_required
@instructions_required
def set_typing(code):
    data = request.get_json(silent=True) or {} # Parse the JSON body of the request, or use an empty dictionary if parsing fails
    participant_id = _get_participant_id(data) # Extract the participant ID from the request data
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400 # Return an error if the participant ID is missing

    with rooms_lock:
        room, participant_id = _bound_room(code, participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403
        if room["current_claim"] is None:
            return jsonify(error="The opening claim must be available before typing."), 403
        # Reject typing pings unless this participant's role is room["next_speaker"].
        if room["next_speaker"] != room["participants"][participant_id]:
            return jsonify(error="It's not your turn to type."), 403

        room["typing"][participant_id] = monotonic() # Update the timestamp of the participant's last typing activity
    return jsonify(status="ok") # Return a success status after setting the typing indicator


@app.get("/api/rooms/<code>/messages") # Retrieves messages from a specific room for a participant and indicates if their partner is currently typing
@consent_required
@screening_required
@instructions_required
def get_room_messages(code):
    participant_id = request.args.get("participant_id", "")
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        return jsonify(error="after_id must be a non-negative integer."), 400

    with rooms_lock:
        room, participant_id = _bound_room(code, participant_id)
        if room is None or not _chat_ready(room):
            return jsonify(error="Both participants must be ready in your assigned room."), 403

        messages = [
            _message_for_client(message)
            for message in room["messages"]
            if message["id"] > after_id
        ]
        now = monotonic()
        partner_typing = any(
            partner_id != participant_id and now - last < TYPING_WINDOW_SECONDS
            for partner_id, last in room["typing"].items()
        )
        current_claim = room["current_claim"]
        question_count = current_claim["interviewer_question_count"] if current_claim else 0
        question_turns_ended = bool(current_claim and current_claim.get("question_turns_ended"))
        if question_turns_ended:
            question_number = 10
        elif room["next_speaker"] == "interviewee":
            question_number = max(question_count, 1)
        else:
            question_number = min(question_count + 1, 10)
        # This response is bound to the requester; it exposes no city answer or claim truth.
        can_send = room["next_speaker"] == room["participants"][participant_id]

    return jsonify(
        messages=messages,
        partner_typing=partner_typing,
        can_send=can_send,
        question_number=question_number,
        question_turns_ended=question_turns_ended,
    )
