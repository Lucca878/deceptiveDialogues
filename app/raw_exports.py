"""Build private participant and room exports from run-specific raw events."""

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

import click
from flask import Flask, current_app
from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Engine

from app.run_tables import RUN_ID_PATTERN
from city_candidates import CANDIDATE_CITIES


PARTICIPANT_COLUMNS = (
    "participant_id", "prolific_id", "study_run_id", "study_started_at", "study_completed_at",
    "verification_duration_seconds", "landing_duration_seconds", "consent_duration_seconds",
    "screening_duration_seconds", "instructions_duration_seconds", "room_duration_seconds",
    "role_instructions_seconds", "dialogue_duration_seconds", "feedback_duration_seconds",
    "debriefing_duration_seconds", "study_ended_duration_seconds", "consent_accepted",
    "room_id", "room_code", "role", "condition", "task_skill", "task_difficulty",
    "task_motivation", "strategy", "additional_feedback",
) + tuple(
    column
    for city_id, _, _ in CANDIDATE_CITIES
    for column in (f"screening_{city_id}_visited", f"screening_{city_id}_familiarity")
)

ROOM_COLUMNS = (
    "room_id", "room_code", "study_run_id", "condition", "interviewer_participant_id",
    "interviewee_participant_id", "dialogue_duration_seconds", "judgement_duration_seconds",
    "dialogue_number", "claim", "ground_truth", "judgement", "confidence", "correct",
) + tuple(
    column
    for turn_number in range(1, 11)
    for column in (
        f"interviewer_message_{turn_number}",
        f"question_type_interviewer_message_{turn_number}",
        f"interviewee_message_{turn_number}",
    )
)

PAGE_DURATION_COLUMNS = {
    "verification_duration_seconds": {"verification"},
    "landing_duration_seconds": {"landing"},
    "consent_duration_seconds": {"consent"},
    "screening_duration_seconds": {"travel-intro", "travel-cities"},
    "instructions_duration_seconds": {"instructions"},
    "room_duration_seconds": {"room"},
    "role_instructions_seconds": {"role-instructions"},
    "dialogue_duration_seconds": {"dialogue"},
    "feedback_duration_seconds": {"feedback"},
    "debriefing_duration_seconds": {"debriefing"},
    "study_ended_duration_seconds": {"study-ended"},
}


def _timestamp(value: Any) -> str | None:
    """Normalize typed database timestamps to ISO text for JSON exports."""
    return value.isoformat() if isinstance(value, datetime) else value


def _ordered_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by server event time, using event ID only to break same-millisecond ties."""
    def key(event: dict[str, Any]) -> tuple[str, str]:
        timestamp = event.get("occurred_at") or event.get("started_at") or event.get("ended_at")
        return _timestamp(timestamp) or "", event["event_id"]

    return sorted(events, key=key)


def _raw_table(engine: Engine, run_id: str) -> Table:
    """Reflect an initialized raw table after validating its run identifier."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise click.ClickException("Run ID must start with a lowercase letter and contain only lowercase letters, digits, or underscores (max 32 characters).")
    try:
        return Table(f"raw_{run_id}", MetaData(), autoload_with=engine)
    except Exception as error:
        raise click.ClickException(f"Raw event table raw_{run_id} is not initialized.") from error


def _metadata_table(engine: Engine, run_id: str) -> Table:
    """Reflect the selected run's metadata table after validating its suffix."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise click.ClickException("Run ID must start with a lowercase letter and contain only lowercase letters, digits, or underscores (max 32 characters).")
    try:
        return Table(f"metadata_{run_id}", MetaData(), autoload_with=engine)
    except Exception as error:
        raise click.ClickException(f"Metadata table metadata_{run_id} is not initialized.") from error


def _read_table_rows(engine: Engine, table: Table) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a reflected run table and preserve its database column order."""
    with engine.connect() as connection:
        rows = connection.execute(select(table)).mappings().all()
    return [column.name for column in table.columns], [dict(row) for row in rows]


