"""Persist completed room snapshots and export private CSV/JSON study data."""

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

import click
from flask import Flask, current_app
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

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
from city_candidates import CANDIDATE_CITIES


PARTICIPANT_COLUMNS = (
    "session_id", "dataset", "study_started_at", "study_completed_at",
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
    "room_id", "room_code", "dataset", "condition", "interviewer_session_id",
    "Interviewee_session_id", "dialogue_duration_seconds", "judgment_duration_seconds",
    "dialogue_number", "claim", "ground_truth", "judgment", "confidence", "correct",
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


def _timestamp() -> str:
    """Return the server time used to label this export operation."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _serialize_feedback(feedback: Feedback | None) -> dict[str, Any] | None:
    if feedback is None:
        return None
    return {
        "task_skill": feedback.task_skill,
        "task_difficulty": feedback.task_difficulty,
        "task_motivation": feedback.task_motivation,
        "strategy": feedback.strategy,
        "additional_feedback": feedback.additional_feedback,
        "submitted_at": feedback.submitted_at,
    }


def _serialize_screening(submission: ScreeningSubmission | None) -> dict[str, Any] | None:
    if submission is None:
        return None
    return {
        "submission_id": submission.submission_id,
        "version": submission.version,
        "submitted_at": submission.submitted_at,
        "claim_order": list(submission.claim_order),
        "answers": [
            {"city_id": answer.city_id, "visited": answer.visited, "familiarity": answer.familiarity}
            for answer in submission.answers
        ],
    }


def _room_for_participant(participant: Participant) -> tuple[Room | None, str | None, ScreeningSubmission | None]:
    """Return the participant's room, role, and role-matched screening record."""
    room = next(iter(participant.interviewer_rooms), None)
    if room is not None:
        return room, "interviewer", room.interviewer_screening
    room = next(iter(participant.interviewee_rooms), None)
    if room is not None:
        return room, "interviewee", room.interviewee_screening
    return None, None, None


def _page_visit_rows(participant: Participant) -> list[dict[str, Any]]:
    return [
        {
            "sequence": visit.sequence,
            "page_name": visit.page_name,
            "entered_at": visit.entered_at,
            "exited_at": visit.exited_at,
            "duration_seconds": visit.duration_seconds,
            "room_id": visit.room_id,
        }
        for visit in participant.page_visits
    ]


def _participant_export(participant: Participant) -> dict[str, Any]:
    room, role, screening = _room_for_participant(participant)
    page_visits = _page_visit_rows(participant)
    totals = {
        name: round(sum(
            (visit["duration_seconds"] or 0.0)
            for visit in page_visits
            if visit["page_name"] in page_names
        ), 3)
        for name, page_names in PAGE_DURATION_COLUMNS.items()
    }
    answers = {
        answer["city_id"]: answer
        for answer in (_serialize_screening(screening) or {}).get("answers", [])
    }
    row = {
        "session_id": participant.study_session_id,
        "dataset": participant.dataset,
        "study_started_at": participant.study_started_at,
        "study_completed_at": participant.prolific_redirect_at,
        **totals,
        "consent_accepted": participant.consent_accepted,
        "room_id": room.id if room else None,
        "room_code": room.room_code if room else None,
        "role": role,
        # Condition belongs to the paired room, so both participants' rows carry the same value.
        "condition": room.condition if room else None,
        "task_skill": participant.feedback.task_skill if participant.feedback else None,
        "task_difficulty": participant.feedback.task_difficulty if participant.feedback else None,
        "task_motivation": participant.feedback.task_motivation if participant.feedback else None,
        "strategy": participant.feedback.strategy if participant.feedback else None,
        "additional_feedback": participant.feedback.additional_feedback if participant.feedback else None,
    }
    for city_id, _, _ in CANDIDATE_CITIES:
        answer = answers.get(city_id, {})
        row[f"screening_{city_id}_visited"] = answer.get("visited")
        row[f"screening_{city_id}_familiarity"] = answer.get("familiarity")
    return {
        **row,
        "screening": _serialize_screening(screening),
        "page_visits": page_visits,
        "feedback_submitted_at": participant.feedback.submitted_at if participant.feedback else None,
    }


def _dialogue_turns(room: Room, dialogue: DialogueRecord) -> list[dict[str, Any]]:
    """Pair interviewer questions with the following interviewee responses for one claim."""
    messages = list(room.messages)
    claim = next((message for message in messages if message.is_claim and message.text == dialogue.claim_text), None)
    if claim is None:
        return []
    next_claim_sequence = next(
        (message.sequence for message in messages if message.is_claim and message.sequence > claim.sequence),
        None,
    )
    dialogue_messages = [
        message for message in messages
        if not message.is_claim
        and message.sequence > claim.sequence
        and (next_claim_sequence is None or message.sequence < next_claim_sequence)
    ]
    turns = []
    pending_question = None
    for message in dialogue_messages:
        if message.role == "interviewer":
            pending_question = message
        elif message.role == "interviewee":
            turns.append({
                "interviewer_message": pending_question.text if pending_question else None,
                "question_type_interviewer_message": pending_question.question_type if pending_question else None,
                "interviewee_message": message.text,
            })
            pending_question = None
    return turns


def _dialogue_export(room: Room, dialogue: DialogueRecord) -> dict[str, Any]:
    return {
        "dialogue_number": dialogue.dialogue_number,
        "claim": dialogue.claim_text,
        "ground_truth": dialogue.truth_status,
        "dialogue_duration_seconds": dialogue.dialogue_duration_seconds,
        "judgment_duration_seconds": dialogue.judgement_duration_seconds,
        "judgment": dialogue.judgement,
        "confidence": dialogue.confidence,
        "correct": dialogue.correct,
        "started_at": dialogue.started_at,
        "conversation_ended_at": dialogue.conversation_ended_at,
        "judgement_submitted_at": dialogue.judgement_submitted_at,
        "judgement_ended_at": dialogue.judgement_ended_at,
        "turns": _dialogue_turns(room, dialogue),
    }


def _room_export(room: Room) -> dict[str, Any]:
    return {
        "room_id": room.id,
        "room_code": room.room_code,
        "dataset": room.dataset,
        "condition": room.condition,
        "interviewer_session_id": room.interviewer_study_session_id,
        "interviewee_session_id": room.interviewee_study_session_id,
        "interviewer_screening": _serialize_screening(room.interviewer_screening),
        "interviewee_screening": _serialize_screening(room.interviewee_screening),
        "created_at": room.created_at,
        "paired_at": room.paired_at,
        "chat_started_at": room.chat_started_at,
        "completed_at": room.completed_at,
        "messages": [
            {
                "sequence": message.sequence,
                "session_id": message.participant_id,
                "role": message.role,
                "message": message.text,
                "question_type": message.question_type,
                "is_claim": message.is_claim,
                "sent_at": message.sent_at,
            }
            for message in room.messages
        ],
        "dialogues": [_dialogue_export(room, dialogue) for dialogue in room.dialogues],
        "feedback": {
            feedback.participant_id: _serialize_feedback(feedback)
            for feedback in room.feedback_submissions
        },
    }


def _participant_csv_row(participant_export: dict[str, Any]) -> dict[str, Any]:
    # CSVs stay flat; the corresponding JSON retains detailed page visits and screening metadata.
    return {
        key: value
        for key, value in participant_export.items()
        if key not in {"screening", "page_visits", "feedback_submitted_at"}
    }


def _room_csv_row(room_export: dict[str, Any], dialogue: dict[str, Any]) -> dict[str, Any]:
    row = {
        "room_id": room_export["room_id"],
        "room_code": room_export["room_code"],
        "dataset": room_export["dataset"],
        "condition": room_export["condition"],
        "interviewer_session_id": room_export["interviewer_session_id"],
        "Interviewee_session_id": room_export["interviewee_session_id"],
        "dialogue_duration_seconds": dialogue["dialogue_duration_seconds"],
        "judgment_duration_seconds": dialogue["judgment_duration_seconds"],
        "dialogue_number": dialogue["dialogue_number"],
        "claim": dialogue["claim"],
        "ground_truth": dialogue["ground_truth"],
        "judgment": dialogue["judgment"],
        "confidence": dialogue["confidence"],
        "correct": dialogue["correct"],
    }
    for turn_number in range(1, 11):
        turn = dialogue["turns"][turn_number - 1] if turn_number <= len(dialogue["turns"]) else {}
        row[f"interviewer_message_{turn_number}"] = turn.get("interviewer_message")
        row[f"question_type_interviewer_message_{turn_number}"] = turn.get("question_type_interviewer_message")
        row[f"interviewee_message_{turn_number}"] = turn.get("interviewee_message")
    return row


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(path, 0o600)


def archive_completed_room(
    room_code: str,
    room_state: dict,
    study_sessions: dict,
    screening_submissions: dict,
    feedback_participant_id: str,
    feedback_record: dict,
) -> bool:
    """Atomically upsert the room snapshot and the submitting participant's feedback."""
    try:
        participants = room_state["participants"]
        interviewer_id = next(pid for pid, role in participants.items() if role == "interviewer")
        interviewee_id = next(pid for pid, role in participants.items() if role == "interviewee")
        study_ids = room_state["study_session_ids"]
        screening_ids = room_state["screening_submission_ids"]
        room_id = room_state["archive_id"]
        participant_models = {}

        with db.session.begin():
            for participant_id in (interviewer_id, interviewee_id):
                study_id = study_ids[participant_id]
                submission_id = screening_ids[participant_id]
                state = study_sessions[study_id]
                screening = screening_submissions[submission_id]
                participant = db.session.get(Participant, study_id)
                if participant is None:
                    participant = Participant(study_session_id=study_id)
                    db.session.add(participant)
                participant.dataset = current_app.config["STUDY_DATASET"]
                participant.study_started_at = state.get("study_started_at") or screening["timestamp"]
                participant.consent_accepted = state.get("consent_accepted", False)
                participant.consent_accepted_at = state.get("consent_accepted_at")
                participant.entry_verified = state.get("entry_verified", False)
                participant.travel_intro_seen = state.get("travel_intro_seen", False)
                participant.travel_screened_out = state.get("travel_screened_out", False)
                participant.screening_complete_version = state.get("screening_complete_version")
                participant.instructions_complete = state.get("instructions_complete", False)
                participant.prolific_redirect_at = state.get("prolific_redirect_at")
                participant.duration_seconds = state.get("duration_seconds")
                participant_models[participant_id] = participant

                submission = db.session.get(ScreeningSubmission, submission_id)
                if submission is None:
                    submission = ScreeningSubmission(submission_id=submission_id)
                    db.session.add(submission)
                submission.participant_id = study_id
                submission.version = screening["version"]
                submission.submitted_at = screening["timestamp"]
                submission.claim_order = list(screening["order"])
                old_answers = {answer.city_id: answer for answer in submission.answers}
                for city_id, answer in old_answers.items():
                    if city_id not in screening["answers"]:
                        submission.answers.remove(answer)
                for city_id, answer in screening["answers"].items():
                    saved_answer = old_answers.get(city_id)
                    if saved_answer is None:
                        saved_answer = ScreeningAnswer(city_id=city_id)
                        submission.answers.append(saved_answer)
                    saved_answer.visited = answer["visited"]
                    saved_answer.familiarity = answer["familiarity"]

            room = db.session.get(Room, room_id)
            if room is None:
                room = Room(id=room_id)
                db.session.add(room)
            room.dataset = current_app.config["STUDY_DATASET"]
            room.room_code = room_code
            room.condition = room_state["conditions"][interviewer_id]
            room.interviewer_study_session_id = study_ids[interviewer_id]
            room.interviewee_study_session_id = study_ids[interviewee_id]
            room.interviewer_screening_submission_id = screening_ids[interviewer_id]
            room.interviewee_screening_submission_id = screening_ids[interviewee_id]
            room.created_at = room_state.get("created_at") or room_state["paired_at"]
            room.paired_at = room_state["paired_at"]
            room.chat_started_at = room_state.get("chat_started_at")
            room.completed_at = room_state.get("completed_at")

            old_messages = {message.sequence: message for message in room.messages}
            retained_sequences = {message["id"] for message in room_state.get("messages", [])}
            for sequence, message in old_messages.items():
                if sequence not in retained_sequences:
                    room.messages.remove(message)
            for message_data in room_state.get("messages", []):
                message = old_messages.get(message_data["id"])
                if message is None:
                    message = Message(sequence=message_data["id"])
                    room.messages.append(message)
                message.participant_id = study_ids[message_data["participant_id"]]
                message.role = message_data["role"]
                message.text = message_data["text"]
                message.question_type = message_data.get("question_type")
                message.is_claim = message_data.get("is_claim", False)
                message.sent_at = message_data["timestamp"]

            old_dialogues = {item.dialogue_number: item for item in room.dialogues}
            retained_dialogues = {item["dialogue_number"] for item in room_state.get("dialogues", [])}
            for number, item in old_dialogues.items():
                if number not in retained_dialogues:
                    room.dialogues.remove(item)
            for data in room_state.get("dialogues", []):
                item = old_dialogues.get(data["dialogue_number"])
                if item is None:
                    item = DialogueRecord(dialogue_number=data["dialogue_number"])
                    room.dialogues.append(item)
                for field in (
                    "city_id", "claim_text", "truth_status", "started_at", "conversation_ended_at",
                    "dialogue_duration_seconds", "judgement", "confidence", "correct",
                    "judgement_submitted_at", "judgement_ended_at", "judgement_duration_seconds",
                ):
                    setattr(item, field, data[field])

            for participant_id, participant in participant_models.items():
                state = study_sessions[study_ids[participant_id]]
                old_visits = {visit.sequence: visit for visit in participant.page_visits}
                retained_visits = {visit["sequence"] for visit in state.get("page_visits", [])}
                for sequence, visit in old_visits.items():
                    if sequence not in retained_visits:
                        participant.page_visits.remove(visit)
                for data in state.get("page_visits", []):
                    visit = old_visits.get(data["sequence"])
                    if visit is None:
                        visit = PageVisit(sequence=data["sequence"])
                        participant.page_visits.append(visit)
                    visit.room_id = data.get("room_id")
                    visit.page_name = data["page_name"]
                    visit.entered_at = data["entered_at"]
                    visit.exited_at = data.get("exited_at")
                    visit.duration_seconds = data.get("duration_seconds")

            participant = participant_models[feedback_participant_id]
            feedback = db.session.get(Feedback, participant.study_session_id)
            if feedback is None:
                feedback = Feedback(participant=participant)
                db.session.add(feedback)
            source = room_state.get("feedback_submissions", {}).get(feedback_participant_id, feedback_record)
            feedback.room_id = room_id
            for field in (
                "task_skill", "task_difficulty", "task_motivation", "strategy", "additional_feedback",
            ):
                setattr(feedback, field, source[field])
            feedback.submitted_at = source["timestamp"]
        return True
    except (SQLAlchemyError, KeyError, TypeError, ValueError):
        db.session.rollback()
        current_app.logger.exception("Could not archive completed study data to the TEST database.")
        return False


def record_prolific_handoff(
    study_session_id: str,
    room_id: str,
    redirect_at: str,
    duration_seconds: float | None,
    page_visits: list[dict],
    room_completed_at: str | None,
) -> bool:
    """Persist final duration, page visit, and shared room completion timestamps."""
    try:
        with db.session.begin():
            participant = db.session.get(Participant, study_session_id)
            room = db.session.get(Room, room_id)
            if participant is None or room is None:
                raise ValueError("The archived participant or room record is missing.")
            participant.prolific_redirect_at = redirect_at
            participant.duration_seconds = duration_seconds
            old_visits = {visit.sequence: visit for visit in participant.page_visits}
            for data in page_visits:
                visit = old_visits.get(data["sequence"])
                if visit is None:
                    visit = PageVisit(sequence=data["sequence"], page_name=data["page_name"], entered_at=data["entered_at"])
                    participant.page_visits.append(visit)
                visit.room_id = data.get("room_id")
                visit.page_name = data["page_name"]
                visit.entered_at = data["entered_at"]
                visit.exited_at = data.get("exited_at")
                visit.duration_seconds = data.get("duration_seconds")
            if room_completed_at is not None:
                room.completed_at = room_completed_at
        return True
    except (SQLAlchemyError, ValueError):
        db.session.rollback()
        current_app.logger.exception("Could not persist final TEST study timing.")
        return False


# Export contract: preserve the agreed column order and spelling exactly.
PARTICIPANT_COLUMNS = (
    "session_id", "dataset", "study_started_at", "study_completed_at",
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
    "room_id", "room_code", "dataset", "condition", "interviewer_session_id",
    "Interviewee_session_id", "dialogue_duration_seconds", "judgment_duration_seconds",
    "dialogue_number", "claim", "ground_truth", "judgment", "confidence", "correct",
) + tuple(
    column
    for turn in range(1, 11)
    for column in (
        f"interviewer_message_{turn}",
        f"question_type_interviewer_message_{turn}",
        f"interviewee_message_{turn}",
    )
)
PAGE_DURATIONS = {
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


def _json_screening(submission: ScreeningSubmission | None) -> dict[str, Any] | None:
    if submission is None:
        return None
    return {
        "submission_id": submission.submission_id,
        "version": submission.version,
        "submitted_at": submission.submitted_at,
        "claim_order": list(submission.claim_order),
        "answers": [
            {"city_id": answer.city_id, "visited": answer.visited, "familiarity": answer.familiarity}
            for answer in submission.answers
        ],
    }


def _json_participant(participant: Participant) -> dict[str, Any]:
    room = next(iter(participant.interviewer_rooms), None)
    role = "interviewer" if room else None
    if room is None:
        room = next(iter(participant.interviewee_rooms), None)
        role = "interviewee" if room else None
    screening = None
    if room is not None:
        screening = room.interviewer_screening if role == "interviewer" else room.interviewee_screening
    page_visits = [
        {
            "sequence": visit.sequence,
            "page_name": visit.page_name,
            "entered_at": visit.entered_at,
            "exited_at": visit.exited_at,
            "duration_seconds": visit.duration_seconds,
            "room_id": visit.room_id,
        }
        for visit in participant.page_visits
    ]
    durations = {
        name: round(sum(
            visit["duration_seconds"] or 0.0
            for visit in page_visits
            if visit["page_name"] in page_names
        ), 3)
        for name, page_names in PAGE_DURATIONS.items()
    }
    answers = {
        answer["city_id"]: answer
        for answer in (_json_screening(screening) or {}).get("answers", [])
    }
    row = {
        "session_id": participant.study_session_id,
        "dataset": participant.dataset,
        "study_started_at": participant.study_started_at,
        "study_completed_at": participant.prolific_redirect_at,
        **durations,
        "consent_accepted": participant.consent_accepted,
        "room_id": room.id if room else None,
        "room_code": room.room_code if room else None,
        "role": role,
        # Condition is assigned to the room, so interviewer and interviewee receive the same exported value.
        "condition": room.condition if room else None,
        "task_skill": participant.feedback.task_skill if participant.feedback else None,
        "task_difficulty": participant.feedback.task_difficulty if participant.feedback else None,
        "task_motivation": participant.feedback.task_motivation if participant.feedback else None,
        "strategy": participant.feedback.strategy if participant.feedback else None,
        "additional_feedback": participant.feedback.additional_feedback if participant.feedback else None,
    }
    for city_id, _, _ in CANDIDATE_CITIES:
        answer = answers.get(city_id, {})
        row[f"screening_{city_id}_visited"] = answer.get("visited")
        row[f"screening_{city_id}_familiarity"] = answer.get("familiarity")
    row["screening"] = _json_screening(screening)
    row["page_visits"] = page_visits
    row["feedback_submitted_at"] = participant.feedback.submitted_at if participant.feedback else None
    return row


def _json_room(room: Room) -> dict[str, Any]:
    messages = list(room.messages)
    claims = [message for message in messages if message.is_claim]
    dialogues = []
    for dialogue in room.dialogues:
        claim = next((message for message in claims if message.text == dialogue.claim_text), None)
        start = claim.sequence if claim else None
        next_start = next((message.sequence for message in claims if start is not None and message.sequence > start), None)
        pending_question = None
        turns = []
        for message in messages:
            if message.is_claim or (start is not None and message.sequence <= start) or (next_start is not None and message.sequence >= next_start):
                continue
            if message.role == "interviewer":
                pending_question = message
            elif message.role == "interviewee":
                turns.append({
                    "interviewer_message": pending_question.text if pending_question else None,
                    "question_type_interviewer_message": pending_question.question_type if pending_question else None,
                    "interviewee_message": message.text,
                })
                pending_question = None
        dialogues.append({
            "dialogue_number": dialogue.dialogue_number,
            "claim": dialogue.claim_text,
            "ground_truth": dialogue.truth_status,
            "dialogue_duration_seconds": dialogue.dialogue_duration_seconds,
            "judgment_duration_seconds": dialogue.judgement_duration_seconds,
            "judgment": dialogue.judgement,
            "confidence": dialogue.confidence,
            "correct": dialogue.correct,
            "started_at": dialogue.started_at,
            "conversation_ended_at": dialogue.conversation_ended_at,
            "judgement_submitted_at": dialogue.judgement_submitted_at,
            "judgement_ended_at": dialogue.judgement_ended_at,
            "turns": turns,
        })
    return {
        "room_id": room.id,
        "room_code": room.room_code,
        "dataset": room.dataset,
        "condition": room.condition,
        "interviewer_session_id": room.interviewer_study_session_id,
        "interviewee_session_id": room.interviewee_study_session_id,
        "interviewer_screening": _json_screening(room.interviewer_screening),
        "interviewee_screening": _json_screening(room.interviewee_screening),
        "created_at": room.created_at,
        "paired_at": room.paired_at,
        "chat_started_at": room.chat_started_at,
        "completed_at": room.completed_at,
        "messages": [
            {
                "sequence": message.sequence,
                "session_id": message.participant_id,
                "role": message.role,
                "message": message.text,
                "question_type": message.question_type,
                "is_claim": message.is_claim,
                "sent_at": message.sent_at,
            }
            for message in messages
        ],
        "dialogues": dialogues,
        "feedback": {
            feedback.participant_id: {
                "task_skill": feedback.task_skill,
                "task_difficulty": feedback.task_difficulty,
                "task_motivation": feedback.task_motivation,
                "strategy": feedback.strategy,
                "additional_feedback": feedback.additional_feedback,
                "submitted_at": feedback.submitted_at,
            }
            for feedback in room.feedback_submissions
        },
    }


def _room_csv_row(room: dict[str, Any], dialogue: dict[str, Any]) -> dict[str, Any]:
    row = {
        "room_id": room["room_id"],
        "room_code": room["room_code"],
        "dataset": room["dataset"],
        "condition": room["condition"],
        "interviewer_session_id": room["interviewer_session_id"],
        "Interviewee_session_id": room["interviewee_session_id"],
        "dialogue_duration_seconds": dialogue["dialogue_duration_seconds"],
        "judgment_duration_seconds": dialogue["judgment_duration_seconds"],
        "dialogue_number": dialogue["dialogue_number"],
        "claim": dialogue["claim"],
        "ground_truth": dialogue["ground_truth"],
        "judgment": dialogue["judgment"],
        "confidence": dialogue["confidence"],
        "correct": dialogue["correct"],
    }
    for turn_number in range(1, 11):
        turn = dialogue["turns"][turn_number - 1] if turn_number <= len(dialogue["turns"]) else {}
        row[f"interviewer_message_{turn_number}"] = turn.get("interviewer_message")
        row[f"question_type_interviewer_message_{turn_number}"] = turn.get("question_type_interviewer_message")
        row[f"interviewee_message_{turn_number}"] = turn.get("interviewee_message")
    return row


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(path, 0o600)


def export_study(output: Path, dataset: str) -> dict[str, int]:
    """Write private CSV and JSON exports for one dataset label."""
    output = output.expanduser().resolve()
    static_root = Path(current_app.static_folder).resolve()
    if output == static_root or static_root in output.parents:
        raise click.ClickException("Export output must not be inside the public static directory.")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    room_directory = output / "rooms"
    room_directory.mkdir(exist_ok=True, mode=0o700)
    os.chmod(room_directory, 0o700)

    participants = db.session.execute(
        select(Participant).where(Participant.dataset == dataset)
        .order_by(Participant.study_started_at, Participant.study_session_id)
    ).scalars().all()
    rooms = db.session.execute(
        select(Room).where(Room.dataset == dataset).order_by(Room.created_at, Room.id)
    ).scalars().all()
    participant_data = [_json_participant(participant) for participant in participants]
    room_data = [_json_room(room) for room in rooms]

    with (output / "participants.csv").open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=PARTICIPANT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(participant_data)
    os.chmod(output / "participants.csv", 0o600)

    with (output / "rooms.csv").open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=ROOM_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for room in room_data:
            for dialogue in room["dialogues"]:
                writer.writerow(_room_csv_row(room, dialogue))
    os.chmod(output / "rooms.csv", 0o600)

    exported_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    participants_json = {"dataset": dataset, "exported_at": exported_at, "participants": participant_data}
    rooms_json = {"dataset": dataset, "exported_at": exported_at, "rooms": room_data}
    complete_json = {"dataset": dataset, "exported_at": exported_at, "participants": participant_data, "rooms": room_data}

    def write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    write_json(output / "participants.json", participants_json)
    write_json(output / "rooms.json", rooms_json)
    write_json(output / "complete.json", complete_json)
    for room in room_data:
        write_json(room_directory / f"room_{room['room_id']}.json", {
            "dataset": dataset, "exported_at": exported_at, "room": room,
        })
    return {
        "participants": len(participants),
        "rooms": len(rooms),
        "dialogues": sum(len(room["dialogues"]) for room in room_data),
    }


def register_study_data(app: Flask) -> None:
    """Register the explicit output-path TEST data export command."""
    @app.cli.command("export-study")
    @click.option("--output", type=click.Path(path_type=Path), required=True)
    @click.option("--dataset", default=None, help="Defaults to STUDY_DATASET.")
    def export_study_command(output: Path, dataset: str | None) -> None:
        selected_dataset = dataset or current_app.config["STUDY_DATASET"]
        counts = export_study(output, selected_dataset)
        click.echo(
            f"Exported dataset={selected_dataset}: {counts['participants']} participants, "
            f"{counts['rooms']} rooms, {counts['dialogues']} dialogues to {output.resolve()}"
        )
