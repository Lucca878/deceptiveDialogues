from secrets import choice
from string import ascii_uppercase, digits
from threading import Lock
from time import monotonic

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
rooms = {}
rooms_lock = Lock()


def _get_participant_id(data):
    participant_id = data.get("participant_id")
    if not isinstance(participant_id, str) or not participant_id or len(participant_id) > 64:
        return None
    return participant_id


@app.get("/")
def landing():
    return render_template("landing.html")


@app.get("/dialogue")
def dialogue():
    return render_template("dialogue.html")


@app.get("/room")
def room():
    return render_template("room.html")


@app.post("/api/rooms")
def create_room():
    data = request.get_json(silent=True) or {}
    participant_id = _get_participant_id(data)
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400

    alphabet = ascii_uppercase + digits
    with rooms_lock:
        code = "".join(choice(alphabet) for _ in range(6))
        while code in rooms:
            code = "".join(choice(alphabet) for _ in range(6))

        rooms[code] = {"participants": {participant_id: None}, "messages": [], "typing": {}}
    return jsonify(code=code, status="waiting")


@app.post("/api/rooms/<code>/join")
def join_room(code):
    data = request.get_json(silent=True) or {}
    participant_id = _get_participant_id(data)
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400

    with rooms_lock:
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. Check the code and try again."), 404

        participants = room["participants"]
        if participant_id not in participants:
            if len(participants) >= 2:
                return jsonify(error="This room is already full."), 409
            participants[participant_id] = None

        if len(participants) == 2 and all(role is None for role in participants.values()):
            participant_ids = list(participants)
            roles = ["interviewer", "interviewee"]
            if choice((True, False)):
                roles.reverse()
            participants.update(zip(participant_ids, roles))

        role = participants[participant_id]
    if role is None:
        return jsonify(code=code.upper(), status="waiting")
    return jsonify(code=code.upper(), status="paired", role=role)


@app.get("/api/rooms/<code>")
def room_status(code):
    participant_id = request.args.get("participant_id", "")
    with rooms_lock:
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404

        participants = room["participants"]
        if participant_id not in participants:
            return jsonify(error="This participant is not in the room."), 403

        role = participants[participant_id]
    if role is None:
        return jsonify(code=code.upper(), status="waiting")
    return jsonify(code=code.upper(), status="paired", role=role)


@app.post("/api/rooms/<code>/messages")
def send_room_message(code):
    data = request.get_json(silent=True) or {}
    participant_id = _get_participant_id(data)
    text = data.get("text")
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="A non-empty message is required."), 400
    text = text.strip()
    if len(text) > 2000:
        return jsonify(error="Messages must be 2000 characters or fewer."), 400

    with rooms_lock:
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404

        role = room["participants"].get(participant_id)
        if role is None:
            return jsonify(error="You are not a paired participant in this room."), 403

        message = {
            "id": len(room["messages"]) + 1,
            "participant_id": participant_id,
            "role": role,
            "text": text,
        }
        room["messages"].append(message)

    return jsonify(message=message), 201


TYPING_WINDOW_SECONDS = 3.0


@app.post("/api/rooms/<code>/typing")
def set_typing(code):
    data = request.get_json(silent=True) or {}
    participant_id = _get_participant_id(data)
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400

    with rooms_lock:
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404
        if room["participants"].get(participant_id) is None:
            return jsonify(error="You are not a paired participant in this room."), 403

        room["typing"][participant_id] = monotonic()
    return jsonify(status="ok")


@app.get("/api/rooms/<code>/messages")
def get_room_messages(code):
    participant_id = request.args.get("participant_id", "")
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        return jsonify(error="after_id must be a non-negative integer."), 400

    with rooms_lock:
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404
        if room["participants"].get(participant_id) is None:
            return jsonify(error="You are not a paired participant in this room."), 403

        messages = [message for message in room["messages"] if message["id"] > after_id]
        now = monotonic()
        partner_typing = any(
            partner_id != participant_id and now - last < TYPING_WINDOW_SECONDS
            for partner_id, last in room["typing"].items()
        )

    return jsonify(messages=messages, partner_typing=partner_typing)
