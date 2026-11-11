from datetime import datetime, timezone
from functools import wraps
from os import environ
from secrets import choice
from secrets import token_hex
from threading import Lock
from time import monotonic

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from city_candidates import CANDIDATE_CITIES

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.config["SECRET_KEY"] = environ.get("FLASK_SECRET_KEY") or token_hex(32)
rooms = {}
rooms_lock = Lock()
ROOM_CODES = tuple(f"ROOM{number:02d}" for number in range(1, 21))
SCREENING_VERSION = "english-cities-v1"


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
    return (
        session.get("travel_intro_seen") is True
        and session.get("travel_screening_complete") == SCREENING_VERSION
    )


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
        return render_template("dialogue.html")


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
            rooms[code] = {"participants": {participant_id: None}, "messages": [], "typing": {}, "ready": set(), "owners": {participant_id: token}}
            session.update(room_code=code, room_participant_id=participant_id, room_token=token)
            return jsonify(code=code, status="waiting")

        token = token_hex(32)
        rooms[code]["owners"][participant_id] = token
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
        role = room["participants"][participant_id]

        message = {
            "id": len(room["messages"]) + 1,
            "participant_id": participant_id,
            "role": role,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        room["messages"].append(message)

    return jsonify(message=_message_for_client(message)), 201 # Return the newly created message without its private timestamp


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

    return jsonify(messages=messages, partner_typing=partner_typing)
