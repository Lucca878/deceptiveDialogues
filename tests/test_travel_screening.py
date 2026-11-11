from html.parser import HTMLParser
import unittest

from werkzeug.datastructures import MultiDict

import server


EXPECTED_CITIES = (
    ("liverpool", "Liverpool", "England"),
    ("bristol", "Bristol", "England"),
    ("leicester", "Leicester", "England"),
    ("coventry", "Coventry", "England"),
    ("wakefield", "Wakefield", "England"),
    ("nottingham", "Nottingham", "England"),
    ("newcastle-upon-tyne", "Newcastle upon Tyne", "England"),
    ("doncaster", "Doncaster", "England"),
    ("milton-keynes", "Milton Keynes", "England"),
    ("salford", "Salford", "England"),
    ("sunderland", "Sunderland", "England"),
    ("brighton-and-hove", "Brighton and Hove", "England"),
    ("wolverhampton", "Wolverhampton", "England"),
    ("kingston-upon-hull", "Kingston upon Hull", "England"),
    ("plymouth", "Plymouth", "England"),
    ("derby", "Derby", "England"),
    ("stoke-on-trent", "Stoke-on-Trent", "England"),
    ("southampton", "Southampton", "England"),
    ("peterborough", "Peterborough", "England"),
    ("westminster", "Westminster", "England"),
)


