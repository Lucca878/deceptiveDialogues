import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

from app.run_tables import initialize_study_run
import server


class RunTableInitializationTests(unittest.TestCase):
    def setUp(self):
        # Use a private in-memory database so run-specific test tables cannot leak between tests.
        self.engine = create_engine("sqlite:///:memory:")
        self.configuration = {
            "dialogue_count": 10,
            "questions_per_dialogue": 10,
            "human_verification_required": True,
        }

    def tearDown(self):
        # Close the in-memory database after each isolated schema test.
        self.engine.dispose()

    def test_initialization_creates_raw_and_metadata_tables_with_run_default(self):
        created = initialize_study_run(
            self.engine,
            "test",
            self.configuration,
            "dialogue_v1",
            "abc1234",
        )

        self.assertTrue(created)
        inspector = inspect(self.engine)
        self.assertIn("raw_test", inspector.get_table_names())
        self.assertIn("metadata_test", inspector.get_table_names())
        self.assertEqual(
            {column["name"] for column in inspector.get_columns("raw_test")},
            {
                "event_id", "study_run_id", "participant_id", "room_id", "room_code",
                "event_type", "dialogue_number", "turn_number", "occurred_at", "started_at",
                "ended_at", "duration_seconds", "event_data",
            },
        )

        # The table default supplies run identity; the application does not send it with each event.
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO raw_test (event_id, event_type, event_data) "
                "VALUES ('event-1', 'message_sent', '{\"message\": \"Hello\"}')"
            ))
            row = connection.execute(text(
                "SELECT study_run_id, event_data FROM raw_test WHERE event_id = 'event-1'"
            )).one()
        self.assertEqual(row.study_run_id, "test")
        self.assertEqual(row.event_data, '{"message": "Hello"}')

    def test_repeated_initialization_preserves_original_snapshot(self):
        self.assertTrue(initialize_study_run(
            self.engine, "pilot_2", self.configuration, "dialogue_v1", "first-revision",
        ))
        self.assertFalse(initialize_study_run(
            self.engine, "pilot_2", self.configuration, "dialogue_v1", "later-revision",
        ))

        with self.engine.connect() as connection:
            metadata = connection.execute(text(
                "SELECT application_revision FROM metadata_pilot_2 WHERE study_run_id = 'pilot_2'"
            )).scalar_one()
        self.assertEqual(metadata, "first-revision")

    def test_initialization_refuses_changed_metadata_and_invalid_identifiers(self):
        initialize_study_run(self.engine, "test", self.configuration, "dialogue_v1")
        changed_configuration = {**self.configuration, "dialogue_count": 12}

        with self.assertRaisesRegex(ValueError, "different immutable metadata"):
            initialize_study_run(self.engine, "test", changed_configuration, "dialogue_v1")
        with self.assertRaisesRegex(ValueError, "Run ID"):
            initialize_study_run(self.engine, "test;drop_tables", self.configuration, "dialogue_v1")

    def test_partial_run_schema_is_not_silently_repaired(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE raw_test (event_id TEXT PRIMARY KEY)"))

        with self.assertRaisesRegex(ValueError, "partial run initialization"):
            initialize_study_run(self.engine, "test", self.configuration, "dialogue_v1")

    def test_cli_requires_human_verification_for_the_study_run_config(self):
        runner = server.app.test_cli_runner()
        with patch.dict(server.app.config, TURNSTILE_ENABLED=False):
            result = runner.invoke(args=[
                "init-study-run",
                "--run-id", "verification_test",
                "--config", "config/study_test.json",
                "--protocol-version", "dialogue_v1",
            ])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires human verification", result.output)


if __name__ == "__main__":
    unittest.main()
