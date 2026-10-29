from datetime import date
import unittest

import server


class TravelScreeningTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def accept_consent(self):
        response = self.client.post("/consent", data={"decision": "accept"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/travel-intro"))

    def begin_screening(self):
        self.accept_consent()
        response = self.client.post("/travel-intro")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/travel-cities"))

    def valid_answers(self):
        start_month, _, _ = server._window_months()
        year, month = start_month.split("-")
        answers = {}
        for index in range(1, server.CITY_COUNT + 1):
            answers.update(
                {
                    f"city_{index}": f"City {index}",
                    f"country_{index}": "Netherlands",
                    f"visit_year_{index}": year,
                    f"visit_month_number_{index}": month,
                    f"duration_{index}": "2_3_days",
                    f"reason_{index}": "holiday",
                    f"companions_{index}": "friends",
                    f"familiarity_{index}": "4",
                }
            )
        return answers

    def test_window_is_36_months_and_month_based(self):
        self.assertEqual(
            server._window_months(date(2026, 10, 29)),
            ("2023-10", "2026-10", "October 2023"),
        )

    def test_room_and_api_require_completed_screening(self):
        self.accept_consent()
        self.assertTrue(self.client.get("/room").headers["Location"].endswith("/travel-intro"))
        self.assertEqual(
            self.client.post("/api/rooms", json={"participant_id": "participant-1"}).status_code,
            403,
        )

    def test_intro_must_be_seen_before_questionnaire(self):
        self.accept_consent()
        response = self.client.get("/travel-cities")
        self.assertTrue(response.headers["Location"].endswith("/travel-intro"))

    def test_visit_date_chooses_year_then_month(self):
        self.begin_screening()
        response = self.client.get("/travel-cities")
        self.assertIn(b'name="visit_year_1"', response.data)
        self.assertIn(b'name="visit_month_number_1"', response.data)
        self.assertIn(b">Year<", response.data)
        self.assertIn(b">Month<", response.data)

    def test_valid_answers_continue_without_retaining_responses(self):
        self.begin_screening()
        response = self.client.post("/travel-cities", data=self.valid_answers())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/room"))
        self.assertEqual(self.client.get("/room").status_code, 200)
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session["travel_screening_complete"])
            self.assertNotIn("City 1", repr(dict(browser_session)))

    def test_duplicate_cities_are_rejected_after_normalization(self):
        self.begin_screening()
        answers = self.valid_answers()
        answers["city_2"] = "  CITY   1  "
        response = self.client.post("/travel-cities", data=answers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"different city and country", response.data)

    def test_same_city_name_in_different_countries_is_distinct(self):
        self.begin_screening()
        answers = self.valid_answers()
        answers["city_2"] = answers["city_1"]
        answers["country_2"] = "Canada"
        self.assertEqual(self.client.post("/travel-cities", data=answers).status_code, 302)

    def test_visit_outside_window_is_rejected(self):
        self.begin_screening()
        answers = self.valid_answers()
        answers["visit_year_1"] = "2023"
        answers["visit_month_number_1"] = "09"
        response = self.client.post("/travel-cities", data=answers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"within the date window", response.data)

    def test_future_visit_is_rejected(self):
        self.begin_screening()
        answers = self.valid_answers()
        _, current_month, _ = server._window_months()
        year, month = map(int, current_month.split("-"))
        next_month = date(year + (month == 12), month % 12 + 1, 1).strftime("%Y-%m")
        answers["visit_year_1"], answers["visit_month_number_1"] = next_month.split("-")
        response = self.client.post("/travel-cities", data=answers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"within the date window", response.data)

    def test_invalid_country_and_missing_field_are_rejected(self):
        self.begin_screening()
        answers = self.valid_answers()
        answers["country_1"] = "Atlantis"
        answers["reason_2"] = ""
        response = self.client.post("/travel-cities", data=answers)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"choose a country from the list", response.data)
        self.assertIn(b"Please complete this field", response.data)

    def test_screen_out_blocks_room_and_apis(self):
        self.begin_screening()
        response = self.client.get("/travel-cities/unable")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"you can't take part in this study", response.data)
        self.assertEqual(self.client.get("/room").status_code, 302)
        self.assertEqual(
            self.client.post("/api/rooms", json={"participant_id": "participant-1"}).status_code,
            403,
        )

    def test_new_start_clears_screening_and_consent(self):
        self.begin_screening()
        self.client.get("/travel-cities/unable")
        response = self.client.get("/start")
        self.assertTrue(response.headers["Location"].endswith("/consent"))
        self.assertTrue(self.client.get("/room").headers["Location"].endswith("/consent"))


if __name__ == "__main__":
    unittest.main()
