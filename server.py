from datetime import date, datetime, timezone
from functools import wraps
from os import environ
from random import SystemRandom
import re
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
WINDOW_MONTHS = 36
CITY_COUNT = 5
ALLOW_FEWER_THAN_5 = False
N_CANDIDATES_SHOWN = 10
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


def _window_months(today=None): # Calculate the start month, current month, and window label for the travel screening period
    today = today or date.today()
    current_month = today.year * 12 + today.month - 1  # Convert the current date to a month index for comparison
    start_month = current_month - WINDOW_MONTHS # Calculate the start month index based on the defined window of months
    start_year, start_month_index = divmod(start_month, 12)  # Convert the start month index to year and month components
    start_month = f"{start_year:04d}-{start_month_index + 1:02d}"  # Format the start month as YYYY-MM
    current_month = f"{today.year:04d}-{today.month:02d}"  # Format the current month as YYYY-MM
    window_label = date(start_year, start_month_index + 1, 1).strftime("%B %Y")  # Create a human-readable label for the window
    return start_month, current_month, window_label


def _visit_month_options(start_month, current_month): # Generate a list of visit month options for a dropdown, grouped by year, within the allowed window
    start_index = int(start_month[:4]) * 12 + int(start_month[5:]) - 1  # Convert the start month to a month index for comparison
    current_index = int(current_month[:4]) * 12 + int(current_month[5:]) - 1  # Convert the current month to a month index for comparison
    options_by_year = []  # Initialize a list to hold the visit month options grouped by year

    for year in range(int(current_month[:4]), int(start_month[:4]) - 1, -1):  # Iterate over each year from current to start
        year_options = []  # Initialize a list to hold the month options for the current year
        for month_number, month_name in reversed(MONTHS):  # Iterate over months in reverse order (December to January)
            option_index = year * 12 + int(month_number) - 1  # Calculate the month index for comparison
            if start_index <= option_index <= current_index:  # Include only months within the allowed window
                year_options.append((f"{year:04d}-{month_number}", month_name))  # Add the month option as a tuple of (YYYY-MM, month name)
        if year_options:  # If there are any valid month options for the year, add them to the grouped options
            options_by_year.append((year, year_options))  # Append the year and its corresponding month options to the list of grouped options

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
        if session.get("other_cities_complete") is not True:
            if request.path.startswith("/api/"):
                return jsonify(error="Please complete the other-city questions before continuing."), 403
            return redirect(url_for("other_cities"))
        return view(*args, **kwargs)

    return wrapped


def _city_key(city, country):
    return f"{' '.join(city.split()).casefold()}|{' '.join(country.split()).casefold()}"


def _select_other_cities(answers): # Select a set of other cities that the user has not visited based on the provided travel answers
    visited = {_city_key(answer["city"], answer["country"]) for answer in answers}  # Create a set of city keys for cities that the user has already visited
    eligible = [
        city_id for city_id, city, country in CANDIDATE_CITIES  # Iterate over candidate cities and select those not already visited
        if _city_key(city, country) not in visited
    ]
    return SystemRandom().sample(eligible, N_CANDIDATES_SHOWN)  # Randomly select a subset of eligible cities to show as other cities


def _travel_answers(form):  # Extract travel answers from the submitted form data
    answers = []
    for index in range(1, CITY_COUNT + 1): # Iterate over each city index to extract the corresponding travel answers from the form
        visit_year = form.get(f"visit_year_{index}", "")  # Extract the visit year for the current city
        visit_month_number = form.get(f"visit_month_number_{index}", "")  # Extract the visit month number for the current city
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
        })  # Append the extracted travel answer for the current city to the answers list
    return answers


def _validate_travel_answers(answers, start_month, current_month):  # Validate the extracted travel answers against the allowed window and other rules
    errors = {}  # Initialize an empty dictionary to store validation errors for each travel answer field
    seen_cities = {}  # Keep track of cities that have already been entered to detect duplicates
    allowed_choices = {  # Define the allowed choices for each travel answer field
        "duration": {"1_day_or_less", "2_3_days", "4_7_days", "more_than_a_week"},
        "reason": {"holiday", "work_or_study", "visiting_family_or_friends", "other"},
        "companions": {"alone", "partner", "family", "friends", "colleagues", "other"},
        "familiarity": {str(value) for value in range(1, 8)},
    }

    for index, answer in enumerate(answers, start=1):  # Iterate over each travel answer and validate its fields
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
            key = _city_key(answer["city"], answer["country"])
            if key in seen_cities:
                errors[f"city_{index}"] = "Each entry must be a different city and country."
                errors[f"city_{seen_cities[key]}"] = "Each entry must be a different city and country."
            else:
                seen_cities[key] = index

    return errors # Return the dictionary of validation errors for the travel answers