def _table_export_value(value: Any) -> Any:
    """Convert typed database values into portable CSV/JSON values."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _write_table_exports(output: Path, table_name: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Write one table as stable-named, private CSV and JSON files."""
    csv_path = output / f"{table_name}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([
                json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list))
                else _table_export_value(value)
                for value in (row[column] for column in columns)
            ])
    os.chmod(csv_path, 0o600)

    json_rows = [
        {column: _table_export_value(row[column]) for column in columns}
        for row in rows
    ]
    json_path = output / f"{table_name}.json"
    json_path.write_text(json.dumps(json_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(json_path, 0o600)


def export_run_tables(output: Path, run_id: str, engine: Engine | None = None) -> dict[str, int]:
    """Export only raw_<run_id> and metadata_<run_id> as private table files."""
    output = output.expanduser().resolve()
    static_root = Path(current_app.static_folder).resolve()
    if output == static_root or static_root in output.parents:
        raise click.ClickException("Export output must not be inside the public static directory.")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)

    selected_engine = engine or current_app.extensions["sqlalchemy"].engine
    raw_table = _raw_table(selected_engine, run_id)
    metadata_table = _metadata_table(selected_engine, run_id)
    raw_columns, raw_rows = _read_table_rows(selected_engine, raw_table)
    metadata_columns, metadata_rows = _read_table_rows(selected_engine, metadata_table)
    _write_table_exports(output, raw_table.name, raw_columns, raw_rows)
    _write_table_exports(output, metadata_table.name, metadata_columns, metadata_rows)
    return {"raw_events": len(raw_rows), "metadata_records": len(metadata_rows)}


def _read_events(engine: Engine, run_id: str) -> list[dict[str, Any]]:
    """Load only the selected run's raw events into JSON-compatible mappings."""
    table = _raw_table(engine, run_id)
    with engine.connect() as connection:
        rows = connection.execute(select(table)).mappings().all()
    return _ordered_events([dict(row) for row in rows])


def _page_visits(events: list[dict[str, Any]], participant_id: str) -> list[dict[str, Any]]:
    """Collect page intervals, including verification visits nested in its first event."""
    visits = []
    for event in events:
        if event.get("participant_id") != participant_id:
            continue
        if event["event_type"] == "page_visit":
            visits.append({
                "page_name": event["event_data"]["page_name"],
                "entered_at": _timestamp(event.get("started_at")),
                "exited_at": _timestamp(event.get("ended_at")),
                "duration_seconds": event.get("duration_seconds"),
                "room_id": event.get("room_id"),
            })
        elif event["event_type"] == "human_verification_completed":
            for visit in event["event_data"].get("verification_page_visits", []):
                # Verification intervals use started_at/ended_at in raw data; normalize them for exports.
                visits.append({
                    "page_name": "verification",
                    "entered_at": _timestamp(visit.get("started_at")),
                    "exited_at": _timestamp(visit.get("ended_at")),
                    "duration_seconds": visit.get("duration_seconds"),
                    "room_id": event.get("room_id"),
                })

    visits.sort(key=lambda visit: visit.get("entered_at") or "")
    for sequence, visit in enumerate(visits, start=1):
        visit["sequence"] = sequence
    return visits


def _screening_event(events: list[dict[str, Any]], participant_id: str) -> dict[str, Any] | None:
    """Return this participant's eligible screening event with its answer data."""
    event = next((
        item for item in events
        if item.get("participant_id") == participant_id and item["event_type"] == "screening_completed"
    ), None)
    if event is None:
        return None
    data = event["event_data"]
    return {
        "submission_id": data["submission_id"],
        "version": data["screening_version"],
        "submitted_at": _timestamp(event.get("occurred_at")),
        "claim_order": list(data["claim_order"]),
        "answers": [
            {"city_id": city_id, "visited": answer["visited"], "familiarity": answer["familiarity"]}
            for city_id, answer in sorted(data["answers"].items())
        ],
    }


def _participant_records(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Derive one participant row and a detailed JSON record from its events."""
    participant_ids = {event["participant_id"] for event in events if event.get("participant_id")}
    for event in events:
        if event["event_type"] == "room_paired":
            participant_ids.update(item["participant_id"] for item in event["event_data"]["participants"])

    participant_records = []
    for participant_id in sorted(participant_ids):
        participant_events = [event for event in events if event.get("participant_id") == participant_id]
        pairing = next((
            event for event in events
            if event["event_type"] == "room_paired"
            and any(item["participant_id"] == participant_id for item in event["event_data"]["participants"])
        ), None)
        assignment = next((
            item for item in pairing["event_data"]["participants"] if item["participant_id"] == participant_id
        ), None) if pairing else None
        page_visits = _page_visits(events, participant_id)
        durations = {
            name: round(sum(
                (visit.get("duration_seconds") or 0.0)
                for visit in page_visits
                if visit["page_name"] in page_names
            ), 3)
            for name, page_names in PAGE_DURATION_COLUMNS.items()
        }
        screening = _screening_event(events, participant_id)
        answers = {answer["city_id"]: answer for answer in (screening or {}).get("answers", [])}
        verification = next((
            event for event in participant_events if event["event_type"] == "human_verification_completed"
        ), None)
        consent = next((
            event for event in participant_events if event["event_type"] == "consent_decided"
        ), None)
        study_start = next((
            event for event in participant_events if event["event_type"] == "study_started"
        ), None)
        study_completion = next((
            event for event in participant_events if event["event_type"] == "study_completed"
        ), None)
        feedback = next((
            event for event in participant_events if event["event_type"] == "feedback_submitted"
        ), None)
        feedback_data = feedback["event_data"] if feedback else {}
        row = {
            "participant_id": participant_id,
            "prolific_id": verification["event_data"].get("prolific_id") if verification else None,
            "study_run_id": run_id,
            "study_started_at": _timestamp(study_start.get("occurred_at")) if study_start else None,
            "study_completed_at": _timestamp(study_completion.get("ended_at") or study_completion.get("occurred_at")) if study_completion else None,
            **durations,
            "consent_accepted": consent["event_data"].get("decision") == "accept" if consent else None,
            "room_id": pairing.get("room_id") if pairing else None,
            "room_code": pairing.get("room_code") if pairing else None,
            "role": assignment.get("role") if assignment else None,
            "condition": pairing["event_data"].get("condition") if pairing else None,
            "task_skill": feedback_data.get("task_skill"),
            "task_difficulty": feedback_data.get("task_difficulty"),
            "task_motivation": feedback_data.get("task_motivation"),
            "strategy": feedback_data.get("strategy"),
            "additional_feedback": feedback_data.get("additional_feedback"),
        }
        for city_id, _, _ in CANDIDATE_CITIES:
            answer = answers.get(city_id, {})
            row[f"screening_{city_id}_visited"] = answer.get("visited")
            row[f"screening_{city_id}_familiarity"] = answer.get("familiarity")

        participant_records.append({
            **row,
            "screening": screening,
            "page_visits": page_visits,
            "feedback_submitted_at": _timestamp(feedback.get("occurred_at")) if feedback else None,
        })
    return participant_records


def _room_records(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Reconstruct one room JSON record and one row per completed dialogue from raw events."""
    pairings = [event for event in events if event["event_type"] == "room_paired"]
    room_records = []
    for pairing in pairings:
        room_id = pairing["room_id"]
        room_code = pairing["room_code"]
        room_events = [event for event in events if event.get("room_id") == room_id]
        assignments = pairing["event_data"]["participants"]
        interviewer = next(item["participant_id"] for item in assignments if item["role"] == "interviewer")
        interviewee = next(item["participant_id"] for item in assignments if item["role"] == "interviewee")
        dialogues = []
        question_categories = {}
        for event in room_events:
            if event["event_type"] == "question_type_assigned":
                question_categories[event["event_data"]["target_event_id"]] = event["event_data"]["question_type"]

        for started in (event for event in room_events if event["event_type"] == "dialogue_started"):
            dialogue_number = started["dialogue_number"]
            message_events = [
                event for event in room_events
                if event["event_type"] == "message_sent" and event["dialogue_number"] == dialogue_number
            ]
            turns: dict[int, dict[str, Any]] = {}
            for event in message_events:
                turn_number = event["turn_number"]
                turn = turns.setdefault(turn_number, {})
                role = event["event_data"]["role"]
                if role == "interviewer":
                    turn["interviewer_message"] = event["event_data"]["message"]
                    turn["question_type_interviewer_message"] = question_categories.get(event["event_id"])
                elif role == "interviewee":
                    turn["interviewee_message"] = event["event_data"]["message"]
            turn_rows = [turns[number] for number in sorted(turns)]
            completed = next((
                event for event in room_events
                if event["event_type"] == "dialogue_completed" and event["dialogue_number"] == dialogue_number
            ), {})
            judgement = next((
                event for event in room_events
                if event["event_type"] == "judgement_submitted" and event["dialogue_number"] == dialogue_number
            ), {})
            continued = next((
                event for event in room_events
                if event["event_type"] == "dialogue_continued" and event["dialogue_number"] == dialogue_number
            ), None)
            judgement_data = judgement.get("event_data", {})
            dialogues.append({
                "dialogue_number": dialogue_number,
                "claim": started["event_data"]["claim"],
                "ground_truth": started["event_data"]["ground_truth"],
                "dialogue_duration_seconds": completed.get("duration_seconds"),
                "judgement_duration_seconds": judgement.get("duration_seconds"),
                "judgement": judgement_data.get("judgement"),
                "confidence": judgement_data.get("confidence"),
                "correct": judgement_data.get("correct"),
                "started_at": _timestamp(started.get("started_at")),
                "conversation_ended_at": _timestamp(completed.get("ended_at")),
                "judgement_submitted_at": _timestamp(judgement.get("occurred_at")),
                "judgement_ended_at": _timestamp(continued.get("occurred_at")) if continued else _timestamp(judgement.get("ended_at")),
                "turns": turn_rows,
            })

        room_messages = []
        dialogue_starts = {event["dialogue_number"]: event for event in room_events if event["event_type"] == "dialogue_started"}
        for number, event in dialogue_starts.items():
            room_messages.append({
                "sequence": 0,
                "participant_id": interviewee,
                "role": "interviewee",
                "message": event["event_data"]["claim"],
                "question_type": None,
                "is_claim": True,
                "sent_at": _timestamp(event.get("started_at")),
                "dialogue_number": number,
                "turn_number": 0,
            })
        for event in room_events:
            if event["event_type"] == "message_sent":
                role = event["event_data"]["role"]
                room_messages.append({
                    "sequence": 0,
                    "participant_id": event["participant_id"],
                    "role": role,
                    "message": event["event_data"]["message"],
                    "question_type": question_categories.get(event["event_id"]) if role == "interviewer" else None,
                    "is_claim": False,
                    "sent_at": _timestamp(event.get("occurred_at")),
                    "dialogue_number": event["dialogue_number"],
                    "turn_number": event["turn_number"],
                })
        room_messages.sort(key=lambda message: (message["dialogue_number"], message["turn_number"], message["sent_at"] or ""))
        for sequence, message in enumerate(room_messages, start=1):
            message["sequence"] = sequence

        interviewer_screening = _screening_event(events, interviewer)
        interviewee_screening = _screening_event(events, interviewee)
        feedback = {
            event["participant_id"]: {
                **event["event_data"],
                "submitted_at": _timestamp(event.get("occurred_at")),
            }
            for event in room_events
            if event["event_type"] == "feedback_submitted"
        }
        study_completed_events = [event for event in room_events if event["event_type"] == "study_completed"]
        room_record = {
            "room_id": room_id,
            "room_code": room_code,
            "study_run_id": run_id,
            "condition": pairing["event_data"]["condition"],
            "interviewer_participant_id": interviewer,
            "interviewee_participant_id": interviewee,
            "interviewer_screening": interviewer_screening,
            "interviewee_screening": interviewee_screening,
            "created_at": _timestamp(pairing.get("occurred_at")),
            "paired_at": _timestamp(pairing.get("occurred_at")),
            "chat_started_at": None,
            "completed_at": max(
                (_timestamp(event.get("ended_at") or event.get("occurred_at")) for event in study_completed_events),
                default=None,
            ),
            "messages": room_messages,
            "dialogues": dialogues,
            "feedback": feedback,
        }
        room_records.append(room_record)
    return room_records


def _room_csv_row(room: dict[str, Any], dialogue: dict[str, Any]) -> dict[str, Any]:
    """Flatten a reconstructed dialogue while keeping the agreed wide turn columns."""
    row = {
        "room_id": room["room_id"],
        "room_code": room["room_code"],
        "study_run_id": room["study_run_id"],
        "condition": room["condition"],
        "interviewer_participant_id": room["interviewer_participant_id"],
        "interviewee_participant_id": room["interviewee_participant_id"],
        "dialogue_duration_seconds": dialogue["dialogue_duration_seconds"],
        "judgement_duration_seconds": dialogue["judgement_duration_seconds"],
        "dialogue_number": dialogue["dialogue_number"],
        "claim": dialogue["claim"],
        "ground_truth": dialogue["ground_truth"],
        "judgement": dialogue["judgement"],
        "confidence": dialogue["confidence"],
        "correct": dialogue["correct"],
    }
    # Pair the separate raw interviewer and interviewee events by the server-assigned turn number.
    turns: dict[int, dict[str, Any]] = {}
    for message in room["messages"]:
        if message.get("dialogue_number") != dialogue["dialogue_number"] or message["is_claim"]:
            continue
        turn = turns.setdefault(message["turn_number"], {})
        if message["role"] == "interviewer":
            turn["interviewer_message"] = message["message"]
            turn["question_type_interviewer_message"] = message.get("question_type")
        elif message["role"] == "interviewee":
            turn["interviewee_message"] = message["message"]
    for turn_number in range(1, 11):
        turn = turns.get(turn_number, {})
        row[f"interviewer_message_{turn_number}"] = turn.get("interviewer_message")
        row[f"question_type_interviewer_message_{turn_number}"] = turn.get("question_type_interviewer_message")
        row[f"interviewee_message_{turn_number}"] = turn.get("interviewee_message")
    return row


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Write a private CSV with stable column ordering and restrictive permissions."""
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(path, 0o600)


def export_raw_study(output: Path, run_id: str, engine: Engine | None = None) -> dict[str, int]:
    """Derive private participant and room files from only raw_<run_id>."""
    output = output.expanduser().resolve()
    static_root = Path(current_app.static_folder).resolve()
    if output == static_root or static_root in output.parents:
        raise click.ClickException("Export output must not be inside the public static directory.")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    selected_engine = engine or current_app.extensions["sqlalchemy"].engine
    events = _read_events(selected_engine, run_id)
    participant_data = _participant_records(events, run_id)
    room_data = _room_records(events, run_id)
    export_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    exported_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    _write_csv(output / f"participants_{run_id}_{export_stamp}.csv", PARTICIPANT_COLUMNS, participant_data)
    room_rows = [
        _room_csv_row(room, dialogue)
        for room in room_data
        for dialogue in sorted(room["dialogues"], key=lambda item: item["dialogue_number"])
    ]
    _write_csv(output / f"rooms_{run_id}_{export_stamp}.csv", ROOM_COLUMNS, room_rows)

    participants_json = {"study_run_id": run_id, "exported_at": exported_at, "participants": participant_data}
    rooms_json = {"study_run_id": run_id, "exported_at": exported_at, "rooms": room_data}
    complete_json = {"study_run_id": run_id, "exported_at": exported_at, "participants": participant_data, "rooms": room_data}

    def write_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    write_json(output / f"participants_{run_id}_{export_stamp}.json", participants_json)
    write_json(output / f"rooms_{run_id}_{export_stamp}.json", rooms_json)
    write_json(output / f"complete_{run_id}_{export_stamp}.json", complete_json)
    for room in room_data:
        write_json(output / f"room_{room['room_id']}_{run_id}_{export_stamp}.json", {
            "study_run_id": run_id, "exported_at": exported_at, "room": room,
        })

    return {
        "participants": len(participant_data),
        "rooms": len(room_data),
        "dialogues": sum(len(room["dialogues"]) for room in room_data),
    }


def register_raw_export_command(app: Flask) -> None:
    """Register CLI commands for derived participant/room and direct run-table exports."""
    @app.cli.command("export-study")
    @click.option("--output", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id", default=None, help="Defaults to STUDY_RUN_ID.")
    def export_raw_study_command(output: Path, run_id: str | None) -> None:
        selected_run_id = run_id or current_app.config["STUDY_RUN_ID"]
        counts = export_raw_study(output, selected_run_id)
        click.echo(
            f"Exported run_id={selected_run_id} from raw events: {counts['participants']} participants, "
            f"{counts['rooms']} rooms, {counts['dialogues']} dialogues to {output.resolve()}"
        )

    @app.cli.command("export-study-tables")
    @click.option("--output", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id", default=None, help="Defaults to STUDY_RUN_ID.")
    def export_study_tables_command(output: Path, run_id: str | None) -> None:
        """Write the selected raw and metadata database tables as CSV and JSON."""
        selected_run_id = run_id or current_app.config["STUDY_RUN_ID"]
        counts = export_run_tables(output, selected_run_id)
        click.echo(
            f"Exported raw_{selected_run_id}: {counts['raw_events']} event rows; "
            f"metadata_{selected_run_id}: {counts['metadata_records']} metadata records "
            f"as CSV/JSON to {output.resolve()}"
        )
