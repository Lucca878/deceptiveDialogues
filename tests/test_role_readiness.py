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

    def test_final_continue_routes_both_participants_to_feedback(self):
        self.pair()
        room = server.rooms["ROOM01"]
        room["ready"].update(room["participants"])
        clients = {"first": self.first, "second": self.second}
        interviewer_id = next(pid for pid, role in room["participants"].items() if role == "interviewer")
        interviewee_id = next(pid for pid, role in room["participants"].items() if role == "interviewee")
        for client in clients.values():
            self.assertEqual(client.get("/dialogue").status_code, 200)
        self.assertEqual(self.first.get("/feedback").location, "/dialogue")
        room["current_claim_index"] = 9
        room["current_claim"]["dialogue_id"] = 10
        room["current_claim"]["question_turns_ended"] = True
        room["current_claim"]["judgement"] = {"judgement": "true", "confidence": 80}

        with patch("server.monotonic", side_effect=[100.0, 101.0, 101.0, 104.0, 104.0, 104.0, 104.0]):
            continued = clients[interviewer_id].post(
                "/api/rooms/ROOM01/continue",
                json={"participant_id": interviewer_id},
            )
            self.assertEqual(continued.status_code, 201)
            self.assertEqual(room["current_claim_index"], 9)
            self.assertIsNotNone(room["current_claim"])

            countdown = clients[interviewee_id].get(
                f"/api/rooms/ROOM01/messages?participant_id={interviewee_id}"
            ).get_json()
            self.assertEqual(countdown["seconds_remaining"], 2.0)
            self.assertEqual(countdown["dialogue_number"], 10)
            self.assertFalse(countdown["transition_complete"])
            self.assertEqual(room["current_claim_index"], 9)
            interviewer_countdown = clients[interviewer_id].get(
                f"/api/rooms/ROOM01/messages?participant_id={interviewer_id}"
            ).get_json()
            self.assertEqual(interviewer_countdown["seconds_remaining"], countdown["seconds_remaining"])
            self.assertEqual(interviewer_countdown["dialogue_number"], countdown["dialogue_number"])

            for participant_id, client in clients.items():
                with self.subTest(participant_id=participant_id):
                    poll = client.get(f"/api/rooms/ROOM01/messages?participant_id={participant_id}")
                    self.assertTrue(poll.get_json()["dialogue_complete"])
                    self.assertTrue(poll.get_json()["transition_complete"])
                    self.assertIsNone(poll.get_json()["dialogue_number"])
                    self.assertEqual(room["current_claim_index"], 10)
                    self.assertIsNone(room["current_claim"])
                    self.assertEqual(client.get("/dialogue").location, "/feedback")
                    feedback = client.get("/feedback")
                    self.assertEqual(feedback.status_code, 200)
                    self.assertIn(b"Thank you for completing all the dialogues.", feedback.data)
        self.assertNotIn("transition_pending", room)
        self.assertNotIn("transition_end", room)

    def test_message_poll_only_returns_messages_from_active_dialogue(self):
        self.pair()
        room = server.rooms["ROOM01"]
        room["ready"].update(room["participants"])
        for client in (self.first, self.second):
            self.assertEqual(client.get("/dialogue").status_code, 200)
        interviewer_id = next(pid for pid, role in room["participants"].items() if role == "interviewer")
        interviewer_client = self.first if interviewer_id == "first" else self.second
        room["messages"].append({
            "id": len(room["messages"]) + 1,
            "participant_id": interviewer_id,
            "role": "interviewer",
            "text": "Previous dialogue question",
            "timestamp": "server timestamp",
            "is_claim": False,
        })

        # Simulate the server advancing to dialogue two while preserving room history.
        room["current_claim_index"] = 1
        room["current_claim"] = None
        room["next_speaker"] = None
        next_dialogue = interviewer_client.get("/dialogue")
        self.assertEqual(next_dialogue.status_code, 200)

        current_messages = interviewer_client.get(
            f"/api/rooms/ROOM01/messages?participant_id={interviewer_id}"
        ).get_json()["messages"]

        self.assertEqual(len(room["messages"]), 3)  # Keep both dialogues in the room's server-side history.
        self.assertEqual(len(current_messages), 1)  # Show only the active dialogue in this browser view.
        self.assertTrue(current_messages[0]["is_claim"])
        self.assertIn("I have visited ", current_messages[0]["text"])

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
            self.assertIn(b"Waiting for your partner", response.data)
            self.assertIn(b"Please keep this page open while you are waiting.", response.data)
            self.assertIn(b"role-instructions-card--waiting", response.data)
            self.assertIn(b'id="readiness-motion-toggle"', response.data)
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
            early_typing = client.post(
                "/api/rooms/ROOM01/typing",
                json={"participant_id": pid},
            )
            self.assertEqual(early_typing.status_code, 403)
            self.assertIn("opening claim", early_typing.get_json()["error"])
            early_message = client.post(
                "/api/rooms/ROOM01/messages",
                json={"participant_id": pid, "text": "Too early"},
            )
            self.assertEqual(early_message.status_code, 403)
            self.assertIn("opening claim", early_message.get_json()["error"])
        self.assertEqual(server.rooms["ROOM01"]["messages"], [])
        self.assertIsNone(server.rooms["ROOM01"]["current_claim"])
        self.assertEqual(server.rooms["ROOM01"]["typing"], {})

        clients_by_role = {}
        for client, pid in ((self.first, "first"), (self.second, "second")):
            status = self.status(client)
            self.assertTrue(status["chat_ready"])
            self.assertEqual(client.get("/role-instructions").location, "/dialogue")
            dialogue_response = client.get("/dialogue")
            self.assertEqual(dialogue_response.status_code, 200)
            self.assertIn(b'id="dialogue-counter"', dialogue_response.data)
            self.assertIn(b'id="dialogue-progress-track"', dialogue_response.data)
            self.assertIn(b'id="question-counter"', dialogue_response.data)
            self.assertIn(b'id="transition-countdown-label"', dialogue_response.data)
            self.assertIn(b'id="transition-countdown"', dialogue_response.data)
            self.assertIn(b'class="button chat-send"', dialogue_response.data)
            if status["role"] == "interviewee":
                self.assertIn(b'id="transition-maze"', dialogue_response.data)
                self.assertIn(b'class="maze-controls"', dialogue_response.data)
                self.assertIn(b'aria-label="9 by 9 maze board"', dialogue_response.data)
                self.assertIn(b"Exit: row 9, column 9.", dialogue_response.data)
                self.assertNotIn(b'id="judgement-form"', dialogue_response.data)
            else:
                self.assertNotIn(b'id="transition-maze"', dialogue_response.data)
                self.assertIn(b'class="chat-composer judgement-form"', dialogue_response.data)
                self.assertIn(b'id="judgement-truth-step"', dialogue_response.data)
                self.assertIn(b'id="judgement-truth-submit" type="button">Submit', dialogue_response.data)
                self.assertIn(b'id="judgement-confidence-step" hidden', dialogue_response.data)
                self.assertIn(b'aria-label="Submit confidence rating">Submit', dialogue_response.data)
                self.assertIn(b'id="judgement-status"', dialogue_response.data)
            clients_by_role[status["role"]] = (client, pid)

        interviewer, interviewer_id = clients_by_role["interviewer"]
        interviewee, interviewee_id = clients_by_role["interviewee"]
        room = server.rooms["ROOM01"]
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertEqual(len(room["messages"]), 1)
        # Check can_send is true only for the interviewer, then follows each accepted turn.
        interviewer_poll = interviewer.get("/api/rooms/ROOM01/messages?participant_id=" + interviewer_id).get_json()
        interviewee_poll = interviewee.get("/api/rooms/ROOM01/messages?participant_id=" + interviewee_id).get_json()
        self.assertIs(interviewer_poll["can_send"], True)
        self.assertIs(interviewee_poll["can_send"], False)
        self.assertEqual(interviewer_poll["question_number"], 1)
        self.assertEqual(interviewee_poll["question_number"], 1)
        # Both roles receive the same active dialogue number without private claim data.
        self.assertEqual(interviewer_poll["dialogue_number"], 1)
        self.assertEqual(interviewee_poll["dialogue_number"], 1)
        self.assertNotIn("truth_status", interviewer_poll)
        self.assertNotIn("truth_status", interviewee_poll)
        self.assertIs(interviewer_poll["question_turns_ended"], False)

        # After the claim appears, test that the interviewee's out-of-turn message is rejected.
        interviewee_attempt = interviewee.post(
            "/api/rooms/ROOM01/messages",
            json={"participant_id": interviewee_id, "text": "Answer too early"},
        )
        self.assertEqual(interviewee_attempt.status_code, 403)
        self.assertIn("not your turn", interviewee_attempt.get_json()["error"].lower())
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertEqual(len(room["messages"]), 1)
        # Check that the server rejects an out-of-turn typing ping without changing the turn.
        early_typing = interviewee.post(
            "/api/rooms/ROOM01/typing",
            json={"participant_id": interviewee_id},
        )
        self.assertEqual(early_typing.status_code, 403)
        self.assertIn("not your turn", early_typing.get_json()["error"].lower())
        self.assertEqual(room["typing"], {})
        self.assertEqual(room["next_speaker"], "interviewer")

        # Test that an accepted interviewer message makes the interviewee the next allowed speaker.
        interviewer_message = interviewer.post(
            "/api/rooms/ROOM01/messages",
            json={"participant_id": interviewer_id, "text": "Question 1?", "role": "spoof"},
        )
        self.assertEqual(interviewer_message.status_code, 201)
        self.assertEqual(interviewer_message.get_json()["message"]["role"], "interviewer")
        self.assertEqual(room["next_speaker"], "interviewee")
        self.assertEqual(len(room["messages"]), 2)
        interviewer_poll = interviewer.get(f"/api/rooms/ROOM01/messages?participant_id={interviewer_id}").get_json()
        interviewee_poll = interviewee.get(f"/api/rooms/ROOM01/messages?participant_id={interviewee_id}").get_json()
        self.assertIs(interviewer_poll["can_send"], False)
        self.assertIs(interviewee_poll["can_send"], True)
        self.assertEqual(interviewer_poll["question_number"], 1)
        self.assertEqual(interviewee_poll["question_number"], 1)

        # Test that an out-of-turn rejection does not change whose turn it is.
        rejected_repeat = interviewer.post(
            "/api/rooms/ROOM01/messages",
            json={"participant_id": interviewer_id, "text": "Another question too soon"},
        )
        self.assertEqual(rejected_repeat.status_code, 403)
        self.assertIn("not your turn", rejected_repeat.get_json()["error"].lower())
        self.assertEqual(room["next_speaker"], "interviewee")
        self.assertEqual(len(room["messages"]), 2)

        interviewee_message = interviewee.post(
            "/api/rooms/ROOM01/messages",
            json={"participant_id": interviewee_id, "text": "Answer 1."},
        )
        self.assertEqual(interviewee_message.status_code, 201)
        self.assertEqual(interviewee_message.get_json()["message"]["role"], "interviewee")
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertEqual(len(room["messages"]), 3)
        interviewer_poll = interviewer.get(f"/api/rooms/ROOM01/messages?participant_id={interviewer_id}").get_json()
        interviewee_poll = interviewee.get(f"/api/rooms/ROOM01/messages?participant_id={interviewee_id}").get_json()
        self.assertIs(interviewer_poll["can_send"], True)
        self.assertIs(interviewee_poll["can_send"], False)
        self.assertEqual(interviewer_poll["question_number"], 2)
        self.assertEqual(interviewee_poll["question_number"], 2)

        for question_number in range(2, 11):
            question = interviewer.post(
                "/api/rooms/ROOM01/messages",
                json={"participant_id": interviewer_id, "text": f"Question {question_number}?"},
            )
            self.assertEqual(question.status_code, 201)
            self.assertEqual(room["current_claim"]["interviewer_question_count"], question_number)
            during_answer_turn = interviewee.get(
                f"/api/rooms/ROOM01/messages?participant_id={interviewee_id}"
            ).get_json()
            self.assertEqual(during_answer_turn["question_number"], question_number)
            self.assertIs(during_answer_turn["can_send"], True)

            answer = interviewee.post(
                "/api/rooms/ROOM01/messages",
                json={"participant_id": interviewee_id, "text": f"Answer {question_number}."},
            )
            self.assertEqual(answer.status_code, 201)
            if question_number < 10:
                next_question_turn = interviewer.get(
                    f"/api/rooms/ROOM01/messages?participant_id={interviewer_id}"
                ).get_json()
                self.assertEqual(next_question_turn["question_number"], question_number + 1)
                self.assertIs(next_question_turn["can_send"], True)

        self.assertTrue(room["current_claim"]["question_turns_ended"])
        self.assertIsNone(room["next_speaker"])
        for client, pid in ((interviewer, interviewer_id), (interviewee, interviewee_id)):
            finished_poll = client.get(f"/api/rooms/ROOM01/messages?participant_id={pid}").get_json()
            self.assertEqual(finished_poll["question_number"], 10)
            self.assertIs(finished_poll["question_turns_ended"], True)
            self.assertIs(finished_poll["can_send"], False)
            extra_message = client.post(
                "/api/rooms/ROOM01/messages",
                json={"participant_id": pid, "text": "Extra message"},
            )
            self.assertEqual(extra_message.status_code, 403)
            extra_typing = client.post("/api/rooms/ROOM01/typing", json={"participant_id": pid})
            self.assertEqual(extra_typing.status_code, 403)

        for client, pid in ((self.first, "first"), (self.second, "second")):
            messages_response = client.get(f"/api/rooms/ROOM01/messages?participant_id={pid}")
            self.assertEqual(messages_response.status_code, 200)
            visible_messages = messages_response.get_json()["messages"]
            self.assertEqual(visible_messages[0]["role"], "interviewee")
            self.assertEqual(set(visible_messages[0]), {"id", "participant_id", "role", "text", "is_claim"})
            self.assertNotIn("truth_status", str(visible_messages))
            expected_typing_status = 200 if room["participants"][pid] == room["next_speaker"] else 403
            self.assertEqual(
                client.post("/api/rooms/ROOM01/typing", json={"participant_id": pid}).status_code,
                expected_typing_status,
            )
        interviewee_id = next(pid for pid, role in room["participants"].items() if role == "interviewee")
        submission_id = room["screening_submission_ids"][interviewee_id]
        submission = server.screening_submissions[submission_id]
        city_id = submission["order"][0]
        city_name = next(name for candidate_id, name, _ in server.CANDIDATE_CITIES if candidate_id == city_id)
        self.assertEqual(len(room["messages"]), 21)  # One claim, ten questions, and ten answers.
        self.assertEqual(room["current_claim"]["interviewer_question_count"], 10)
        self.assertEqual(room["messages"][0]["id"], 1)
        self.assertEqual(room["messages"][0]["participant_id"], interviewee_id)
        self.assertEqual(room["messages"][0]["text"], f"I have visited {city_name} in the past three years.")
        self.assertEqual(room["current_claim"], {
            "city_id": city_id,
            "truth_status": submission["answers"][city_id]["visited"] == "yes",
            "dialogue_id": 1,  # The first shuffled claim is recorded as dialogue one.
            "opening_message_id": 1,
            "interviewer_question_count": 10,
            "question_turns_ended": True,
        })

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