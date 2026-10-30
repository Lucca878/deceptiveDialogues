from concurrent.futures import ThreadPoolExecutor
import unittest

import server


class RoomMatchingTests(unittest.TestCase):
    def setUp(self):
        with server.rooms_lock:
            server.rooms.clear()

    def tearDown(self):
        with server.rooms_lock:
            server.rooms.clear()

    def client(self):
        client = server.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["consent_accepted"] = True
            browser_session["travel_screening_complete"] = True
            browser_session["other_cities_complete"] = True
        return client

    def match(self, participant_id):
        return self.client().post("/api/rooms", json={"participant_id": participant_id})

    def test_consecutive_people_share_room_and_receive_opposite_roles(self):
        first = self.match("first").get_json()
        second = self.match("second").get_json()
        third = self.match("third").get_json()
        self.assertEqual(first, {"code": "ROOM01", "status": "waiting"})
        self.assertEqual(second["code"], "ROOM01")
        self.assertEqual(second["status"], "paired")
        status = self.client().get("/api/rooms/ROOM01?participant_id=first").get_json()
        self.assertEqual({status["role"], second["role"]}, {"interviewer", "interviewee"})
        self.assertEqual(third, {"code": "ROOM02", "status": "waiting"})

    def test_refresh_and_retry_keep_existing_membership(self):
        first = self.match("first").get_json()
        self.assertEqual(self.match("first").get_json(), first)
        self.match("second")
        paired = self.match("first").get_json()
        self.assertEqual(paired["status"], "paired")
        self.assertEqual(self.match("first").get_json(), paired)
        self.assertEqual(len(server.rooms), 1)

    def test_capacity_is_twenty_people_with_no_recycling(self):
        for index in range(20):
            self.assertEqual(self.match(f"person-{index}").status_code, 200)
        overflow = self.match("overflow")
        self.assertEqual(overflow.status_code, 409)
        self.assertEqual(overflow.get_json()["error"], "All rooms are currently occupied.")
        self.assertEqual(len(server.rooms), 10)
        self.assertEqual(self.match("person-0").status_code, 200)

    def test_concurrent_arrivals_never_overfill_or_duplicate_memberships(self):
        ids = [f"person-{index}" for index in range(20)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(self.match, ids + ids))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(server.rooms), 10)
        memberships = []
        for room in server.rooms.values():
            self.assertEqual(len(room["participants"]), 2)
            self.assertEqual(set(room["participants"].values()), {"interviewer", "interviewee"})
            memberships.extend(room["participants"])
        self.assertCountEqual(memberships, ids)

    def test_manual_join_removed_and_inputs_guarded(self):
        self.assertEqual(self.client().post("/api/rooms/ROOM01/join", json={"participant_id": "p"}).status_code, 404)
        self.assertEqual(self.client().post("/api/rooms", json={}).status_code, 400)
        self.assertEqual(server.app.test_client().post("/api/rooms", json={"participant_id": "p"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()