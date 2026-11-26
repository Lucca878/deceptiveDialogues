import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine

import server
from app.database import db
from app.raw_exports import PARTICIPANT_COLUMNS, ROOM_COLUMNS, export_raw_study
from app.run_tables import initialize_study_run, insert_raw_events
from city_candidates import CANDIDATE_CITIES


class RawEventExportTests(unittest.TestCase):
    def setUp(self):
        # Isolate the dynamic raw_test table from the application's legacy ORM schema.
        self.engine = create_engine("sqlite:///:memory:")
        initialize_study_run(
            self.engine,
            "test",
            {"dialogue_count": 10, "human_verification_required": True},
            "dialogue_v1",
        )
        self.events = self._fixture_events()
        with self.engine.begin() as connection:
            insert_raw_events(connection, "test", self.events)

    def tearDown(self):
        # Dispose the in-memory database once export assertions finish.
        self.engine.dispose()

    def _fixture_events(self):
        participant_ids = ("participant-interviewer", "participant-interviewee")
        room_id = "opaque-room-id"
        room_code = "ROOM01"
        first_time = "2026-01-01T10:00:00.000+00:00"
        answers = {
            city_id: {"visited": "yes" if index < 10 else "no", "familiarity": 4 + (index % 3)}
            for index, (city_id, _, _) in enumerate(CANDIDATE_CITIES)
        }
        events = []

        def add(event_id, event_type, *, participant_id=None, dialogue_number=None, turn_number=None,
                occurred_at=None, started_at=None, ended_at=None, duration_seconds=None,
                event_data=None, event_room_id=room_id, event_room_code=room_code):
            events.append({
                "event_id": event_id,
                "participant_id": participant_id,
                "room_id": event_room_id,
                "room_code": event_room_code,
                "event_type": event_type,
                "dialogue_number": dialogue_number,
                "turn_number": turn_number,
                "occurred_at": occurred_at,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                "event_data": event_data or {},
            })

        for index, participant_id in enumerate(participant_ids):
            participant_start = f"2026-01-01T10:00:{index:02d}.000+00:00"
            add(
                f"verification-{index}", "human_verification_completed", participant_id=participant_id,
                occurred_at="2026-01-01T10:00:01.000+00:00",
                started_at="2026-01-01T10:00:00.000+00:00",
                ended_at="2026-01-01T10:00:01.000+00:00", duration_seconds=1.0,
                event_data={"verified": True, "prolific_id": f"prolific-{index}", "verification_page_visits": []},
                event_room_id=None, event_room_code=None,
            )
            add(f"start-{index}", "study_started", participant_id=participant_id, occurred_at=participant_start, event_room_id=None, event_room_code=None)
            add(f"consent-{index}", "consent_decided", participant_id=participant_id, occurred_at="2026-01-01T10:01:00.000+00:00", event_data={"decision": "accept"}, event_room_id=None, event_room_code=None)
            add(
                f"screening-{index}", "screening_completed", participant_id=participant_id,
                occurred_at="2026-01-01T10:02:00.000+00:00",
                event_data={"submission_id": f"screen-{index}", "screening_version": server.SCREENING_VERSION,
                            "claim_order": ["liverpool"], "answers": answers},
                event_room_id=None, event_room_code=None,
            )
            add(f"instructions-{index}", "instructions_completed", participant_id=participant_id, occurred_at="2026-01-01T10:03:00.000+00:00", event_room_id=None, event_room_code=None)
            add(
                f"page-dialogue-{index}", "page_visit", participant_id=participant_id,
                started_at="2026-01-01T10:04:00.000+00:00", ended_at="2026-01-01T10:10:40.000+00:00",
                duration_seconds=400.0, event_data={"page_name": "dialogue"},
            )

        add(
            "pair-1", "room_paired", occurred_at="2026-01-01T10:03:10.000+00:00",
            event_data={"participants": [
                {"participant_id": participant_ids[0], "role": "interviewer"},
                {"participant_id": participant_ids[1], "role": "interviewee"},
            ], "condition": "feedback"},
        )
        add("claim-1", "dialogue_started", participant_id=participant_ids[1], dialogue_number=1,
            started_at="2026-01-01T10:04:00.000+00:00",
            event_data={"role": "interviewee", "city_id": "liverpool", "claim": "I visited Liverpool.", "ground_truth": True})
        add("question-1", "message_sent", participant_id=participant_ids[0], dialogue_number=1, turn_number=1,
            occurred_at="2026-01-01T10:05:00.000+00:00",
            event_data={"role": "interviewer", "message": "What did you do there?", "is_claim": False})
        add("category-1", "question_type_assigned", participant_id=participant_ids[0], dialogue_number=1, turn_number=1,
            occurred_at="2026-01-01T10:05:01.000+00:00",
            event_data={"target_event_id": "question-1", "question_type": "open-ended"})
        add("answer-1", "message_sent", participant_id=participant_ids[1], dialogue_number=1, turn_number=1,
            occurred_at="2026-01-01T10:06:00.000+00:00",
            event_data={"role": "interviewee", "message": "I visited the waterfront.", "is_claim": False})
        add("dialogue-end-1", "dialogue_completed", participant_id=participant_ids[1], dialogue_number=1,
            started_at="2026-01-01T10:04:00.000+00:00", ended_at="2026-01-01T10:10:00.000+00:00",
            duration_seconds=360.0, event_data={"claim": "I visited Liverpool.", "ground_truth": True})
        add("judgement-1", "judgement_submitted", participant_id=participant_ids[0], dialogue_number=1,
            occurred_at="2026-01-01T10:10:10.000+00:00", started_at="2026-01-01T10:10:00.000+00:00",
            ended_at="2026-01-01T10:10:40.000+00:00", duration_seconds=40.0,
            event_data={"judgement": "true", "confidence": 80, "correct": True})
        add("continue-1", "dialogue_continued", participant_id=participant_ids[0], dialogue_number=1,
            occurred_at="2026-01-01T10:10:40.000+00:00")
        for index, participant_id in enumerate(participant_ids):
            add(f"feedback-{index}", "feedback_submitted", participant_id=participant_id,
                occurred_at="2026-01-01T10:14:30.000+00:00",
                event_data={"task_skill": 5 + index, "task_difficulty": 3, "task_motivation": 6,
                            "strategy": "Ask focused questions", "additional_feedback": "Optional comment"})
            add(f"completed-{index}", "study_completed", participant_id=participant_id,
                occurred_at="2026-01-01T10:15:00.000+00:00", started_at=participant_start,
                ended_at="2026-01-01T10:15:00.000+00:00", duration_seconds=900.0,
                event_data={"handoff_initiated": True})
        return events

    def test_raw_events_reconstruct_participant_and_room_csvs(self):
        output_directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, output_directory)
        with server.app.app_context():
            counts = export_raw_study(output_directory, "test", self.engine)

        self.assertEqual(counts, {"participants": 2, "rooms": 1, "dialogues": 1})
        participant_csv = next(output_directory.glob("participants_test_*.csv"))
        room_csv = next(output_directory.glob("rooms_test_*.csv"))
        with participant_csv.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            participants = list(reader)
            self.assertEqual(reader.fieldnames, list(PARTICIPANT_COLUMNS))
        interviewer_row = next(row for row in participants if row["role"] == "interviewer")
        interviewee_row = next(row for row in participants if row["role"] == "interviewee")
        self.assertEqual(interviewer_row["participant_id"], "participant-interviewer")
        self.assertEqual(interviewer_row["prolific_id"], "prolific-0")
        self.assertEqual(interviewer_row["study_run_id"], "test")
        self.assertEqual(interviewer_row["room_id"], "opaque-room-id")
        self.assertEqual(interviewer_row["condition"], "feedback")
        self.assertEqual(interviewee_row["condition"], "feedback")
        self.assertEqual(interviewer_row["screening_liverpool_visited"], "yes")
        self.assertEqual(interviewer_row["screening_liverpool_familiarity"], "4")
        self.assertEqual(interviewer_row["dialogue_duration_seconds"], "400.0")

        with room_csv.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            dialogues = list(reader)
            self.assertEqual(reader.fieldnames, list(ROOM_COLUMNS))
        self.assertEqual(len(dialogues), 1)
        self.assertEqual(dialogues[0]["interviewer_participant_id"], "participant-interviewer")
        self.assertEqual(dialogues[0]["interviewee_participant_id"], "participant-interviewee")
        self.assertEqual(dialogues[0]["dialogue_duration_seconds"], "360.0")
        self.assertEqual(dialogues[0]["judgement_duration_seconds"], "40.0")
        self.assertEqual(dialogues[0]["judgement"], "true")
        self.assertEqual(dialogues[0]["interviewer_message_1"], "What did you do there?")
        self.assertEqual(dialogues[0]["question_type_interviewer_message_1"], "open-ended")
        self.assertEqual(dialogues[0]["interviewee_message_1"], "I visited the waterfront.")

        complete_json = json.loads(next(output_directory.glob("complete_test_*.json")).read_text())
        room_json = json.loads(next(output_directory.glob("room_opaque-room-id_test_*.json")).read_text())
        self.assertEqual(len(complete_json["participants"]), 2)
        self.assertEqual(len(complete_json["rooms"]), 1)
        self.assertEqual(room_json["room"]["dialogues"][0]["turns"][0]["question_type_interviewer_message"], "open-ended")

    def test_raw_export_rejects_uninitialized_run_and_public_static_path(self):
        with server.app.app_context():
            with tempfile.TemporaryDirectory() as output_directory:
                with self.assertRaises(Exception):
                    export_raw_study(Path(output_directory), "missing", self.engine)
            with self.assertRaises(Exception):
                export_raw_study(Path(server.app.static_folder), "test", self.engine)

    def test_export_study_cli_uses_raw_test(self):
        # Exercise the registered production command against the app's selected SQLite engine.
        with patch.dict(server.app.config, TURNSTILE_ENABLED=False, STUDY_RUN_ID="test"), server.app.app_context():
            engine = db.engine
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE IF EXISTS raw_test")
                connection.exec_driver_sql("DROP TABLE IF EXISTS metadata_test")
            initialize_study_run(engine, "test", {"human_verification_required": True}, "dialogue_v1")
            with engine.begin() as connection:
                insert_raw_events(connection, "test", [{
                    "event_id": "cli-start-event",
                    "participant_id": "cli-participant",
                    "event_type": "study_started",
                    "occurred_at": "2026-01-01T10:00:00.000+00:00",
                    "event_data": {},
                }])

        output_directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, output_directory)
        result = server.app.test_cli_runner().invoke(args=[
            "export-study", "--output", str(output_directory), "--run-id", "test",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("from raw events", result.output)
        participant_csv = next(output_directory.glob("participants_test_*.csv"))
        with participant_csv.open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(rows[0]["participant_id"], "cli-participant")

        # Drop dynamic test-run tables so this CLI integration does not leak state across suites.
        with server.app.app_context(), db.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS raw_test")
            connection.exec_driver_sql("DROP TABLE IF EXISTS metadata_test")


if __name__ == "__main__":
    unittest.main()
