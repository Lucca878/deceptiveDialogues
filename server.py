from datetime import date
from functools import wraps
from os import environ
import re
from secrets import choice
from secrets import token_hex
from string import ascii_uppercase, digits
from threading import Lock
from time import monotonic

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.config["SECRET_KEY"] = environ.get("FLASK_SECRET_KEY") or token_hex(32)
rooms = {}
rooms_lock = Lock()
WINDOW_MONTHS = 36
CITY_COUNT = 5
ALLOW_FEWER_THAN_5 = False
MONTHS = (
    ("01", "January"),
    ("02", "February"),
    ("03", "March"),
    ("04", "April"),
    ("05", "May"),
    ("06", "June"),
    ("07", "July"),
    ("08", "August"),
    ("09", "September"),
    ("10", "October"),
    ("11", "November"),
    ("12", "December"),
)
COUNTRIES = tuple(sorted("""Afghanistan
Albania
Algeria
Andorra
Angola
Antigua and Barbuda
Argentina
Armenia
Australia
Austria
Azerbaijan
Bahamas
Bahrain
Bangladesh
Barbados
Belarus
Belgium
Belize
Benin
Bhutan
Bolivia
Bosnia and Herzegovina
Botswana
Brazil
Brunei
Bulgaria
Burkina Faso
Burundi
Cabo Verde
Cambodia
Cameroon
Canada
Central African Republic
Chad
Chile
China
Colombia
Comoros
Congo
Costa Rica
Côte d'Ivoire
Croatia
Cuba
Cyprus
Czechia
Democratic Republic of the Congo
Denmark
Djibouti
Dominica
Dominican Republic
Ecuador
Egypt
El Salvador
Equatorial Guinea
Eritrea
Estonia
Eswatini
Ethiopia
Fiji
Finland
France
Gabon
Gambia
Georgia
Germany
Ghana
Greece
Grenada
Guatemala
Guinea
Guinea-Bissau
Guyana
Haiti
Honduras
Hungary
Iceland
India
Indonesia
Iran
Iraq
Ireland
Israel
Italy
Jamaica
Japan
Jordan
Kazakhstan
Kenya
Kiribati
Kuwait
Kyrgyzstan
Laos
Latvia
Lebanon
Lesotho
Liberia
Libya
Liechtenstein
Lithuania
Luxembourg
Madagascar
Malawi
Malaysia
Maldives
Mali
Malta
Marshall Islands
Mauritania
Mauritius
Mexico
Micronesia
Moldova
Monaco
Mongolia
Montenegro
Morocco
Mozambique
Myanmar
Namibia
Nauru
Nepal
Netherlands
New Zealand
Nicaragua
Niger
Nigeria
North Korea
North Macedonia
Norway
Oman
Pakistan
Palau
Palestine
Panama
Papua New Guinea
Paraguay
Peru
Philippines
Poland
Portugal
Qatar
Romania
Russia
Rwanda
Saint Kitts and Nevis
Saint Lucia
Saint Vincent and the Grenadines
Samoa
San Marino
Sao Tome and Principe
Saudi Arabia
Senegal
Serbia
Seychelles
Sierra Leone
Singapore
Slovakia
Slovenia
Solomon Islands
Somalia
South Africa
South Korea
South Sudan
Spain
Sri Lanka
Sudan
Suriname
Sweden
Switzerland
Syria
Tajikistan
Tanzania
Thailand
Timor-Leste
Togo
Tonga
Trinidad and Tobago
Tunisia
Türkiye
Turkmenistan
Tuvalu
Uganda
Ukraine
United Arab Emirates
United Kingdom
United States
Uruguay
Uzbekistan
Vanuatu
Vatican City
Venezuela
Vietnam
Yemen
Zambia
Zimbabwe
Åland Islands
American Samoa
Anguilla
Antarctica
Aruba
Bermuda
Bonaire, Sint Eustatius and Saba
British Indian Ocean Territory
British Virgin Islands
Cayman Islands
Christmas Island
Cocos (Keeling) Islands
Cook Islands
Curaçao
Falkland Islands
Faroe Islands
French Guiana
French Polynesia
French Southern Territories
Gibraltar
Greenland
Guadeloupe
Guam
Guernsey
Hong Kong
Isle of Man
Jersey
Kosovo
Macao
Martinique
Mayotte
Montserrat
New Caledonia
Niue
Norfolk Island
Northern Mariana Islands
Puerto Rico
Réunion
Saint Barthélemy
Saint Helena, Ascension and Tristan da Cunha
Saint Martin (French part)
Saint Pierre and Miquelon
Sint Maarten (Dutch part)
South Georgia and the South Sandwich Islands
Svalbard and Jan Mayen
Taiwan
Tokelau
Turks and Caicos Islands
United States Minor Outlying Islands
United States Virgin Islands
Wallis and Futuna
Western Sahara""".splitlines(), key=str.casefold))