class FormParser(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.inputs = []
        self.headings = []
        self.scripts = []
        self.forms = []
        self.buttons = []
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.inputs.append(attrs)
        if tag == "h2":
            self.headings.append(attrs.get("id"))
        if tag == "script":
            self.scripts.append(attrs)
        if tag == "form":
            self.forms.append(attrs)
        if tag == "button":
            self.buttons.append(attrs)


class TravelScreeningTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        with server.rooms_lock:
            server.rooms.clear()

    def tearDown(self):
        with server.rooms_lock:
            server.rooms.clear()

    def assert_redirect(self, response, path):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], path)

    def accept_consent(self, destination="/travel-intro"):
        self.assert_redirect(self.client.post("/consent", data={"decision": "accept"}), destination)

    def begin_screening(self):
        self.accept_consent()
        self.assert_redirect(self.client.post("/travel-intro"), "/travel-cities")

    def answers(self, yes_count=5, rating="4"):
        data = {}
        for index, (city_id, _, _) in enumerate(EXPECTED_CITIES):
            data[f"visited_{city_id}"] = "yes" if index < yes_count else "no"
            data[f"familiarity_{city_id}"] = rating
        return data

    def assert_all_gates_blocked(self, destination):
        for path in ("/room", "/dialogue", "/role-instructions"):
            self.assert_redirect(self.client.get(path), destination)
        self.assert_redirect(self.client.post("/role-instructions"), destination)
        for method, path in (
            ("POST", "/api/rooms"),
            ("GET", "/api/rooms/ROOM01?participant_id=test"),
            ("POST", "/api/rooms/ROOM01/messages"),
            ("GET", "/api/rooms/ROOM01/messages?participant_id=test"),
            ("POST", "/api/rooms/ROOM01/typing"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.open(path, method=method, json={"participant_id": "test", "text": "Hello"})
                self.assertEqual(response.status_code, 403)
                self.assertIn("error", response.get_json())
        self.assertEqual(server.rooms, {})

    def test_exact_city_ids_names_order_and_complete_rendered_form(self):
        self.assertEqual(server.CANDIDATE_CITIES, EXPECTED_CITIES)
        self.begin_screening()
        response = self.client.get("/travel-cities")
        self.assertEqual(response.status_code, 200)
        form = FormParser(response)
        self.assertEqual(form.headings, [f"candidate-{city_id}" for city_id, _, _ in EXPECTED_CITIES])
        self.assertEqual(len(form.inputs), 180)
        self.assertEqual(len({entry["id"] for entry in form.inputs}), 180)
        self.assertEqual({entry["name"] for entry in form.inputs}, set(self.answers()))
        self.assertTrue(all(entry["type"] == "radio" and "required" in entry for entry in form.inputs))
        self.assertFalse(any("checked" in entry for entry in form.inputs))
        self.assertEqual(form.scripts, [])
        for city_id, name, country in EXPECTED_CITIES:
            self.assertIn(f">{name}, {country}</h2>", response.get_data(as_text=True))
            for field, options in (("visited", ["yes", "no"]), ("familiarity", list("1234567"))):
                self.assertEqual([entry["value"] for entry in form.inputs if entry["name"] == f"{field}_{city_id}"], options)
        self.assertIn(b"past 3 years", response.data)

    def test_get_refresh_is_stable_and_stores_no_city_selection(self):
        self.begin_screening()
        self.assertEqual(self.client.get("/travel-cities").data, self.client.get("/travel-cities").data)
        with self.client.session_transaction() as browser_session:
            self.assertEqual(dict(browser_session), {"consent_accepted": True, "travel_intro_seen": True})

    def test_consent_and_intro_are_required_for_get_post_and_all_gates(self):
        for method, path in (("GET", "/travel-intro"), ("POST", "/travel-intro"), ("GET", "/travel-cities"), ("POST", "/travel-cities")):
            self.assert_redirect(self.client.open(path, method=method, data=self.answers()), "/consent")
        self.assert_all_gates_blocked("/consent")
        self.accept_consent()
        for method in ("GET", "POST"):
            self.assert_redirect(self.client.open("/travel-cities", method=method, data=self.answers()), "/travel-intro")
        self.assert_all_gates_blocked("/travel-intro")
        self.assertEqual(self.client.get("/travel-intro").status_code, 200)
        self.assert_redirect(self.client.get("/travel-cities"), "/travel-intro")

    def test_yes_count_boundaries_and_no_persisted_answers(self):
        for yes_count in (0, 4, 5, 15, 16, 20):
            with self.subTest(yes_count=yes_count):
                self.client = server.app.test_client()
                self.begin_screening()
                eligible = 5 <= yes_count <= 15
                response = self.client.post("/travel-cities", data=self.answers(yes_count))
                self.assert_redirect(response, "/instructions" if eligible else "/screened-out-cities")
                with self.client.session_transaction() as browser_session:
                    expected = {"consent_accepted": True, "travel_intro_seen": True}
                    expected.update({"travel_screening_complete": server.SCREENING_VERSION} if eligible else {"travel_screened_out": True})
                    self.assertEqual(dict(browser_session), expected)
                if eligible:
                    self.assert_all_gates_blocked("/instructions")
                else:
                    self.assert_all_gates_blocked("/screened-out-cities")

    def test_every_familiarity_rating_is_valid_and_does_not_change_eligibility(self):
        for rating in map(str, range(1, 8)):
            for yes_count in (4, 5, 15, 16):
                with self.subTest(rating=rating, yes_count=yes_count):
                    self.client = server.app.test_client()
                    self.begin_screening()
                    self.assert_redirect(self.client.post("/travel-cities", data=self.answers(yes_count, rating)), "/instructions" if 5 <= yes_count <= 15 else "/screened-out-cities")

    def test_invalid_or_missing_fields_return_400_preserve_selections_and_do_not_screen_out(self):
        for field in ("visited", "familiarity"):
            for invalid in (None, "", "8", "invalid"):
                for yes_count in (0, 5, 20):
                    with self.subTest(field=field, invalid=invalid, yes_count=yes_count):
                        self.client = server.app.test_client()
                        self.begin_screening()
                        data = self.answers(yes_count)
                        key = f"{field}_westminster"
                        if invalid is None:
                            del data[key]
                        else:
                            data[key] = invalid
                        response = self.client.post("/travel-cities", data=data)
                        self.assertEqual(response.status_code, 400)
                        checked = {entry["name"]: entry["value"] for entry in FormParser(response).inputs if "checked" in entry}
                        self.assertEqual(checked, {name: value for name, value in data.items() if name != key})
                        with self.client.session_transaction() as browser_session:
                            self.assertEqual(dict(browser_session), {"consent_accepted": True, "travel_intro_seen": True})
                        self.assert_all_gates_blocked("/travel-intro")
                        self.assert_redirect(self.client.post("/travel-cities", data=self.answers()), "/instructions")

    def test_all_missing_fields_report_errors_before_eligibility(self):
        self.begin_screening()
        response = self.client.post("/travel-cities", data={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.count(b"Please choose Yes or No."), 20)
        self.assertEqual(response.data.count(b"Please choose a rating from 1 to 7."), 20)
        self.assertEqual(self.client.get("/travel-cities").status_code, 200)

    def test_duplicate_field_values_are_rejected(self):
        self.begin_screening()
        for key in ("visited_liverpool", "familiarity_liverpool"):
            data = MultiDict(self.answers())
            data.add(key, data[key])
            self.assertEqual(self.client.post("/travel-cities", data=data).status_code, 400)

    def test_screen_out_survives_return_and_resubmission(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers(16))
        response = self.client.get("/screened-out-cities")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"you can't take part in this study", response.data)
        self.assert_redirect(self.client.get("/start"), "/consent")
        self.assert_redirect(self.client.get("/consent"), "/travel-intro")
        for decision in ("accept", "deny", "accept"):
            self.client.post("/consent", data={"decision": decision})
        for method, path in (("GET", "/travel-intro"), ("POST", "/travel-intro"), ("GET", "/travel-cities"), ("POST", "/travel-cities")):
            self.assert_redirect(self.client.open(path, method=method, data=self.answers()), "/screened-out-cities")
        self.assert_all_gates_blocked("/screened-out-cities")

    def test_old_completion_flags_cannot_bypass_new_screening(self):
        for marker in (True, "old-version"):
            with self.subTest(marker=marker):
                self.client = server.app.test_client()
                with self.client.session_transaction() as browser_session:
                    browser_session.update(consent_accepted=True, travel_intro_seen=True, travel_screening_complete=marker, other_cities_complete=True, other_city_ids=["paris"], instructions_complete=True)
                self.assert_all_gates_blocked("/travel-intro")
                self.assertEqual(self.client.get("/travel-intro").status_code, 200)
                self.assertEqual(self.client.get("/travel-cities").status_code, 200)
                self.assert_redirect(self.client.post("/travel-cities", data=self.answers()), "/instructions")
                with self.client.session_transaction() as browser_session:
                    self.assertEqual(dict(browser_session), {"consent_accepted": True, "travel_intro_seen": True, "travel_screening_complete": server.SCREENING_VERSION})
                self.assert_all_gates_blocked("/instructions")

    def test_current_completion_still_requires_intro_and_screen_out_takes_precedence(self):
        self.accept_consent()
        with self.client.session_transaction() as browser_session:
            browser_session["travel_screening_complete"] = server.SCREENING_VERSION
        self.assert_all_gates_blocked("/travel-intro")
        with self.client.session_transaction() as browser_session:
            browser_session["travel_intro_seen"] = True
            browser_session["travel_screened_out"] = True
        self.assert_all_gates_blocked("/screened-out-cities")

    def test_completed_screening_returns_to_instructions_without_resubmitting(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers())
        self.assert_redirect(self.client.get("/consent"), "/instructions")
        self.accept_consent("/instructions")
        for method, path in (("GET", "/travel-intro"), ("POST", "/travel-intro"), ("GET", "/travel-cities"), ("POST", "/travel-cities")):
            self.assert_redirect(self.client.open(path, method=method, data=self.answers(0)), "/instructions")
        self.assert_all_gates_blocked("/instructions")

    def test_instructions_get_and_post_require_consent_and_current_screening(self):
        for state, destination in (
            ({}, "/consent"),
            ({"consent_accepted": True}, "/travel-intro"),
            ({"consent_accepted": True, "travel_intro_seen": True}, "/travel-intro"),
            ({"consent_accepted": True, "travel_intro_seen": True, "travel_screening_complete": "old-version"}, "/travel-intro"),
            ({"consent_accepted": True, "travel_screening_complete": server.SCREENING_VERSION}, "/travel-intro"),
            ({"travel_intro_seen": True, "travel_screening_complete": server.SCREENING_VERSION}, "/consent"),
        ):
            for method in ("GET", "POST"):
                with self.subTest(state=state, method=method):
                    self.client = server.app.test_client()
                    with self.client.session_transaction() as browser_session:
                        browser_session.update(state)
                    self.assert_redirect(self.client.open("/instructions", method=method), destination)
                    with self.client.session_transaction() as browser_session:
                        self.assertEqual(dict(browser_session), state)

    def test_instructions_get_is_shared_explanation_and_does_not_grant_access(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers())
        with self.client.session_transaction() as browser_session:
            before = dict(browser_session)
        response = self.client.get("/instructions")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<h1 id="instructions-title">Instructions</h1>', response.data)
        self.assertIn(b"In the next part of this study, you will be matched with another human participant for a text conversation. After you have been paired, you will receive instructions for your assigned role.", response.data)
        self.assertIn(b"Find a partner</button>", response.data)
        form = FormParser(response)
        self.assertEqual(form.forms, [{"method": "post", "action": "/instructions"}])
        self.assertEqual(form.buttons, [{"class": "button travel-primary", "type": "submit"}])
        self.assertEqual(form.scripts, [])
        self.assertEqual(self.client.get("/instructions").data, response.data)
        with self.client.session_transaction() as browser_session:
            self.assertEqual(dict(browser_session), before)
        self.assert_all_gates_blocked("/instructions")

    def test_instructions_post_grants_matching_but_not_dialogue_access(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers())
        self.assert_redirect(self.client.post("/instructions"), "/room")
        with self.client.session_transaction() as browser_session:
            self.assertIs(browser_session["instructions_complete"], True)
        self.assertEqual(self.client.get("/room").status_code, 200)
        self.assert_redirect(self.client.get("/dialogue"), "/room")
        self.assertEqual(self.client.post("/api/rooms", json={"participant_id": "test"}).status_code, 200)
        self.assertEqual(self.client.post("/api/rooms", json={"participant_id": "partner"}).status_code, 403)
        self.assertEqual(self.client.get("/api/rooms/ROOM01?participant_id=test").status_code, 200)
        self.assertEqual(self.client.post("/api/rooms/ROOM01/messages", json={"participant_id": "test", "text": "Hello"}).status_code, 403)
        self.assertEqual(self.client.get("/api/rooms/ROOM01/messages?participant_id=test").status_code, 403)
        self.assertEqual(self.client.post("/api/rooms/ROOM01/typing", json={"participant_id": "test"}).status_code, 403)
        self.assert_redirect(self.client.get("/instructions"), "/room")
        self.assert_redirect(self.client.post("/instructions"), "/room")

    def test_return_and_renewed_consent_retain_instructions_progress(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers())
        self.client.post("/instructions")
        self.assert_redirect(self.client.get("/start"), "/consent")
        self.assert_redirect(self.client.get("/consent"), "/instructions")
        for decision in ("accept", "deny", "accept"):
            response = self.client.post("/consent", data={"decision": decision})
            if decision == "deny":
                self.assertEqual(response.status_code, 200)
                self.assert_redirect(self.client.post("/instructions"), "/consent")
            else:
                self.assert_redirect(response, "/instructions")
            with self.client.session_transaction() as browser_session:
                self.assertIs(browser_session["instructions_complete"], True)
        for method, path in (("GET", "/travel-intro"), ("POST", "/travel-intro"), ("GET", "/travel-cities"), ("POST", "/travel-cities")):
            self.assert_redirect(self.client.open(path, method=method, data=self.answers(0)), "/instructions")
        self.assert_redirect(self.client.get("/instructions"), "/room")
        self.assertEqual(self.client.post("/api/rooms", json={"participant_id": "test"}).status_code, 200)

    def test_new_screening_outcome_invalidates_obsolete_instructions_progress(self):
        for yes_count in (4, 5):
            with self.subTest(yes_count=yes_count):
                self.client = server.app.test_client()
                self.begin_screening()
                with self.client.session_transaction() as browser_session:
                    browser_session["instructions_complete"] = True
                destination = "/instructions" if yes_count == 5 else "/screened-out-cities"
                self.assert_redirect(self.client.post("/travel-cities", data=self.answers(yes_count)), destination)
                with self.client.session_transaction() as browser_session:
                    self.assertNotIn("instructions_complete", browser_session)
                self.assert_all_gates_blocked(destination)

    def test_screenout_blocks_instructions_even_with_obsolete_completion_flags(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers(4))
        for stale_progress in (False, True):
            with self.subTest(stale_progress=stale_progress):
                if stale_progress:
                    with self.client.session_transaction() as browser_session:
                        browser_session.update(travel_screening_complete=server.SCREENING_VERSION, instructions_complete=True)
                for method in ("GET", "POST"):
                    self.assert_redirect(self.client.open("/instructions", method=method), "/screened-out-cities")
                with self.client.session_transaction() as browser_session:
                    self.assertEqual(browser_session.get("instructions_complete"), True if stale_progress else None)
                self.assert_all_gates_blocked("/screened-out-cities")

    def test_instructions_flag_must_be_boolean_true(self):
        self.begin_screening()
        self.client.post("/travel-cities", data=self.answers())
        for marker in (False, "true", 1):
            with self.subTest(marker=marker):
                with self.client.session_transaction() as browser_session:
                    browser_session["instructions_complete"] = marker
                self.assertEqual(self.client.get("/instructions").status_code, 200)
                self.assert_all_gates_blocked("/instructions")

    def test_obsolete_routes_are_absent_and_screenout_page_is_gated(self):
        for path in ("/other-cities", "/travel-cities/unable"):
            for method in ("GET", "POST"):
                self.assertEqual(self.client.open(path, method=method).status_code, 404)
        self.assert_redirect(self.client.get("/screened-out-cities"), "/consent")
        self.accept_consent()
        self.assert_redirect(self.client.get("/screened-out-cities"), "/travel-intro")


if __name__ == "__main__":
    unittest.main()
