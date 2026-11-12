from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import server


class RoleReadinessTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(server.app.config, TURNSTILE_ENABLED=False)
        config.start()
        self.addCleanup(config.stop)
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()
        self.first = self.eligible_client()
        self.second = self.eligible_client()

    def tearDown(self):
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()

    def eligible_client(self):
        """Use separate real consent, screening and pre-match sessions."""
        client = server.app.test_client()
        self.assertEqual(client.post("/consent", data={"decision": "accept"}).status_code, 302)
        client.post("/travel-intro")
        answers = {}
        for index, (city_id, _, _) in enumerate(server.CANDIDATE_CITIES):
            answers[f"visited_{city_id}"] = "yes" if index < 5 else "no"
            answers[f"familiarity_{city_id}"] = "4"
        self.assertEqual(client.post("/travel-cities", data=answers).location, "/instructions")
        self.assertEqual(client.post("/instructions").location, "/room")
        return client

    def pair(self):
        self.assertEqual(self.first.post("/api/rooms", json={"participant_id": "first"}).get_json()["status"], "waiting")
        self.assertEqual(self.second.post("/api/rooms", json={"participant_id": "second"}).get_json()["status"], "paired")

    def status(self, client):
        response = client.get("/api/rooms/ROOM01")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def assert_chat_blocked(self, client, participant_id):
        for method, suffix in (("GET", "/messages"), ("POST", "/messages"), ("POST", "/typing")):
            with self.subTest(method=method, suffix=suffix, participant_id=participant_id):
                response = client.open(
                    f"/api/rooms/ROOM01{suffix}?participant_id={participant_id}",
                    method=method, json={"participant_id": participant_id, "text": "Hello"},
                )
                self.assertEqual(response.status_code, 403)
                self.assertIn("error", response.get_json())

    def test_full_lifecycle_private_instructions_and_both_ready(self):
        self.assertEqual(self.first.get("/dialogue").location, "/room")
        self.assertEqual(self.first.get("/role-instructions").location, "/room")
        self.first.post("/role-instructions")
        self.first.post("/api/rooms", json={"participant_id": "first"})
        self.assertEqual(self.first.get("/role-instructions").location, "/room")
        self.assertEqual(server.rooms["ROOM01"]["ready"], set())
        self.second.post("/api/rooms", json={"participant_id": "second"})
        for client, pid, other_pid in ((self.first, "first", "second"), (self.second, "second", "first")):
            status = self.status(client)
            self.assertEqual(set(status), {"code", "status", "role", "chat_ready"})
            self.assertFalse(status["chat_ready"])
            role = status["role"]
            other_role = "interviewee" if role == "interviewer" else "interviewer"
            response = client.get(f"/role-instructions?role={other_role}&chat-role={other_role}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            private_text = {
                "interviewer": b"Your task is to decide whether their claim about visiting a city is true.",
                "interviewee": b"Your task is to convince them that your claim about visiting a city is true.",
            }
            self.assertIn(private_text[role], response.data)
            self.assertNotIn(private_text[other_role], response.data)
            self.assertNotIn(other_pid.encode(), response.data)
            self.assertIn(b"I'm ready", response.data)
            self.assertEqual(client.get("/dialogue").location, "/role-instructions")
            self.assert_chat_blocked(client, pid)
        self.assertEqual(server.rooms["ROOM01"]["ready"], set())

        self.assertEqual(self.first.post("/role-instructions").location, "/role-instructions")
        for _ in range(2):
            response = self.first.get("/role-instructions")
            self.assertIn(b"Waiting for your partner. Please keep this page open.", response.data)
            self.assertNotIn(b"I'm ready", response.data)
            self.first.post("/role-instructions")
            self.first.post("/api/rooms", json={"participant_id": "first"})
            self.first.get("/room")
            self.assertEqual(server.rooms["ROOM01"]["ready"], {"first"})
        self.assertFalse(self.status(self.first)["chat_ready"])
        self.assertFalse(self.status(self.second)["chat_ready"])
        self.assert_chat_blocked(self.first, "first")
        self.assert_chat_blocked(self.second, "second")

        self.second.post("/role-instructions")
        for client, pid in ((self.first, "first"), (self.second, "second")):
            self.assertTrue(self.status(client)["chat_ready"])
            self.assertEqual(client.get("/role-instructions").location, "/dialogue")
            self.assertEqual(client.get("/dialogue").status_code, 200)
            self.assertEqual(client.post("/api/rooms/ROOM01/messages", json={"participant_id": pid, "text": "Hello", "role": "spoof"}).status_code, 201)
            self.assertEqual(client.get(f"/api/rooms/ROOM01/messages?participant_id={pid}").status_code, 200)
            self.assertEqual(client.post("/api/rooms/ROOM01/typing", json={"participant_id": pid}).status_code, 200)
        self.assertEqual(server.rooms["ROOM01"]["messages"][0]["role"], self.status(self.first)["role"])

    def test_swapped_ids_and_new_session_cannot_claim_membership(self):
        self.pair()
        outsider = self.eligible_client()
        self.assertEqual(outsider.post("/api/rooms", json={"participant_id": "first"}).status_code, 403)
        self.assertEqual(self.first.post("/api/rooms", json={"participant_id": "second"}).status_code, 403)
        self.assertEqual(self.first.post("/api/rooms", json={"participant_id": "new-id"}).status_code, 403)
        for client, claimed in ((self.first, "second"), (self.second, "first"), (outsider, "first")):
            self.assertEqual(client.get(f"/api/rooms/ROOM01?participant_id={claimed}").status_code, 403)
            self.assertEqual(client.get(f"/role-instructions?participant_id={claimed}").status_code, 403)
            self.assertEqual(client.post("/role-instructions", data={"participant_id": claimed}).status_code, 403)
            self.assertEqual(client.post("/role-instructions", json={"participant_id": claimed}).status_code, 403)
            self.assert_chat_blocked(client, claimed)
        self.assertEqual(server.rooms["ROOM01"]["ready"], set())
        self.first.post("/role-instructions")
        self.second.post("/role-instructions")
        self.assert_chat_blocked(self.first, "second")
        self.assert_chat_blocked(self.second, "first")
        self.assert_chat_blocked(outsider, "first")
        self.assertEqual(outsider.get("/dialogue").location, "/room")

    def test_membership_binding_and_expired_room_are_checked(self):
        self.pair()
        for key, value in (("room_token", "wrong-token"), ("room_participant_id", "second"), ("room_code", "ROOM02")):
            with self.first.session_transaction() as browser_session:
                original = browser_session[key]
                browser_session[key] = value
            self.assertEqual(self.first.get("/role-instructions").location, "/room")
            self.assertEqual(self.first.get("/dialogue").location, "/room")
            self.assertEqual(self.first.get("/api/rooms/ROOM01").status_code, 403)
            self.assert_chat_blocked(self.first, "first")
            with self.first.session_transaction() as browser_session:
                browser_session[key] = original
        with server.rooms_lock:
            server.rooms.clear()
        self.assertEqual(self.first.get("/role-instructions").location, "/room")
        self.assertEqual(self.first.post("/api/rooms", json={"participant_id": "first"}).status_code, 200)
        self.assertEqual(server.rooms["ROOM01"]["ready"], set())

    def test_concurrent_ready_is_idempotent(self):
        self.pair()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda client: client.post("/role-instructions"), (self.first, self.second)))
        self.assertTrue(all(response.status_code == 302 for response in responses))
        self.first.post("/role-instructions")
        self.second.post("/role-instructions")
        self.assertEqual(server.rooms["ROOM01"]["ready"], {"first", "second"})
        self.assertTrue(self.status(self.first)["chat_ready"])


if __name__ == "__main__":
    unittest.main()