def _get_participant_id(data):
    participant_id = data.get("participant_id")
    if not isinstance(participant_id, str) or not participant_id or len(participant_id) > 64:
        return None
    return participant_id


def _message_for_client(message):
    return {key: message[key] for key in ("id", "participant_id", "role", "text")}


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
    return redirect(url_for("consent")) # redirect to consent page


@app.post("/consent") # Handle consent decision (accept or deny)
def consent_decision():
    decision = request.form.get("decision") # Get the user's consent decision from the form; request.form contains the submitted form data from current request; get retrieves the value for the given key, or None if the key is not present
    if decision in ("accept", "deny"): # If the decision is either "accept" or "deny", clear session data related to other cities
        # This ensures that any previous travel city selections and related session data are removed when the user makes a new consent decision; means a new flow was started
        session.pop("other_city_ids", None)
        session.pop("other_cities_complete", None)
        session.pop("travel_intro_seen", None)
        session.pop("travel_screening_complete", None)
        session.pop("travel_screened_out", None)
    if decision == "accept":
        session["consent_accepted"] = True
        return redirect(url_for("travel_intro"))
    if decision == "deny":
        session.pop("consent_accepted", None)
        return render_template("no_consent.html")
    return "Invalid consent choice.", 400


@app.get("/travel-intro")
@consent_required # Ensure the user has given consent before accessing the travel intro page
def travel_intro(): # Display the travel introduction page if the user has not been screened out or completed travel screening
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


@app.route("/travel-cities", methods=["GET", "POST"]) # Handle travel cities page for both GET and POST requests
@consent_required
def travel_cities():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if session.get("travel_intro_seen") is not True:
        return redirect(url_for("travel_intro"))
    if session.get("travel_screening_complete") is True:
        return redirect(url_for("room"))

    start_month, current_month, window_label = _window_months() # Get the start month, current month, and window label for travel screening
    if request.method == "POST":
        answers = _travel_answers(request.form)  # Extract travel answers from the submitted form data
        errors = _validate_travel_answers(answers, start_month, current_month)  # Validate the extracted travel answers against the allowed window
        if errors: # If there are validation errors, re-render the travel cities page with the errors and previously entered answers
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

        session["other_city_ids"] = _select_other_cities(answers)  # Store the selected other cities in the session
        session.pop("other_cities_complete", None)  # Clear any previous completion status for other cities
        session["travel_screening_complete"] = True  # Mark the travel screening as complete
        return redirect(url_for("other_cities"))  # Redirect to the other cities page after completing travel screening

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


@app.post("/travel-cities/unable")
@consent_required
def unable_to_name_cities():
    if session.get("travel_screening_complete") is True:
        return redirect(url_for("room"))
    if session.get("travel_intro_seen") is not True:
        return redirect(url_for("travel_intro"))
    if ALLOW_FEWER_THAN_5:
        session["other_city_ids"] = _select_other_cities([])
        session["travel_screening_complete"] = True
        return redirect(url_for("other_cities"))
    session["travel_screened_out"] = True
    return render_template("screened_out.html")


@app.get("/screened-out-cities")
@consent_required
def screened_out_cities():
    if session.get("travel_screened_out") is not True:
        return redirect(url_for("travel_intro"))
    return render_template("screened_out.html")


@app.route("/other-cities", methods=["GET", "POST"]) # Handles the form for other cities visited by the participant
@consent_required
def other_cities():
    if session.get("travel_screened_out") is True:
        return redirect(url_for("screened_out_cities"))
    if session.get("travel_screening_complete") is not True:
        return redirect(url_for("travel_intro"))
    if session.get("other_cities_complete") is True:
        return redirect(url_for("room"))

    city_lookup = {city_id: (city, country) for city_id, city, country in CANDIDATE_CITIES} # Maps city IDs to their corresponding city and country for quick lookup
    selected_ids = session.get("other_city_ids", []) # Retrieves the list of other city IDs selected by the participant from the session
    if len(selected_ids) != N_CANDIDATES_SHOWN or any(city_id not in city_lookup for city_id in selected_ids):
        # A session from before this step was introduced must repeat the visited-city form.
        session.pop("travel_screening_complete", None) # Clears the travel screening completion flag to force the participant to redo the form
        return redirect(url_for("travel_cities"))

    cities = [(city_id, *city_lookup[city_id]) for city_id in selected_ids] # Prepares the list of cities with their IDs, names, and countries for rendering the form
    values = {} # Stores the form values submitted by the participant
    errors = {} # Stores validation errors for the form fields  
    if request.method == "POST": # Handles the form submission for other cities
        for city_id in selected_ids:
            for field, allowed in (("visited", {"yes", "no"}), ("familiarity", {str(n) for n in range(1, 8)})):
                key = f"{field}_{city_id}"
                values[key] = request.form.get(key, "")
                if values[key] not in allowed:
                    errors[key] = "Please choose Yes or No." if field == "visited" else "Please choose a rating from 1 to 7." # Sets the appropriate error message based on the field type
        if not errors:
            session["other_cities_complete"] = True
            return redirect(url_for("room"))

    return render_template("other_cities.html", cities=cities, values=values, errors=errors), 400 if errors else 200


