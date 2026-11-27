from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import patch

from sqlalchemy import MetaData, Table, select, text

import server
from app.database import db
from app.run_tables import initialize_study_run


class RoleReadinessTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(server.app.config, TURNSTILE_ENABLED=False)
        config.start()
        self.addCleanup(config.stop)
        with server.app.app_context():
            # Drop dynamic run tables explicitly because they are not registered in Flask-SQLAlchemy's fixed metadata.
            with db.engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS raw_test"))
                connection.execute(text("DROP TABLE IF EXISTS metadata_test"))
            # Create the raw event destination that the production initializer prepares before participant collection.
            initialize_study_run(
                db.engine,
                "test",
                {"human_verification_required": True},
                "dialogue_v1",
            )
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()
            server.study_sessions.clear()
        self.first = self.eligible_client()
        self.second = self.eligible_client()

    def tearDown(self):
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()
            server.study_sessions.clear()
        with server.app.app_context():
            db.session.remove()
            # Clean up the dynamically named run tables after the isolated test.
            with db.engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS raw_test"))
                connection.execute(text("DROP TABLE IF EXISTS metadata_test"))

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
        self.assertEqual(self.first.post("/api/rooms", json={"room_participant_id": "first"}).get_json()["status"], "waiting")
        self.assertEqual(self.second.post("/api/rooms", json={"room_participant_id": "second"}).get_json()["status"], "paired")

    def complete_dialogues(self):
        """Prepare a paired room whose server-side claim order is complete."""
        self.pair()
        room = server.rooms["ROOM01"]
        room["ready"].update(room["participants"])
        room["current_claim_index"] = 10
        room["current_claim"] = None
        room["next_speaker"] = None
        return room

    def prepare_judgement(self, condition):
        """Prepare a ready pair with a claim awaiting the interviewer's judgement."""
        self.pair()
        room = server.rooms["ROOM01"]
        room["ready"].update(room["participants"])
        interviewer_id = next(
            pid for pid, role in room["participants"].items() if role == "interviewer"
        )
        interviewee_id = next(
            pid for pid, role in room["participants"].items() if role == "interviewee"
        )
        clients = {"first": self.first, "second": self.second}
        room["conditions"] = {interviewer_id: condition}
        room["current_claim"] = {
            "city_id": "test-city",
            "truth_status": True,
            "dialogue_id": 1,
            "opening_message_id": 1,
            "started_at": "2026-01-01T10:00:00.000+00:00",
            "conversation_ended_at": "2026-01-01T10:00:10.000+00:00",
            "dialogue_duration_seconds": 10.0,
            "judgement_started_at": "2026-01-01T10:00:10.000+00:00",
            "judgement_submitted_at": None,
            "judgement_ended_at": None,
            "judgement_duration_seconds": None,
            "interviewer_question_count": 10,
            "question_turns_ended": True,
            "judgement": None,
        }
        room["next_speaker"] = None
        return room, clients[interviewer_id], interviewer_id, clients[interviewee_id], interviewee_id

    def test_start_click_and_page_visits_use_server_timestamps(self):
        client = server.app.test_client()
        timestamps = iter((
            "2026-01-01T09:00:00.000+00:00",  # Landing page entry.
            "2026-01-01T09:01:00.000+00:00",  # First Start-the-study click.
            "2026-01-01T09:01:02.000+00:00",  # Consent page entry.
        ))

        with patch("server._utc_timestamp", side_effect=lambda: next(timestamps)):
            landing = client.get("/")
            self.assertEqual(landing.status_code, 200)
            start = client.get("/start")
            self.assertEqual(start.status_code, 302)
            consent = client.get("/consent")
            self.assertEqual(consent.status_code, 200)

        with client.session_transaction() as browser_session:
            participant_id = browser_session["participant_id"]
            self.assertEqual(browser_session["study_started_at"], "2026-01-01T09:01:00.000+00:00")
        self.assertEqual(len(participant_id), 32)
        state = server.study_sessions[participant_id]
        self.assertEqual(state["study_started_at"], "2026-01-01T09:01:00.000+00:00")
        self.assertEqual([visit["page_name"] for visit in state["page_visits"]], ["landing", "consent"])
        self.assertEqual(state["page_visits"][0]["exited_at"], "2026-01-01T09:01:00.000+00:00")
        self.assertEqual(state["page_visits"][0]["duration_seconds"], 60.0)
        self.assertIsNone(state["page_visits"][1]["exited_at"])
        lifecycle_events = state["raw_events"]
        self.assertEqual(
            [event["event_type"] for event in lifecycle_events],
            ["page_visit", "study_started", "page_visit"],
        )
        self.assertEqual(lifecycle_events[1]["occurred_at"], "2026-01-01T09:01:00.000+00:00")
        self.assertEqual(lifecycle_events[0]["ended_at"], "2026-01-01T09:01:00.000+00:00")

    def test_participant_lifecycle_events_are_buffered_until_feedback(self):
        with self.first.session_transaction() as browser_session:
            participant_id = browser_session["participant_id"]
        state = server.study_sessions[participant_id]
        lifecycle_events = state["raw_events"]
        self.assertEqual(
            [event["event_type"] for event in lifecycle_events],
            ["study_started", "consent_decided", "screening_completed", "instructions_completed"],
        )
        screening_event = lifecycle_events[2]
        self.assertEqual(screening_event["participant_id"], participant_id)
        self.assertEqual(screening_event["event_data"]["screening_version"], server.SCREENING_VERSION)
        self.assertEqual(len(screening_event["event_data"]["answers"]), len(server.CANDIDATE_CITIES))
        self.assertEqual(len(screening_event["event_data"]["claim_order"]), 10)

        # Study lifecycle events remain memory-only until this participant submits final feedback.
        with server.app.app_context():
            raw_table = Table("raw_test", MetaData(), autoload_with=db.engine)
            self.assertEqual(db.session.execute(select(raw_table)).mappings().all(), [])

    def test_dialogue_and_judgement_durations_include_feedback_until_continue(self):
        room, interviewer, interviewer_id, _, _ = self.prepare_judgement("feedback")
        judgement_submitted_at = "2026-01-01T10:00:20.000+00:00"
        continue_clicked_at = "2026-01-01T10:00:50.000+00:00"

        with patch("server._utc_timestamp", side_effect=(judgement_submitted_at, continue_clicked_at)):
            submitted = interviewer.post("/api/rooms/ROOM01/judgement", json={
                "room_participant_id": interviewer_id,
                "judgement": "true",
                "confidence": 80,
            })
            self.assertEqual(submitted.status_code, 201)
            continued = interviewer.post("/api/rooms/ROOM01/continue", json={
                "room_participant_id": interviewer_id,
            })

        self.assertEqual(continued.status_code, 201)
        claim = room["current_claim"]
        self.assertEqual(claim["conversation_ended_at"], "2026-01-01T10:00:10.000+00:00")
        self.assertEqual(claim["dialogue_duration_seconds"], 10.0)
        self.assertEqual(claim["judgement_submitted_at"], judgement_submitted_at)
        self.assertEqual(claim["judgement_ended_at"], continue_clicked_at)
        self.assertEqual(claim["judgement_duration_seconds"], 40.0)
        judgement_event = next(event for event in room["raw_events"] if event["event_type"] == "judgement_submitted")
        self.assertEqual(judgement_event["participant_id"], room["participant_ids"][interviewer_id])
        self.assertEqual(judgement_event["event_data"]["judgement"], "true")
        self.assertEqual(judgement_event["event_data"]["confidence"], 80)
        self.assertEqual(judgement_event["started_at"], claim["judgement_started_at"])
        self.assertEqual(judgement_event["occurred_at"], judgement_submitted_at)
        self.assertEqual(judgement_event["ended_at"], continue_clicked_at)
        self.assertEqual(judgement_event["duration_seconds"], 40.0)
        continuation_event = next(event for event in room["raw_events"] if event["event_type"] == "dialogue_continued")
        self.assertEqual(continuation_event["dialogue_number"], 1)
        self.assertEqual(continuation_event["occurred_at"], continue_clicked_at)

    def prepare_message_turn(self):
        """Prepare a ready pair with the interviewer allowed to send the first question."""
        self.pair()
        room = server.rooms["ROOM01"]
        room["ready"].update(room["participants"])
        clients = {"first": self.first, "second": self.second}
        for client in clients.values():
            self.assertEqual(client.get("/dialogue").status_code, 200)
        interviewer_id = next(
            pid for pid, role in room["participants"].items() if role == "interviewer"
        )
        interviewee_id = next(
            pid for pid, role in room["participants"].items() if role == "interviewee"
        )
        return room, clients[interviewer_id], interviewer_id, clients[interviewee_id], interviewee_id

    def status(self, client):
        response = client.get("/api/rooms/ROOM01")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def assert_chat_blocked(self, client, participant_id):
        for method, suffix in (("GET", "/messages"), ("POST", "/messages"), ("POST", "/typing")):
            with self.subTest(method=method, suffix=suffix, participant_id=participant_id):
                response = client.open(
                    f"/api/rooms/ROOM01{suffix}?room_participant_id={participant_id}",
                    method=method, json={"room_participant_id": participant_id, "text": "Hello"},
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
        room["current_claim"]["judgement"] = {"judgement": "true", "confidence": 80, "correct": True}

        with patch("server.monotonic", side_effect=[100.0, 101.0, 101.0, 104.0, 104.0, 104.0, 104.0]):
            continued = clients[interviewer_id].post(
                "/api/rooms/ROOM01/continue",
                json={"room_participant_id": interviewer_id},
            )
            self.assertEqual(continued.status_code, 201)
            self.assertEqual(room["current_claim_index"], 9)
            self.assertIsNotNone(room["current_claim"])

            countdown = clients[interviewee_id].get(
                f"/api/rooms/ROOM01/messages?room_participant_id={interviewee_id}"
            ).get_json()
            self.assertEqual(countdown["seconds_remaining"], 2.0)
            self.assertEqual(countdown["dialogue_number"], 10)
            self.assertFalse(countdown["transition_complete"])
            self.assertEqual(room["current_claim_index"], 9)
            interviewer_countdown = clients[interviewer_id].get(
                f"/api/rooms/ROOM01/messages?room_participant_id={interviewer_id}"
            ).get_json()
            self.assertEqual(interviewer_countdown["seconds_remaining"], countdown["seconds_remaining"])
            self.assertEqual(interviewer_countdown["dialogue_number"], countdown["dialogue_number"])

            for participant_id, client in clients.items():
                with self.subTest(participant_id=participant_id):
                    poll = client.get(f"/api/rooms/ROOM01/messages?room_participant_id={participant_id}")
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

    def test_feedback_submission_requires_all_ten_dialogues(self):
        self.pair()
        room = server.rooms["ROOM01"]
        room["ready"].update(room["participants"])

        response = self.first.post("/submit_feedback", data={})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/dialogue")
        self.assertNotIn("feedback_submissions", room)

    def test_feedback_validates_ratings_and_five_word_strategy_limit(self):
        room = self.complete_dialogues()
        response = self.first.post("/submit_feedback", data={
            "task_skill": "8",
            "task_difficulty": "4",
            "task_motivation": "",
            "strategy": "one two three four five six",
            "additional_feedback": "Optional note",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Choose one rating from 1 to 7.", response.data)
        self.assertIn(b"Use no more than five words.", response.data)
        self.assertIn(b'value="4" checked', response.data)
        self.assertEqual(room.get("feedback_submissions", {}), {})

    def test_feedback_saves_once_in_memory_and_redirects_to_debriefing(self):
        room = self.complete_dialogues()
        response = self.first.post("/submit_feedback", data={
            "task_skill": "6",
            "task_difficulty": "2",
            "task_motivation": "7",
            "strategy": "Ask focused follow-up questions",
            "additional_feedback": "",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/debriefing")
        saved = room["feedback_submissions"]["first"]
        self.assertEqual(saved["task_skill"], 6)
        self.assertEqual(saved["task_difficulty"], 2)
        self.assertEqual(saved["task_motivation"], 7)
        self.assertEqual(saved["strategy"], "Ask focused follow-up questions")
        self.assertEqual(saved["additional_feedback"], "")
        self.assertIn("timestamp", saved)

        duplicate = self.first.post("/submit_feedback", data={
            "task_skill": "1",
            "task_difficulty": "1",
            "task_motivation": "1",
            "strategy": "Changed answer",
            "additional_feedback": "Changed note",
        })
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(duplicate.location, "/debriefing")
        self.assertEqual(room["feedback_submissions"]["first"], saved)

        second_feedback = self.second.post("/submit_feedback", data={
            "task_skill": "5",
            "task_difficulty": "3",
            "task_motivation": "6",
            "strategy": "Ask focused questions",
            "additional_feedback": "",
        })

        self.assertEqual(second_feedback.status_code, 302)
        self.assertEqual(second_feedback.location, "/debriefing")
        second_saved = room["feedback_submissions"]["second"]
        self.assertEqual(second_saved["task_skill"], 5)
        self.assertEqual(second_saved["task_difficulty"], 3)
        self.assertEqual(second_saved["task_motivation"], 6)
        self.assertEqual(second_saved["strategy"], "Ask focused questions")

        # A valid feedback submit stores the room's events and both participants' event buffers in raw_test.
        with server.app.app_context():
            raw_table = Table("raw_test", MetaData(), autoload_with=db.engine)
            saved_events = db.session.execute(select(raw_table)).mappings().all()
        self.assertIn("room_paired", {event["event_type"] for event in saved_events})

        with self.first.session_transaction() as browser_session:
            first_participant_id = browser_session["participant_id"]
        with self.second.session_transaction() as browser_session:
            second_participant_id = browser_session["participant_id"]

        server.study_sessions[first_participant_id]["study_started_at"] = "2026-01-01T10:00:00.000+00:00"
        server.study_sessions[second_participant_id]["study_started_at"] = "2026-01-01T10:00:00.000+00:00"

        for participant_id in (first_participant_id, second_participant_id):
            visit = {
                "sequence": 1,
                "page_name": "study-ended",
                "entered_at": "2026-01-01T10:01:00.000+00:00",
                "exited_at": None,
                "duration_seconds": None,
                "event_id": f"page-visit-{participant_id}",
            }
            state = server.study_sessions[participant_id]
            state["page_visits"] = [visit]
            state["open_page_visit"] = visit
            state.setdefault("raw_events", []).append({
                "event_id": visit["event_id"],
                "participant_id": participant_id,
                "room_id": room["archive_id"],
                "room_code": "ROOM01",
                "event_type": "page_visit",
                "dialogue_number": None,
                "turn_number": None,
                "occurred_at": None,
                "started_at": visit["entered_at"],
                "ended_at": None,
                "duration_seconds": None,
                "event_data": {"page_name": "study-ended"},
            })

        first_redirect_time = "2026-01-01T10:02:00.000+00:00"
        second_redirect_time = "2026-01-01T10:03:00.000+00:00"

        with patch("server._utc_timestamp", side_effect=(first_redirect_time, second_redirect_time)), patch.dict(
            server.app.config, {"PROLIFIC_COMPLETION_URL": "https://prolific.example/complete"},
        ):
            response = self.first.get("/complete-study")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "https://prolific.example/complete")
            self.assertIsNone(room.get("completed_at"))
            second_response = self.second.get("/complete-study")

        self.assertEqual(second_response.status_code, 302)
        first_state = server.study_sessions[first_participant_id]
        self.assertEqual(first_state["prolific_redirect_at"], first_redirect_time)
        self.assertEqual(first_state["duration_seconds"], 120.0)
        first_completion_events = [event for event in first_state["raw_events"] if event["event_type"] == "study_completed"]
        self.assertEqual(len(first_completion_events), 1)
        self.assertEqual(first_completion_events[0]["ended_at"], first_redirect_time)
        self.assertEqual(first_state["page_visits"][0]["exited_at"], first_redirect_time)
        self.assertEqual(first_state["page_visits"][0]["duration_seconds"], 60.0)
        second_state = server.study_sessions[second_participant_id]
        self.assertEqual(second_state["prolific_redirect_at"], second_redirect_time)
        self.assertEqual(second_state["duration_seconds"], 180.0)
        second_completion_events = [event for event in second_state["raw_events"] if event["event_type"] == "study_completed"]
        self.assertEqual(len(second_completion_events), 1)
        self.assertEqual(second_completion_events[0]["ended_at"], second_redirect_time)
        self.assertEqual(room["completed_at"], second_redirect_time)
        self.assertEqual(room["participants_completed_at"]["first"], first_redirect_time)
        self.assertEqual(room["participants_completed_at"]["second"], second_redirect_time)

        with server.app.app_context():
            raw_table = Table("raw_test", MetaData(), autoload_with=db.engine)
            raw_rows = db.session.execute(select(raw_table)).mappings().all()
            completion_events = [row for row in raw_rows if row["event_type"] == "study_completed"]
            self.assertCountEqual(
                [row["participant_id"] for row in completion_events],
                [first_participant_id, second_participant_id],
            )
            saved_visits = {row["event_id"]: row for row in raw_rows if row["event_type"] == "page_visit"}
            first_visit = saved_visits[f"page-visit-{first_participant_id}"]
            second_visit = saved_visits[f"page-visit-{second_participant_id}"]
            self.assertEqual(first_visit["ended_at"], datetime.fromisoformat(first_redirect_time).replace(tzinfo=None))
            self.assertEqual(first_visit["duration_seconds"], 60.0)
            self.assertEqual(second_visit["ended_at"], datetime.fromisoformat(second_redirect_time).replace(tzinfo=None))
            self.assertEqual(second_visit["duration_seconds"], 120.0)

    def test_interviewer_category_can_overlap_with_interviewee_reply(self):
        room, interviewer, interviewer_id, interviewee, interviewee_id = self.prepare_message_turn()
        endpoint = "/api/rooms/ROOM01/messages"

        sent_question = interviewer.post(endpoint, json={
            "room_participant_id": interviewer_id,
            "text": "What did you enjoy about the city?",
        })
        self.assertEqual(sent_question.status_code, 201)
        self.assertEqual(room["current_claim"]["interviewer_question_count"], 1)
        self.assertEqual(room["next_speaker"], "interviewee")
        pending_message = room["messages"][-1]
        self.assertIsNone(pending_message.get("question_type"))
        self.assertEqual(
            room["current_claim"]["pending_question_type_message_id"],
            pending_message["id"],
        )
        interviewer_poll = interviewer.get(endpoint, query_string={"room_participant_id": interviewer_id}).get_json()
        self.assertIs(interviewer_poll["can_send"], False)
        self.assertIs(interviewer_poll["question_type_pending"], True)
        self.assertEqual(interviewer_poll["question_number"], 1)

        # The interviewee can see the sent question but cannot reply before classification.
        interviewee_poll = interviewee.get(endpoint, query_string={"room_participant_id": interviewee_id}).get_json()
        self.assertIs(interviewee_poll["can_send"], True)
        self.assertNotIn("question_type_pending", interviewee_poll)
        interviewer_repeat = interviewer.post(endpoint, json={
            "room_participant_id": interviewer_id,
            "text": "A second question cannot be sent yet.",
        })
        self.assertEqual(interviewer_repeat.status_code, 403)
        overlapping_reply = interviewee.post(endpoint, json={
            "room_participant_id": interviewee_id,
            "text": "I enjoyed the museums.",
        })
        self.assertEqual(overlapping_reply.status_code, 201)
        self.assertEqual(room["next_speaker"], "interviewer")
        interviewer_after_reply = interviewer.get(endpoint, query_string={"room_participant_id": interviewer_id}).get_json()
        self.assertIs(interviewer_after_reply["can_send"], False)
        self.assertIs(interviewer_after_reply["question_type_pending"], True)
        blocked_next_question = interviewer.post(endpoint, json={
            "room_participant_id": interviewer_id,
            "text": "The next question must wait for classification.",
        })
        self.assertEqual(blocked_next_question.status_code, 403)
        self.assertIn("categorize", blocked_next_question.get_json()["error"].lower())

        invalid_category = interviewer.post("/api/rooms/ROOM01/question-type", json={
            "room_participant_id": interviewer_id,
            "question_type": "unsupported",
        })
        self.assertEqual(invalid_category.status_code, 400)
        self.assertEqual(room["next_speaker"], "interviewer")

        accepted = interviewer.post("/api/rooms/ROOM01/question-type", json={
            "room_participant_id": interviewer_id,
            "question_type": "open-ended",
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(pending_message["question_type"], "open-ended")
        self.assertEqual(pending_message["role"], "interviewer")
        self.assertNotIn("pending_question_type_message_id", room["current_claim"])
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertIs(interviewer.get(endpoint, query_string={"room_participant_id": interviewer_id}).get_json()["can_send"], True)

        # Neither the interviewer nor interviewee receives the server-only category through polling.
        for client, participant_id in ((interviewer, interviewer_id), (interviewee, interviewee_id)):
            poll = client.get(endpoint, query_string={"room_participant_id": participant_id}).get_json()
            question = next(message for message in poll["messages"] if message["id"] == pending_message["id"])
            self.assertNotIn("question_type", question)

        interviewer_poll = interviewer.get(endpoint, query_string={"room_participant_id": interviewer_id}).get_json()
        self.assertNotIn("question_type_pending", interviewer_poll)
        self.assertIs(interviewer_poll["can_send"], True)

    def test_interviewee_messages_reject_category_and_store_no_category(self):
        room, interviewer, interviewer_id, interviewee, interviewee_id = self.prepare_message_turn()
        endpoint = "/api/rooms/ROOM01/messages"

        bypassed_category = interviewer.post(endpoint, json={
            "room_participant_id": interviewer_id,
            "text": "What did you enjoy about the city?",
            "question_type": "follow-up",
        })
        self.assertEqual(bypassed_category.status_code, 400)
        self.assertEqual(len(room["messages"]), 1)

        interviewer_question = interviewer.post(endpoint, json={
            "room_participant_id": interviewer_id,
            "text": "What did you enjoy about the city?",
        })
        self.assertEqual(interviewer_question.status_code, 201)
        self.assertEqual(room["next_speaker"], "interviewee")

        rejected_answer_category = interviewee.post(endpoint, json={
            "room_participant_id": interviewee_id,
            "text": "I enjoyed the museums.",
            "question_type": "probing",
        })
        self.assertEqual(rejected_answer_category.status_code, 400)
        self.assertEqual(room["next_speaker"], "interviewee")
        self.assertEqual(len(room["messages"]), 2)

        accepted_answer = interviewee.post(endpoint, json={
            "room_participant_id": interviewee_id,
            "text": "I enjoyed the museums.",
        })
        self.assertEqual(accepted_answer.status_code, 201)
        self.assertEqual(room["next_speaker"], "interviewer")

        category = interviewer.post("/api/rooms/ROOM01/question-type", json={
            "room_participant_id": interviewer_id,
            "question_type": "follow-up",
        })
        self.assertEqual(category.status_code, 200)
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertIs(interviewer.get(endpoint, query_string={"room_participant_id": interviewer_id}).get_json()["can_send"], True)
        self.assertNotIn("question_type", room["messages"][-1])
        self.assertNotIn("question_type", accepted_answer.get_json()["message"])
        for client, participant_id in ((interviewer, interviewer_id), (interviewee, interviewee_id)):
            poll = client.get(endpoint, query_string={"room_participant_id": participant_id}).get_json()
            self.assertTrue(all("question_type" not in message for message in poll["messages"]))

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
            "room_participant_id": interviewer_id,
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
            f"/api/rooms/ROOM01/messages?room_participant_id={interviewer_id}"
        ).get_json()["messages"]

        self.assertEqual(len(room["messages"]), 3)  # Keep both dialogues in the room's server-side history.
        self.assertEqual(len(current_messages), 1)  # Show only the active dialogue in this browser view.
        self.assertTrue(current_messages[0]["is_claim"])
        self.assertIn("I have visited ", current_messages[0]["text"])

    def test_role_instruction_template_is_selected_by_role_and_condition(self):
        original_configuration = server.STUDY_CONFIGURATION
        test_configuration = deepcopy(original_configuration)
        conditions = test_configuration["condition_allocation"]["conditions"]
        for condition_name, condition in conditions.items():
            for role, instruction in condition["role_instructions"].items():
                instruction["text"] = f"<p>{condition_name}-{role}-only</p>"
        server.STUDY_CONFIGURATION = test_configuration
        try:
            self.pair()
            room = server.rooms["ROOM01"]
            interviewer_id = next(
                participant_id
                for participant_id, role in room["participants"].items()
                if role == "interviewer"
            )
            clients = {"first": self.first, "second": self.second}

            def capture_fragment(_template_name, **context):
                # Only the selected trusted fragment is passed to rendering; the condition label stays server-side.
                self.assertNotIn("condition", context)
                self.assertNotIn("conditions", context)
                return context["role_instruction_html"]

            for condition_name, condition in conditions.items():
                room["conditions"] = {interviewer_id: condition_name}
                for participant_id, role in room["participants"].items():
                    with self.subTest(condition=condition_name, role=role):
                        expected_text = condition["role_instructions"][role]["text"]
                        with patch("server.render_template", side_effect=capture_fragment):
                            response = clients[participant_id].get("/role-instructions")
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.get_data(as_text=True), expected_text)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
        finally:
            server.STUDY_CONFIGURATION = original_configuration

    def test_full_lifecycle_private_instructions_and_both_ready(self):
        self.assertEqual(self.first.get("/dialogue").location, "/room")
        self.assertEqual(self.first.get("/role-instructions").location, "/room")
        self.first.post("/role-instructions")
        self.first.post("/api/rooms", json={"room_participant_id": "first"})
        self.assertEqual(self.first.get("/role-instructions").location, "/room")
        self.assertEqual(server.rooms["ROOM01"]["ready"], set())
        self.second.post("/api/rooms", json={"room_participant_id": "second"})
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
            self.first.post("/api/rooms", json={"room_participant_id": "first"})
            self.first.get("/room")
            self.assertEqual(server.rooms["ROOM01"]["ready"], {"first"})
        self.assertFalse(self.status(self.first)["chat_ready"])
        self.assertFalse(self.status(self.second)["chat_ready"])
        self.assert_chat_blocked(self.first, "first")
        self.assert_chat_blocked(self.second, "second")
        self.assertEqual(server.rooms["ROOM01"]["messages"], [])

        self.second.post("/role-instructions")
        ready_events = [event for event in server.rooms["ROOM01"]["raw_events"] if event["event_type"] == "participant_ready"]
        self.assertEqual(len(ready_events), 2)
        self.assertCountEqual(
            [event["participant_id"] for event in ready_events],
            list(server.rooms["ROOM01"]["participant_ids"].values()),
        )
        self.assertCountEqual(
            [event["event_data"]["role"] for event in ready_events],
            ["interviewer", "interviewee"],
        )
        for client, pid in ((self.first, "first"), (self.second, "second")):
            early_typing = client.post(
                "/api/rooms/ROOM01/typing",
                json={"room_participant_id": pid},
            )
            self.assertEqual(early_typing.status_code, 403)
            self.assertIn("opening claim", early_typing.get_json()["error"])
            early_message = client.post(
                "/api/rooms/ROOM01/messages",
                json={"room_participant_id": pid, "text": "Too early"},
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
                self.assertNotIn(b'id="question-type-step"', dialogue_response.data)
            else:
                self.assertNotIn(b'id="transition-maze"', dialogue_response.data)
                self.assertIn(b'id="question-type-step" hidden', dialogue_response.data)
                self.assertNotIn(b"While you wait for the reply, select the question type.", dialogue_response.data)
                self.assertIn(b'aria-label="Please select a question type"', dialogue_response.data)
                self.assertIn(b"Please select a question type", dialogue_response.data)
                self.assertIn(b'id="question-type" name="question_type"', dialogue_response.data)
                self.assertIn(b'value="open-ended"', dialogue_response.data)
                self.assertIn(b'value="closed-ended"', dialogue_response.data)
                self.assertIn(b'value="follow-up"', dialogue_response.data)
                self.assertIn(b'value="clarification"', dialogue_response.data)
                self.assertIn(b'value="probing"', dialogue_response.data)
                self.assertIn(b'id="chat-input"', dialogue_response.data)
                self.assertIn(b'type="submit" disabled>Send</button>', dialogue_response.data)
                self.assertIn(b'class="chat-composer judgement-form"', dialogue_response.data)
                self.assertIn(b'id="judgement-truth-step"', dialogue_response.data)
                self.assertIn(b'id="judgement-truth-submit" type="button" disabled>Submit', dialogue_response.data)
                self.assertIn(b'id="judgement-confidence-step" hidden', dialogue_response.data)
                self.assertIn(b'id="judgement-confidence-submit" type="submit" aria-label="Submit confidence rating" disabled>Submit', dialogue_response.data)
                self.assertIn(b'id="judgement-status"', dialogue_response.data)
            clients_by_role[status["role"]] = (client, pid)

        interviewer, interviewer_id = clients_by_role["interviewer"]
        interviewee, interviewee_id = clients_by_role["interviewee"]
        room = server.rooms["ROOM01"]
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertEqual(len(room["messages"]), 1)
        # Check can_send is true only for the interviewer, then follows each accepted turn.
        interviewer_poll = interviewer.get("/api/rooms/ROOM01/messages?room_participant_id=" + interviewer_id).get_json()
        interviewee_poll = interviewee.get("/api/rooms/ROOM01/messages?room_participant_id=" + interviewee_id).get_json()
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
            json={"room_participant_id": interviewee_id, "text": "Answer too early"},
        )
        self.assertEqual(interviewee_attempt.status_code, 403)
        self.assertIn("not your turn", interviewee_attempt.get_json()["error"].lower())
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertEqual(len(room["messages"]), 1)
        # Check that the server rejects an out-of-turn typing ping without changing the turn.
        early_typing = interviewee.post(
            "/api/rooms/ROOM01/typing",
            json={"room_participant_id": interviewee_id},
        )
        self.assertEqual(early_typing.status_code, 403)
        self.assertIn("not your turn", early_typing.get_json()["error"].lower())
        self.assertEqual(room["typing"], {})
        self.assertEqual(room["next_speaker"], "interviewer")

        # Test that an accepted interviewer message makes the interviewee the next allowed speaker.
        bypassed_category = interviewer.post(
            "/api/rooms/ROOM01/messages",
            json={"room_participant_id": interviewer_id, "text": "Question 1?", "role": "spoof", "question_type": "open-ended"},
        )
        self.assertEqual(bypassed_category.status_code, 400)
        self.assertEqual(len(room["messages"]), 1)

        interviewer_message = interviewer.post(
            "/api/rooms/ROOM01/messages",
            json={"room_participant_id": interviewer_id, "text": "Question 1?", "role": "spoof"},
        )
        self.assertEqual(interviewer_message.status_code, 201)
        self.assertEqual(interviewer_message.get_json()["message"]["role"], "interviewer")
        self.assertEqual(room["next_speaker"], "interviewee")
        self.assertEqual(len(room["messages"]), 2)
        interviewer_poll = interviewer.get(f"/api/rooms/ROOM01/messages?room_participant_id={interviewer_id}").get_json()
        interviewee_poll = interviewee.get(f"/api/rooms/ROOM01/messages?room_participant_id={interviewee_id}").get_json()
        self.assertIs(interviewer_poll["can_send"], False)
        self.assertIs(interviewee_poll["can_send"], True)
        self.assertEqual(interviewer_poll["question_number"], 1)
        self.assertEqual(interviewee_poll["question_number"], 1)
        self.assertTrue(interviewer_poll["question_type_pending"])

        category = interviewer.post("/api/rooms/ROOM01/question-type", json={
            "room_participant_id": interviewer_id,
            "question_type": "open-ended",
        })
        self.assertEqual(category.status_code, 200)
        self.assertEqual(room["next_speaker"], "interviewee")
        interviewee_poll = interviewee.get(f"/api/rooms/ROOM01/messages?room_participant_id={interviewee_id}").get_json()
        self.assertIs(interviewee_poll["can_send"], True)

        # Test that an out-of-turn rejection does not change whose turn it is.
        rejected_repeat = interviewer.post(
            "/api/rooms/ROOM01/messages",
            json={"room_participant_id": interviewer_id, "text": "Another question too soon"},
        )
        self.assertEqual(rejected_repeat.status_code, 403)
        self.assertIn("not your turn", rejected_repeat.get_json()["error"].lower())
        self.assertEqual(room["next_speaker"], "interviewee")
        self.assertEqual(len(room["messages"]), 2)

        interviewee_message = interviewee.post(
            "/api/rooms/ROOM01/messages",
            json={"room_participant_id": interviewee_id, "text": "Answer 1."},
        )
        self.assertEqual(interviewee_message.status_code, 201)
        self.assertEqual(interviewee_message.get_json()["message"]["role"], "interviewee")
        self.assertEqual(room["next_speaker"], "interviewer")
        self.assertEqual(len(room["messages"]), 3)
        interviewer_poll = interviewer.get(f"/api/rooms/ROOM01/messages?room_participant_id={interviewer_id}").get_json()
        interviewee_poll = interviewee.get(f"/api/rooms/ROOM01/messages?room_participant_id={interviewee_id}").get_json()
        self.assertIs(interviewer_poll["can_send"], True)
        self.assertIs(interviewee_poll["can_send"], False)
        self.assertEqual(interviewer_poll["question_number"], 2)
        self.assertEqual(interviewee_poll["question_number"], 2)

        for question_number in range(2, 11):
            question = interviewer.post(
                "/api/rooms/ROOM01/messages",
                json={"room_participant_id": interviewer_id, "text": f"Question {question_number}?"},
            )
            self.assertEqual(question.status_code, 201)
            self.assertEqual(room["current_claim"]["interviewer_question_count"], question_number)
            during_category_turn = interviewee.get(
                f"/api/rooms/ROOM01/messages?room_participant_id={interviewee_id}"
            ).get_json()
            self.assertIs(during_category_turn["can_send"], True)

            # Earlier answers can follow classification or overlap with it; the final answer exercises the race.
            if question_number < 10:
                category = interviewer.post("/api/rooms/ROOM01/question-type", json={
                    "room_participant_id": interviewer_id,
                    "question_type": "open-ended",
                })
                self.assertEqual(category.status_code, 200)
                during_answer_turn = interviewee.get(
                    f"/api/rooms/ROOM01/messages?room_participant_id={interviewee_id}"
                ).get_json()
                self.assertEqual(during_answer_turn["question_number"], question_number)
                self.assertIs(during_answer_turn["can_send"], True)

            answer = interviewee.post(
                "/api/rooms/ROOM01/messages",
                json={"room_participant_id": interviewee_id, "text": f"Answer {question_number}."},
            )
            self.assertEqual(answer.status_code, 201)
            if question_number == 10:
                # Final judgement remains blocked until its question receives a category.
                self.assertTrue(room["current_claim"]["question_turns_ended"])
                final_pending_poll = interviewer.get(
                    f"/api/rooms/ROOM01/messages?room_participant_id={interviewer_id}"
                ).get_json()
                self.assertTrue(final_pending_poll["question_type_pending"])
                self.assertFalse(final_pending_poll["can_send"])
                premature_judgement = interviewer.post("/api/rooms/ROOM01/judgement", json={
                    "room_participant_id": interviewer_id,
                    "judgement": "true",
                    "confidence": 50,
                })
                self.assertEqual(premature_judgement.status_code, 403)
                self.assertIn("categorize", premature_judgement.get_json()["error"].lower())
                final_category = interviewer.post("/api/rooms/ROOM01/question-type", json={
                    "room_participant_id": interviewer_id,
                    "question_type": "open-ended",
                })
                self.assertEqual(final_category.status_code, 200)
            if question_number < 10:
                next_question_turn = interviewer.get(
                    f"/api/rooms/ROOM01/messages?room_participant_id={interviewer_id}"
                ).get_json()
                self.assertEqual(next_question_turn["question_number"], question_number + 1)
                self.assertIs(next_question_turn["can_send"], True)

        self.assertTrue(room["current_claim"]["question_turns_ended"])
        self.assertIsNone(room["next_speaker"])
        dialogue_completion_events = [event for event in room["raw_events"] if event["event_type"] == "dialogue_completed"]
        judgement_events = [event for event in room["raw_events"] if event["event_type"] == "judgement_submitted"]
        continuation_events = [event for event in room["raw_events"] if event["event_type"] == "dialogue_continued"]
        self.assertEqual(len(dialogue_completion_events), 1)
        self.assertEqual(judgement_events, [])
        self.assertEqual(continuation_events, [])
        self.assertEqual(dialogue_completion_events[0]["dialogue_number"], 1)
        self.assertEqual(dialogue_completion_events[0]["ended_at"], room["current_claim"]["conversation_ended_at"])
        self.assertEqual(dialogue_completion_events[0]["duration_seconds"], room["current_claim"]["dialogue_duration_seconds"])
        for client, pid in ((interviewer, interviewer_id), (interviewee, interviewee_id)):
            finished_poll = client.get(f"/api/rooms/ROOM01/messages?room_participant_id={pid}").get_json()
            self.assertEqual(finished_poll["question_number"], 10)
            self.assertIs(finished_poll["question_turns_ended"], True)
            self.assertIs(finished_poll["can_send"], False)
            extra_message = client.post(
                "/api/rooms/ROOM01/messages",
                json={"room_participant_id": pid, "text": "Extra message"},
            )
            self.assertEqual(extra_message.status_code, 403)
            extra_typing = client.post("/api/rooms/ROOM01/typing", json={"room_participant_id": pid})
            self.assertEqual(extra_typing.status_code, 403)

        for client, pid in ((self.first, "first"), (self.second, "second")):
            messages_response = client.get(f"/api/rooms/ROOM01/messages?room_participant_id={pid}")
            self.assertEqual(messages_response.status_code, 200)
            visible_messages = messages_response.get_json()["messages"]
            self.assertEqual(visible_messages[0]["role"], "interviewee")
            self.assertEqual(set(visible_messages[0]), {"id", "room_participant_id", "role", "text", "is_claim"})
            self.assertNotIn("truth_status", str(visible_messages))
            expected_typing_status = 200 if room["participants"][pid] == room["next_speaker"] else 403
            self.assertEqual(
                client.post("/api/rooms/ROOM01/typing", json={"room_participant_id": pid}).status_code,
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
        self.assertEqual(room["messages"][0]["room_participant_id"], interviewee_id)
        self.assertEqual(room["messages"][0]["text"], f"I have visited {city_name} in the past three years.")
        self.assertEqual(room["current_claim"]["city_id"], city_id)
        self.assertEqual(
            room["current_claim"]["truth_status"],
            submission["answers"][city_id]["visited"] == "yes",
        )
        self.assertEqual(room["current_claim"]["dialogue_id"], 1)
        self.assertEqual(room["current_claim"]["opening_message_id"], 1)
        self.assertEqual(room["current_claim"]["interviewer_question_count"], 10)
        self.assertTrue(room["current_claim"]["question_turns_ended"])
        self.assertIsNotNone(room["current_claim"]["started_at"])
        self.assertIsNotNone(room["current_claim"]["conversation_ended_at"])
        self.assertGreaterEqual(room["current_claim"]["dialogue_duration_seconds"], 0)
        self.assertEqual(
            room["current_claim"]["judgement_started_at"],
            room["current_claim"]["conversation_ended_at"],
        )

    def test_swapped_ids_and_new_session_cannot_claim_membership(self):
        self.pair()
        outsider = self.eligible_client()
        self.assertEqual(outsider.post("/api/rooms", json={"room_participant_id": "first"}).status_code, 403)
        self.assertEqual(self.first.post("/api/rooms", json={"room_participant_id": "second"}).status_code, 403)
        self.assertEqual(self.first.post("/api/rooms", json={"room_participant_id": "new-id"}).status_code, 403)
        for client, claimed in ((self.first, "second"), (self.second, "first"), (outsider, "first")):
            self.assertEqual(client.get(f"/api/rooms/ROOM01?room_participant_id={claimed}").status_code, 403)
            self.assertEqual(client.get(f"/role-instructions?room_participant_id={claimed}").status_code, 403)
            self.assertEqual(client.post("/role-instructions", data={"room_participant_id": claimed}).status_code, 403)
            self.assertEqual(client.post("/role-instructions", json={"room_participant_id": claimed}).status_code, 403)
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
        self.assertEqual(self.first.post("/api/rooms", json={"room_participant_id": "first"}).status_code, 200)
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