def _window_months(today=None):
    today = today or date.today()
    current_month = today.year * 12 + today.month - 1
    start_month = current_month - WINDOW_MONTHS
    start_year, start_month_index = divmod(start_month, 12)
    start_month = f"{start_year:04d}-{start_month_index + 1:02d}"
    current_month = f"{today.year:04d}-{today.month:02d}"
    window_label = date(start_year, start_month_index + 1, 1).strftime("%B %Y")
    return start_month, current_month, window_label


def _visit_month_options(start_month, current_month):
    start_index = int(start_month[:4]) * 12 + int(start_month[5:]) - 1
    current_index = int(current_month[:4]) * 12 + int(current_month[5:]) - 1
    options_by_year = []

    for year in range(int(current_month[:4]), int(start_month[:4]) - 1, -1):
        year_options = []
        for month_number, month_name in reversed(MONTHS):
            option_index = year * 12 + int(month_number) - 1
            if start_index <= option_index <= current_index:
                year_options.append((f"{year:04d}-{month_number}", month_name))
        if year_options:
            options_by_year.append((year, year_options))

    return options_by_year


def consent_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("consent_accepted") is not True:
            if request.path.startswith("/api/"):
                return jsonify(error="Please provide consent before continuing."), 403
            return redirect(url_for("consent"))
        return view(*args, **kwargs)

    return wrapped


def screening_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("travel_screened_out") is True:
            if request.path.startswith("/api/"):
                return jsonify(error="This study is not available after the travel screening."), 403
            return redirect(url_for("screened_out_cities"))
        if session.get("travel_screening_complete") is not True:
            if request.path.startswith("/api/"):
                return jsonify(error="Please complete the travel questions before continuing."), 403
            return redirect(url_for("travel_intro"))
        return view(*args, **kwargs)

    return wrapped


def _travel_answers(form):
    answers = []
    for index in range(1, CITY_COUNT + 1):
        visit_year = form.get(f"visit_year_{index}", "")
        visit_month_number = form.get(f"visit_month_number_{index}", "")
        answers.append({
            "city": form.get(f"city_{index}", "").strip(),
            "country": form.get(f"country_{index}", ""),
            "visit_year": visit_year,
            "visit_month_number": visit_month_number,
            "visit_month": f"{visit_year}-{visit_month_number}" if visit_year and visit_month_number else "",
            "duration": form.get(f"duration_{index}", ""),
            "reason": form.get(f"reason_{index}", ""),
            "companions": form.get(f"companions_{index}", ""),
            "familiarity": form.get(f"familiarity_{index}", ""),
        })
    return answers


def _validate_travel_answers(answers, start_month, current_month):
    errors = {}
    seen_cities = {}
    allowed_choices = {
        "duration": {"1_day_or_less", "2_3_days", "4_7_days", "more_than_a_week"},
        "reason": {"holiday", "work_or_study", "visiting_family_or_friends", "other"},
        "companions": {"alone", "partner", "family", "friends", "colleagues", "other"},
        "familiarity": {str(value) for value in range(1, 8)},
    }

    for index, answer in enumerate(answers, start=1):
        for field in ("city", "country", "visit_month", "duration", "reason", "companions", "familiarity"):
            if not answer[field]:
                errors[f"{field}_{index}"] = "Please complete this field."

        if len(answer["city"]) > 100:
            errors[f"city_{index}"] = "City names must be 100 characters or fewer."

        if answer["country"] and answer["country"] not in COUNTRIES:
            errors[f"country_{index}"] = "Please choose a country from the list."

        if answer["visit_month"]:
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", answer["visit_month"]):
                errors[f"visit_month_{index}"] = "Choose a valid month and year."
            elif not start_month <= answer["visit_month"] <= current_month:
                errors[f"visit_month_{index}"] = "Choose a visit within the date window shown above."

        for field, choices in allowed_choices.items():
            if answer[field] and answer[field] not in choices:
                errors[f"{field}_{index}"] = "Choose one of the listed options."

        if answer["city"] and answer["country"] in COUNTRIES:
            normalized_city = " ".join(answer["city"].split()).casefold()
            normalized_country = answer["country"].casefold()
            key = f"{normalized_city}|{normalized_country}"
            if key in seen_cities:
                errors[f"city_{index}"] = "Each entry must be a different city and country."
                errors[f"city_{seen_cities[key]}"] = "Each entry must be a different city and country."
            else:
                seen_cities[key] = index

    return errors


