from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import server
from screening_fixtures import retain_eligible_submission


class RoomMatchingTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(server.app.config, TURNSTILE_ENABLED=False)
        config.start()
        self.addCleanup(config.stop)
        self.clients = {}
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()

    def tearDown(self):
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()

    def client(self):
        client = server.app.test_client()
        retain_eligible_submission(client)
        with client.session_transaction() as browser_session:
            browser_session["consent_accepted"] = True
            browser_session["travel_intro_seen"] = True
            browser_session["travel_screening_complete"] = server.SCREENING_VERSION
            browser_session["instructions_complete"] = True
        return client

    def match(self, participant_id):
        if participant_id not in self.clients:
            self.clients[participant_id] = self.client()
        return self.clients[participant_id].post("/api/rooms", json={"participant_id": participant_id})

    def test_consecutive_people_share_room_and_receive_opposite_roles(self):
        first = self.match("first").get_json()
        second = self.match("second").get_json()
        third = self.match("third").get_json()
        self.assertEqual(first, {"code": "ROOM01", "status": "waiting"})
        self.assertEqual(second["code"], "ROOM01")
        self.assertEqual(second["status"], "paired")
        status = self.clients["first"].get("/api/rooms/ROOM01?participant_id=first").get_json()
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

    def test_configured_capacity_with_no_recycling(self):
        for index in range(2 * len(server.ROOM_CODES)):
            self.assertEqual(self.match(f"person-{index}").status_code, 200)
        overflow = self.match("overflow")
        self.assertEqual(overflow.status_code, 409)
        self.assertEqual(overflow.get_json()["error"], "All rooms are currently occupied.")
        self.assertEqual(len(server.rooms), len(server.ROOM_CODES))
        self.assertEqual(self.match("person-0").status_code, 200)

    def test_concurrent_arrivals_never_overfill_or_duplicate_memberships(self):
        ids = [f"person-{index}" for index in range(2 * len(server.ROOM_CODES))]
        self.clients = {pid: self.client() for pid in ids}
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(self.match, ids))
            responses += list(executor.map(self.match, ids))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(server.rooms), len(server.ROOM_CODES))
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

    def test_message_timestamp_is_server_generated_and_not_exposed(self):
        client = self.client()
        partner = self.client()
        client.post("/api/rooms", json={"participant_id": "first"})
        partner.post("/api/rooms", json={"participant_id": "second"})
        client.post("/role-instructions")
        partner.post("/role-instructions")
        self.assertEqual(client.get("/dialogue").status_code, 200)

        sent = client.post(
            "/api/rooms/ROOM01/messages",
            json={"participant_id": "first", "text": "Hello", "timestamp": "client value"},
        )
        self.assertEqual(sent.status_code, 201)
        self.assertNotIn("timestamp", sent.get_json()["message"])

        stored_message = server.rooms["ROOM01"]["messages"][-1]
        timestamp = datetime.fromisoformat(stored_message["timestamp"])
        self.assertEqual(timestamp.tzinfo, timezone.utc)
        self.assertNotEqual(stored_message["timestamp"], "client value")

        received = client.get(
            "/api/rooms/ROOM01/messages?participant_id=first&after_id=0"
        ).get_json()
        sent_message = next(message for message in received["messages"] if message["text"] == "Hello")
        self.assertNotIn("timestamp", sent_message)


if __name__ == "__main__":
    unittest.main()