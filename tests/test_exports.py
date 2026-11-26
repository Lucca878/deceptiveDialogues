import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from app.study_data import PARTICIPANT_COLUMNS, ROOM_COLUMNS, export_study
from app.database import (
    db,
    DialogueRecord,
    Feedback,
    Message,
    PageVisit,
    Participant,
    Room,
    ScreeningAnswer,
    ScreeningSubmission,
)


class StudyExportTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(server.app.config, TURNSTILE_ENABLED=False, STUDY_DATASET="test")
        config.start()
        self.addCleanup(config.stop)
        with server.app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()
            server.study_sessions.clear()

    def tearDown(self):
        with server.app.app_context():
            db.session.remove()
            db.drop_all()
        with server.rooms_lock:
            server.rooms.clear()
            server.screening_submissions.clear()
            server.study_sessions.clear()

    def seed_completed_room(self):
        participants = [
            Participant(
                study_session_id="session-interviewer",
                dataset="test",
                study_started_at="2026-01-01T10:00:00.000+00:00",
                consent_accepted=True,
                consent_accepted_at="2026-01-01T10:01:00.000+00:00",
                entry_verified=True,
                travel_intro_seen=True,
                screening_complete_version=server.SCREENING_VERSION,
                instructions_complete=True,
                prolific_redirect_at="2026-01-01T10:15:00.000+00:00",
                duration_seconds=900.0,
            ),
            Participant(
                study_session_id="session-interviewee",
                dataset="test",
                study_started_at="2026-01-01T10:00:10.000+00:00",
                consent_accepted=True,
                consent_accepted_at="2026-01-01T10:01:10.000+00:00",
                entry_verified=True,
                travel_intro_seen=True,
                screening_complete_version=server.SCREENING_VERSION,
                instructions_complete=True,
                prolific_redirect_at="2026-01-01T10:15:10.000+00:00",
                duration_seconds=900.0,
            ),
        ]
        interviewer_screening = ScreeningSubmission(
            submission_id="screen-interviewer",
            participant=participants[0],
            version=server.SCREENING_VERSION,
            submitted_at="2026-01-01T10:02:00.000+00:00",
            claim_order=["liverpool"],
            answers=[
                ScreeningAnswer(city_id=city_id, visited="yes" if index < 5 else "no", familiarity=4)
                for index, (city_id, _, _) in enumerate(server.CANDIDATE_CITIES)
            ],
        )
        interviewee_screening = ScreeningSubmission(
            submission_id="screen-interviewee",
            participant=participants[1],
            version=server.SCREENING_VERSION,
            submitted_at="2026-01-01T10:02:10.000+00:00",
            claim_order=["liverpool"],
            answers=[
                ScreeningAnswer(city_id=city_id, visited="yes" if index < 5 else "no", familiarity=6)
                for index, (city_id, _, _) in enumerate(server.CANDIDATE_CITIES)
            ],
        )
        room = Room(
            id="opaque-room-id",
            dataset="test",
            room_code="ROOM01",
            condition="feedback",
            interviewer=participants[0],
            interviewee=participants[1],
            interviewer_screening=interviewer_screening,
            interviewee_screening=interviewee_screening,
            created_at="2026-01-01T10:03:00.000+00:00",
            paired_at="2026-01-01T10:03:10.000+00:00",
            chat_started_at="2026-01-01T10:04:00.000+00:00",
            completed_at="2026-01-01T10:14:00.000+00:00",
            messages=[
                Message(
                    sequence=1,
                    participant=participants[1],
                    role="interviewee",
                    text="I have visited Liverpool in the past three years.",
                    is_claim=True,
                    sent_at="2026-01-01T10:04:00.000+00:00",
                ),
                Message(
                    sequence=2,
                    participant=participants[0],
                    role="interviewer",
                    text="What did you do there?",
                    question_type="open-ended",
                    is_claim=False,
                    sent_at="2026-01-01T10:04:10.000+00:00",
                ),
                Message(
                    sequence=3,
                    participant=participants[1],
                    role="interviewee",
                    text="I visited the waterfront.",
                    is_claim=False,
                    sent_at="2026-01-01T10:04:20.000+00:00",
                ),
            ],
            dialogues=[
                DialogueRecord(
                    dialogue_number=1,
                    city_id="liverpool",
                    claim_text="I have visited Liverpool in the past three years.",
                    truth_status=True,
                    started_at="2026-01-01T10:04:00.000+00:00",
                    conversation_ended_at="2026-01-01T10:10:00.000+00:00",
                    dialogue_duration_seconds=360.0,
                    judgement="true",
                    confidence=80,
                    correct=True,
                    judgement_submitted_at="2026-01-01T10:10:10.000+00:00",
                    judgement_ended_at="2026-01-01T10:10:40.000+00:00",
                    judgement_duration_seconds=40.0,
                ),
            ],
        )
        participants[0].page_visits.append(PageVisit(
            room=room,
            sequence=1,
            page_name="dialogue",
            entered_at="2026-01-01T10:04:00.000+00:00",
            exited_at="2026-01-01T10:10:40.000+00:00",
            duration_seconds=400.0,
        ))
        participants[0].feedback = Feedback(
            room=room,
            task_skill=5,
            task_difficulty=3,
            task_motivation=6,
            strategy="Ask focused questions",
            additional_feedback="Optional comment",
            submitted_at="2026-01-01T10:14:30.000+00:00",
        )
        with server.app.app_context():
            db.session.add(room)
            db.session.commit()

    def test_export_writes_ordered_csvs_and_complete_json_bundle(self):
        self.seed_completed_room()
        with tempfile.TemporaryDirectory() as output_directory:
            with server.app.app_context():
                counts = export_study(Path(output_directory), "test")

            self.assertEqual(counts, {"participants": 2, "rooms": 1, "dialogues": 1})
            participant_csv = next(Path(output_directory).glob("participants_test_*.csv"))
            room_csv = next(Path(output_directory).glob("rooms_test_*.csv"))
            with participant_csv.open(encoding="utf-8-sig", newline="") as csv_file:
                participant_reader = csv.DictReader(csv_file)
                participant_rows = list(participant_reader)
                headers = participant_reader.fieldnames
            self.assertEqual(headers, list(PARTICIPANT_COLUMNS))
            self.assertEqual(len(participant_rows), 2)
            self.assertEqual(participant_rows[0]["participant_id"], "session-interviewer")
            self.assertEqual(participant_rows[0]["study_run_id"], "test")
            self.assertEqual(participant_rows[0]["room_id"], "opaque-room-id")
            self.assertEqual(participant_rows[0]["role"], "interviewer")
            self.assertEqual(participant_rows[0]["condition"], "feedback")
            self.assertEqual(participant_rows[1]["role"], "interviewee")
            self.assertEqual(participant_rows[1]["condition"], "feedback")
            self.assertEqual(participant_rows[0]["screening_liverpool_visited"], "yes")
            self.assertEqual(participant_rows[0]["screening_liverpool_familiarity"], "4")
            self.assertEqual(participant_rows[0]["dialogue_duration_seconds"], "400.0")

            with room_csv.open(encoding="utf-8-sig", newline="") as csv_file:
                room_reader = csv.DictReader(csv_file)
                room_rows = list(room_reader)
                room_headers = room_reader.fieldnames
            self.assertEqual(room_headers, list(ROOM_COLUMNS))
            self.assertEqual(len(room_rows), 1)
            self.assertEqual(room_rows[0]["interviewee_participant_id"], "session-interviewee")
            self.assertEqual(room_rows[0]["judgement"], "true")
            self.assertEqual(room_rows[0]["confidence"], "80.0")
            self.assertEqual(room_rows[0]["interviewer_message_1"], "What did you do there?")
            self.assertEqual(room_rows[0]["question_type_interviewer_message_1"], "open-ended")
            self.assertEqual(room_rows[0]["interviewee_message_1"], "I visited the waterfront.")

            participants_json = json.loads(next(Path(output_directory).glob("participants_test_*.json")).read_text())
            rooms_json = json.loads(next(Path(output_directory).glob("rooms_test_*.json")).read_text())
            complete_json = json.loads(next(Path(output_directory).glob("complete_test_*.json")).read_text())
            per_room_json = json.loads(next(Path(output_directory).glob("room_opaque-room-id_test_*.json")).read_text())
            self.assertEqual(len(participants_json["participants"]), 2)
            self.assertEqual(len(rooms_json["rooms"]), 1)
            self.assertEqual(len(complete_json["participants"]), 2)
            self.assertEqual(len(complete_json["rooms"]), 1)
            self.assertEqual(per_room_json["room"]["dialogues"][0]["turns"][0]["question_type_interviewer_message"], "open-ended")
            self.assertNotIn("room_token", str(complete_json))
            self.assertNotIn("session_cookie", str(complete_json))

    def test_export_refuses_public_static_directory(self):
        with server.app.app_context():
            with self.assertRaises(Exception):
                export_study(Path(server.app.static_folder), "test")


if __name__ == "__main__":
    unittest.main()