@app.post("/api/rooms")
@consent_required
@screening_required
def match_room(): # Handles the API request to match a participant to a room
    data = request.get_json(silent=True) or {}
    participant_id = _get_participant_id(data)
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400

    with rooms_lock:
        # Returning clients keep their place, even when every room is occupied. This ensures that participants who have already been assigned a room do not lose their spot.
        for code, room in rooms.items():
            if participant_id in room["participants"]:
                role = room["participants"][participant_id]
                if role is None:
                    return jsonify(code=code, status="waiting")
                return jsonify(code=code, status="paired", role=role)

        # Fill a waiting room before opening the next unused code. This prioritizes pairing participants in existing rooms with one participant already waiting.
        code = next((code for code in ROOM_CODES if code in rooms and len(rooms[code]["participants"]) == 1), None)
        if code is None:
            code = next((code for code in ROOM_CODES if code not in rooms), None)
            if code is None:
                return jsonify(error="All rooms are currently occupied."), 409
            rooms[code] = {"participants": {participant_id: None}, "messages": [], "typing": {}}
            return jsonify(code=code, status="waiting")

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
def room_status(code):
    participant_id = request.args.get("participant_id", "") # Retrieves the participant ID from the query parameters
    with rooms_lock: # Ensures thread-safe access to the rooms dictionary
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404

        participants = room["participants"]
        if participant_id not in participants:
            return jsonify(error="This participant is not in the room."), 403

        role = participants[participant_id]
    if role is None: # If the participant has not been assigned a role yet, they are still waiting
        return jsonify(code=code.upper(), status="waiting")
    return jsonify(code=code.upper(), status="paired", role=role) # Returns the participant's role if they are paired


@app.post("/api/rooms/<code>/messages")
@consent_required
@screening_required
def send_room_message(code): # Handles sending a message to a specific room by a participant
    data = request.get_json(silent=True) or {} # Get the JSON data from the request body, or use an empty dictionary if none is provided
    participant_id = _get_participant_id(data) # Extract the participant ID from the request data
    text = data.get("text") # Extract the message text from the request data
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400 # Return an error if no participant ID is provided
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="A non-empty message is required."), 400 # Return an error if the message text is empty or not a string
    text = text.strip() # Trim leading and trailing whitespace from the message text
    if len(text) > 2000:
        return jsonify(error="Messages must be 2000 characters or fewer."), 400 # Return an error if the message exceeds the character limit    

    with rooms_lock:
        room = rooms.get(code.upper())
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404 # Return an error if the room does not exist

        role = room["participants"].get(participant_id)
        if role is None:
            return jsonify(error="You are not a paired participant in this room."), 403 # Return an error if the participant is not paired in the room

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
def set_typing(code):
    data = request.get_json(silent=True) or {} # Parse the JSON body of the request, or use an empty dictionary if parsing fails
    participant_id = _get_participant_id(data) # Extract the participant ID from the request data
    if participant_id is None:
        return jsonify(error="A participant ID is required."), 400 # Return an error if the participant ID is missing

    with rooms_lock:
        room = rooms.get(code.upper()) # Retrieve the room object for the given code, or None if it doesn't exist
        if room is None:
            return jsonify(error="Room not found. It may have expired."), 404 # Return an error if the room does not exist
        if room["participants"].get(participant_id) is None:
            return jsonify(error="You are not a paired participant in this room."), 403 # Return an error if the participant is not paired in the room

        room["typing"][participant_id] = monotonic() # Update the timestamp of the participant's last typing activity
    return jsonify(status="ok") # Return a success status after setting the typing indicator


@app.get("/api/rooms/<code>/messages") # Retrieves messages from a specific room for a participant and indicates if their partner is currently typing
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