def _get_participant_id(data):
    participant_id = data.get("participant_id")
    if not isinstance(participant_id, str) or not participant_id or len(participant_id) > 64:
        return None
    return participant_id


@app.get("/")
def landing():
    return render_template("landing.html")


@app.get("/dialogue")
@consent_required
@screening_required
def dialogue():
    return render_template("dialogue.html")


@app.get("/room")
@consent_required
@screening_required
def room():
    return render_template("room.html")


@app.get("/consent")
def consent():
    if session.get("consent_accepted") is True:
        return redirect(url_for("travel_intro"))
    return render_template("consent.html")


@app.get("/start")
def start_study():
    session.pop("consent_accepted", None)
    session.pop("travel_intro_seen", None)
    session.pop("travel_screening_complete", None)
    session.pop("travel_screened_out", None)
    return redirect(url_for("consent"))


@app.post("/consent")
def consent_decision():
    decision = request.form.get("decision")
    if decision == "accept":
        session["consent_accepted"] = True
        session.pop("travel_intro_seen", None)
        session.pop("travel_screening_complete", None)
        session.pop("travel_screened_out", None)
        return redirect(url_for("travel_intro"))
    if decision == "deny":
        session.pop("consent_accepted", None)
        session.pop("travel_intro_seen", None)
        session.pop("travel_screening_complete", None)
        session.pop("travel_screened_out", None)
        return render_template("no_consent.html")
    return "Invalid consent choice.", 400


@app.get("/travel-intro")
@consent_required
def travel_intro():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if session.get("travel_screening_complete") is True:
        return redirect(url_for("room"))
    return render_template("travel_intro.html")


@app.post("/travel-intro")
@consent_required
def begin_travel_screening():
    session["travel_intro_seen"] = True
    return redirect(url_for("travel_cities"))


@app.route("/travel-cities", methods=["GET", "POST"])
@consent_required
def travel_cities():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if session.get("travel_intro_seen") is not True:
        return redirect(url_for("travel_intro"))
    if session.get("travel_screening_complete") is True:
        return redirect(url_for("room"))

    start_month, current_month, window_label = _window_months()
    if request.method == "POST":
        answers = _travel_answers(request.form)
        errors = _validate_travel_answers(answers, start_month, current_month)
        if errors:
            return render_template(
                "travel_cities.html",
                answers=answers,
                errors=errors,
                start_month=start_month,
                current_month=current_month,
                window_label=window_label,
                countries=COUNTRIES,
                months=MONTHS,
                visit_month_options=_visit_month_options(start_month, current_month),
            ), 400

        session["travel_screening_complete"] = True
        return redirect(url_for("room"))

    answers = _travel_answers({})
    return render_template(
        "travel_cities.html",
        answers=answers,
        errors={},
        start_month=start_month,
        current_month=current_month,
        window_label=window_label,
        countries=COUNTRIES,
        months=MONTHS,
        visit_month_options=_visit_month_options(start_month, current_month),
    )


@app.get("/travel-cities/unable")
@consent_required
def unable_to_name_cities():
    if session.get("travel_screening_complete") is True:
        return redirect(url_for("room"))
    if session.get("travel_intro_seen") is not True:
        return redirect(url_for("travel_intro"))
    if ALLOW_FEWER_THAN_5:
        session["travel_screening_complete"] = True
        return redirect(url_for("room"))
    session["travel_screened_out"] = True
    return render_template("screened_out.html")


@app.get("/screened-out-cities")
@consent_required
def screened_out_cities():
    if session.get("travel_screened_out") is not True:
        return redirect(url_for("travel_intro"))
    return render_template("screened_out.html")


@app.post("/api/rooms")
@consent_required
@screening_required
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
@consent_required
@screening_required
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
@consent_required
@screening_required
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
@consent_required
@screening_required
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
@consent_required
@screening_required
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
@consent_required
@screening_required
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
